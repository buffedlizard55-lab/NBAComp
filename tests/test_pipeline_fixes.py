"""Regression tests for the 2026-09-21 "silent empty pipeline" defects.

Ground truth for every test here is the committed database + collection log
from the Actions runs of 2026-09-21 (data/nbacomp.db, data/diagnostics.txt):

  * collection_log id=680/697: `daily-kalshi-snapshot` crashed with
    `IntegrityError: NOT NULL constraint failed: kalshi_markets.series_ticker`
    -> ZERO Kalshi prices were ever stored.
  * every `backtest` row: status 'empty', detail 'no games in window'
    -> the two-season backfill was gated behind a manual workflow input the
    cron never sets, and the daily job only fetched today-2..today+8.
  * `bref-backfill`/`bref-verify`: '2026-september: HTTP 404' every run
    -> the offseason has no monthly page and that was logged as a failure.

Each test asserts the FIXED behaviour, so a regression reproduces the outage.
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from nbacomp import audit, collect, db, sitegen
from nbacomp.sources import espn

# --- real payload excerpts ------------------------------------------------
SAS_MARKET = {
    "ticker": "KXNBAGAME-26OCT20OKCSAS-SAS",
    "event_ticker": "KXNBAGAME-26OCT20OKCSAS",
    "title": "San Antonio wins",
    "yes_sub_title": "San Antonio",
    "status": "active",
    "close_time": "2026-10-23T01:30:00Z",
    "yes_bid_dollars": "0.5400",
    "yes_ask_dollars": "0.5500",
    "last_price_dollars": "0.5500",
    "volume_fp": "9924.08",
    "open_interest_fp": "7632.77",
    "result": "",
}
MVP_MARKET_NO_SERIES = {
    # observed failure mode: a live row with NO series_ticker key at all
    "ticker": "KXNBAMVP-26MVPWINNER-JOKIC",
    "title": "Nikola Jokic wins MVP",
    "status": "active",
    "yes_bid_dollars": "0.1200",
    "yes_ask_dollars": "0.1300",
    "last_price_dollars": "0.1250",
    "volume_fp": "10.0",
    "open_interest_fp": "5.0",
    "result": "",
}
IDENTITY_LESS = {"ticker": "NOPREFIX", "title": "no event, no series"}


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda *_: None)


# ---------------------------------------------------------------- normalize
def test_normalize_fills_series_from_query_and_event_from_ticker():
    row = {"ticker": "KXNBAMVP-26MVPWINNER-JOKIC", "series_ticker": None,
           "event_ticker": None}
    out = collect.normalize_market_row(row, "KXNBAMVP")
    assert out["series_ticker"] == "KXNBAMVP"          # from the query we sent
    assert out["event_ticker"] == "KXNBAMVP-26MVPWINNER"  # from the ticker itself


def test_normalize_rejects_rows_without_any_identity():
    assert collect.normalize_market_row({"ticker": None}, "KXNBAGAME") is None
    assert collect.normalize_market_row(dict(IDENTITY_LESS), "X") is None


def test_snapshot_stores_markets_that_omit_series_ticker(con, monkeypatch):
    """The exact defect that emptied kalshi_markets on every Actions run."""
    monkeypatch.setattr(collect, "_live_series", lambda c: ["KXNBAMVP", "KXNBAGAME"])
    monkeypatch.setattr(collect.kalshi, "get_markets",
                        lambda s, status=None, max_pages=10, **k:
                        [MVP_MARKET_NO_SERIES] if s == "KXNBAMVP" else [SAS_MARKET])
    monkeypatch.setattr(collect.kalshi, "get_orderbook",
                        lambda t: __import__("nbacomp.http", fromlist=["x"]).HttpResult(
                            404, None, "mock://", error="HTTP 404"))

    n = collect.kalshi_snapshot(con)
    con.commit()
    assert n == 2
    rows = {r["ticker"]: r for r in con.execute("SELECT * FROM kalshi_markets")}
    assert rows["KXNBAMVP-26MVPWINNER-JOKIC"]["series_ticker"] == "KXNBAMVP"
    assert rows["KXNBAGAME-26OCT20OKCSAS-SAS"]["yes_ask"] == 55  # dollar string -> cents
    log = con.execute("SELECT status, detail FROM collection_log WHERE task='kalshi-snapshot' "
                      "AND source='kalshi'").fetchone()
    assert "open_markets=2" in log["detail"]
    assert con.execute("SELECT COUNT(*) c FROM anomalies").fetchone()["c"] == 0


def test_snapshot_flags_anomaly_when_nothing_could_be_stored(con, monkeypatch):
    monkeypatch.setattr(collect, "_live_series", lambda c: ["KXNBAGAME"])
    monkeypatch.setattr(collect.kalshi, "get_markets", lambda *a, **k: [dict(IDENTITY_LESS)])
    assert collect.kalshi_snapshot(con) == 0
    anoms = [r["check_name"] for r in con.execute("SELECT check_name FROM anomalies")]
    assert "kalshi-snapshot-stored-nothing" in anoms
    assert "kalshi-market-missing-identity" in anoms


# ------------------------------------------------------- resumable backfill
def test_espn_backfill_walks_backwards_and_persists_cursor(con, monkeypatch):
    days = ["20250115", "20250114", "20250113"]
    seen = []

    def fake_scoreboard(day):
        seen.append(day)
        return espn_scoreboard_ok(day)

    monkeypatch.setattr(collect.espn, "scoreboard", fake_scoreboard)
    db.insert(con, "meta", {"key": collect.BACKFILL_CURSOR_KEY, "value": "20250115",
                            "updated_utc": "2026-09-21T00:00:00Z"})
    st = collect.espn_backfill_resumable(con, days_per_run=2, floor="20231001")
    assert seen == ["20250114", "20250113"]
    assert st["days_fetched"] == 2 and st["games"] == 2
    assert con.execute("SELECT value FROM meta WHERE key=?",
                       (collect.BACKFILL_CURSOR_KEY,)).fetchone()["value"] == "20250113"
    assert con.execute("SELECT COUNT(*) c FROM games").fetchone()["c"] == 2
    # odds captured alongside the schedule
    assert con.execute("SELECT COUNT(*) c FROM odds_snapshots").fetchone()["c"] > 0


def test_espn_backfill_does_not_advance_cursor_past_a_failure(con, monkeypatch):
    calls = []

    def boom(day):
        calls.append(day)
        return espn_scoreboard_fail()

    monkeypatch.setattr(collect.espn, "scoreboard", boom)
    db.insert(con, "meta", {"key": collect.BACKFILL_CURSOR_KEY, "value": "20250115",
                            "updated_utc": "2026-09-21T00:00:00Z"})
    st = collect.espn_backfill_resumable(con, days_per_run=5)
    assert calls == ["20250114"]              # stopped, did not skip ahead
    assert st["stopped"].startswith("20250114")
    assert con.execute("SELECT value FROM meta WHERE key=?",
                       (collect.BACKFILL_CURSOR_KEY,)).fetchone()["value"] == "20250115"
    assert con.execute("SELECT status FROM collection_log WHERE task='espn-backfill'"
                       ).fetchone()["status"] == "fail"


def test_espn_backfill_stops_at_floor_and_reports_done(con, monkeypatch):
    seen = []
    monkeypatch.setattr(collect.espn, "scoreboard",
                        lambda d: (seen.append(d), espn_scoreboard_ok(d))[1])
    db.insert(con, "meta", {"key": collect.BACKFILL_CURSOR_KEY, "value": "20231003",
                            "updated_utc": "2026-09-21T00:00:00Z"})
    st = collect.espn_backfill_resumable(con, days_per_run=10, floor="20231001")
    assert seen == ["20231002", "20231001"]
    assert st["done"] is True
    assert con.execute("SELECT status FROM collection_log WHERE task='espn-backfill'"
                       ).fetchone()["status"] == "done"


def test_boxscore_backfill_skips_days_without_finals_and_respects_budget(con, monkeypatch):
    for day, gid in (("2025-01-14", "espn:1"), ("2025-01-15", "espn:2"), ("2025-01-16", "espn:3")):
        db.insert(con, "games", {
            "game_id": gid, "source": "espn", "season": "2024-25", "game_date_et": day,
            "tipoff_utc": day + "T00:30:00Z", "home_team": "BOS", "away_team": "LAL",
            "home_score": 100, "away_score": 90, "status": "final", "captured_utc": "x"})

    touched = []
    monkeypatch.setattr(collect, "collect_boxscores",
                        lambda c, d: (touched.append(d), 4)[1])
    st = collect.boxscores_backfill_resumable(con, "20250114", "20250116", max_games=1)
    assert touched == ["20250114"]            # budget of 1 game spent, then stop
    assert st["done"] is False and st["next"] == "20250115"
    assert con.execute("SELECT value FROM meta WHERE key='boxscore_backfill_next_day'"
                       ).fetchone()["value"] == "20250115"

    st2 = collect.boxscores_backfill_resumable(con, "20250114", "20250116", max_games=50)
    assert touched == ["20250114", "20250115", "20250116"]   # resumes where it stopped
    assert st2["done"] is True and st2["next"] == "20250117"


def test_boxscore_backfill_never_refetches_a_day_already_logged(con, monkeypatch):
    db.insert(con, "games", {
        "game_id": "espn:1", "source": "espn", "season": "2024-25", "game_date_et": "2025-01-14",
        "tipoff_utc": "2025-01-14T00:30:00Z", "home_team": "BOS", "away_team": "LAL",
        "home_score": 100, "away_score": 90, "status": "final", "captured_utc": "x"})
    db.insert(con, "team_gamelogs", {
        "season": "2024-25", "game_id": "espn:1", "game_date_et": "2025-01-14", "team": "BOS",
        "opp": "LAL", "is_home": 1, "pts": 100, "opp_pts": 90, "source": "espn:summary",
        "captured_utc": "x"})
    touched = []
    monkeypatch.setattr(collect, "collect_boxscores", lambda c, d: touched.append(d) or 0)
    collect.boxscores_backfill_resumable(con, "20250114", "20250116", max_games=50)
    assert touched == []                       # already have logs -> no request


# ------------------------------------------------------------------- BRef
def test_bref_current_month_skips_offseason_instead_of_logging_failure(con):
    st = collect.bref_current_month(con, dt.datetime(2026, 9, 21))
    assert st["skipped"] == "2026-september"
    row = con.execute("SELECT status, detail FROM collection_log WHERE task='bref-backfill'").fetchone()
    assert row["status"] == "skipped"
    assert "offseason" in row["detail"]
    assert con.execute("SELECT COUNT(*) c FROM source_status WHERE source_id LIKE 'bref:%'"
                       ).fetchone()["c"] == 0        # no pointless 404 request


def test_bref_current_month_uses_season_end_year_for_october(con, monkeypatch):
    calls = []
    monkeypatch.setattr(collect.bref, "backfill_month",
                        lambda c, y, m: calls.append(("backfill", y, m)) or {})
    monkeypatch.setattr(collect.bref, "verify_month",
                        lambda c, y, m: calls.append(("verify", y, m)) or 0)
    collect.bref_current_month(con, dt.datetime(2026, 10, 21))
    assert calls == [("backfill", 2027, "october"), ("verify", 2027, "october")]


# --------------------------------------------------- settled-history probe
def test_event_ticker_construction_matches_verified_format():
    assert collect.event_ticker_for("2026-10-20", "OKC", "SAS") == "KXNBAGAME-26OCT20OKCSAS"
    assert collect.event_ticker_for("2025-06-13", "NYK", "SAS") == "KXNBAGAME-25JUN13NYKSAS"
    assert collect.event_ticker_candidates("2026-10-20", "SAS", "OKC") == [
        "KXNBAGAME-26OCT20OKCSAS", "KXNBAGAME-26OCT20SASOKC"]
    assert collect.market_ticker_candidates("E", "SAS", "OKC") == [
        "E-OKC", "E-SAS", "E-YES", "E-NO"]
    assert collect.event_ticker_for("not-a-date", "BOS", "LAL") is None


def test_settled_history_records_unavailable_when_api_returns_nothing(con, monkeypatch):
    _add_final_game(con)
    probed = {}
    monkeypatch.setattr(collect.kalshi, "get_candlesticks",
                        lambda tickers, s, e, i=60: probed.update(tickers=tickers) or [])
    monkeypatch.setattr(collect.kalshi, "_get",
                        lambda path, params=None, **k: _http(404, None))
    out = collect.kalshi_settled_history(con)
    assert out["availability"] == "unavailable"
    assert out["tickers_probed"] == 8          # 2 event orders x 4 suffixes
    assert set(probed["tickers"]) >= {"KXNBAGAME-25JUN13NYKSAS-NYK", "KXNBAGAME-25JUN13NYKSAS-YES"}
    # honest, machine-readable record of the negative result
    meta = json.loads(con.execute(
        "SELECT value FROM meta WHERE key='kalshi_settled_availability:KXNBAGAME'"
    ).fetchone()["value"])
    assert meta["availability"] == "unavailable"
    assert con.execute("SELECT COUNT(*) c FROM kalshi_candles").fetchone()["c"] == 0
    assert con.execute("SELECT status FROM collection_log WHERE task='kalshi-settled-history' "
                       "AND source='kalshi:KXNBAGAME'").fetchone()["status"] == "empty"


def test_settled_history_stores_only_tickers_that_returned_candles(con, monkeypatch):
    _add_final_game(con)
    hit = "KXNBAGAME-25JUN13NYKSAS-SAS"
    candle = {"end_period_ts": 1781000000, "price": {"open_dollars": "0.38",
                                                    "high_dollars": "0.40",
                                                    "low_dollars": "0.37",
                                                    "close_dollars": "0.39"},
              "volume_fp": "707.69"}
    monkeypatch.setattr(collect.kalshi, "get_candlesticks",
                        lambda tickers, s, e, i=60:
                        [{"market_ticker": hit, "candlesticks": [candle]}])
    monkeypatch.setattr(collect.kalshi, "_get", lambda path, params=None, **k: _http(200, {"trades": []}))
    out = collect.kalshi_settled_history(con)
    assert out["availability"] == "candles-available" and out["candles"] == 1
    rows = list(con.execute("SELECT * FROM kalshi_markets"))
    assert [r["ticker"] for r in rows] == [hit]      # nothing invented for the misses
    assert rows[0]["series_ticker"] == "KXNBAGAME"
    assert rows[0]["event_ticker"] == "KXNBAGAME-25JUN13NYKSAS"
    assert rows[0]["yes_bid"] is None and rows[0]["result"] is None  # no fabricated quote
    c = con.execute("SELECT * FROM kalshi_candles").fetchone()
    assert c["open"] == 38 and c["close"] == 39 and c["volume"] == 707


def test_settled_history_without_games_is_logged_not_guessed(con):
    out = collect.kalshi_settled_history(con)
    assert out["availability"] == "no-games"
    assert con.execute("SELECT COUNT(*) c FROM kalshi_markets").fetchone()["c"] == 0


# ------------------------------------------------------------------- audit
def test_audit_flags_empty_pipeline_as_critical(con):
    summary = audit.run_checks(con)
    names = {c["check"] for c in summary["checks"]}
    assert "empty-games-table" in names
    assert "empty-kalshi-markets-table" in names
    assert summary["critical"] >= 2


def test_audit_flags_collector_crashes_and_persistent_failures(con):
    db.log_collection(con, "daily-kalshi-snapshot", "nbacomp", "crash", "Traceback ...")
    for _ in range(6):
        db.log_collection(con, "bref-backfill", "basketball-reference", "fail", "HTTP 404")
    summary = audit.run_checks(con)
    names = {c["check"] for c in summary["checks"]}
    assert "collector-crash" in names
    assert "persistent-source-failure" in names
    crash = next(c for c in summary["checks"] if c["check"] == "collector-crash")
    assert crash["detail"]["tasks"] == ["daily-kalshi-snapshot"]


def test_audit_does_not_flag_no_bets_without_data(con):
    _add_final_game(con)
    summary = audit.run_checks(con)
    assert "no-bets-despite-data" not in {c["check"] for c in summary["checks"]}


# -------------------------------------------------------------------- site
def test_dashboard_shows_pipeline_health_and_coverage(tmp_path, con):
    audit.run_checks(con)                       # populates anomalies
    con.commit()
    sitegen.index(con, str(tmp_path))
    html = (tmp_path / "index.html").read_text()
    assert "Pipeline health" in html
    assert "empty-games-table" in html          # failures are published, not hidden
    assert "Kalshi NBA markets stored" in html


def test_dashboard_reports_clean_health_when_no_critical_anomalies(tmp_path, con):
    sitegen.index(con, str(tmp_path))
    html = (tmp_path / "index.html").read_text()
    assert "no critical anomalies" in html


# ---------------------------------------------------------------- helpers
def _add_final_game(con):
    db.insert(con, "games", {
        "game_id": "espn:401584999", "source": "espn", "season": "2024-25",
        "game_date_et": "2025-06-13", "tipoff_utc": "2025-06-14T00:30:00Z",
        "home_team": "NYK", "away_team": "SAS", "home_score": 110, "away_score": 101,
        "status": "final", "captured_utc": "2026-09-21T00:00:00Z"})


def _http(status, payload):
    from nbacomp import http
    body = json.dumps(payload).encode() if payload is not None else None
    return http.HttpResult(status, body, "mock://kalshi",
                           error=None if 200 <= status < 300 else f"HTTP {status}")


def espn_scoreboard_ok(day, home="BOS", away="LAL", hs="112", as_="105", gid=None):
    from nbacomp import http
    y, m, d = day[:4], day[4:6], day[6:]
    payload = {"events": [{
        "id": gid or f"4017{day}",
        "date": f"{y}-{m}-{d}T00:30Z",
        "season": {"year": int(y) + (1 if int(m) >= 10 else 0), "type": 2},
        "status": {"type": {"state": "post"}},
        "competitions": [{
            "neutral": False,
            "competitors": [
                {"homeAway": "home", "id": "2", "team": {"abbreviation": home,
                                                         "displayName": "Home Team"},
                 "score": hs, "winner": True},
                {"homeAway": "away", "id": "13", "team": {"abbreviation": away,
                                                          "displayName": "Away Team"},
                 "score": as_, "winner": False},
            ],
            "odds": [{"provider": {"name": "ESPN BET"}, "details": "BOS -6.5",
                      "overUnder": 221.5, "spread": -6.5,
                      "homeTeamOdds": {"moneyLine": -260},
                      "awayTeamOdds": {"moneyLine": 215}}],
        }],
    }]}
    return http.HttpResult(200, json.dumps(payload).encode(), "mock://espn")


def espn_scoreboard_fail():
    from nbacomp import http
    return http.HttpResult(0, None, "mock://espn", error="URLError: timed out")


def test_boxscore_backfill_skips_games_without_an_espn_id(con, monkeypatch):
    """Run 35553630400 wasted its whole box-score budget on BRef-only rows:
    `bref:2024-10-28-MIA-DET`.split(':')[1] is a date fragment, not an ESPN
    event id, so every summary request 404'd (41 logged failures)."""
    db.insert(con, "games", {
        "game_id": "bref:2024-10-28-MIA-DET", "source": "basketball-reference",
        "season": "2024-25", "game_date_et": "2024-10-28",
        "tipoff_utc": "2024-10-28T23:30:00Z", "home_team": "DET", "away_team": "MIA",
        "home_score": 100, "away_score": 90, "status": "final", "captured_utc": "x"})
    touched = []
    monkeypatch.setattr(collect, "collect_boxscores", lambda c, d: touched.append(d) or 0)
    st = collect.boxscores_backfill_resumable(con, "20241028", "20241030", max_games=40)
    assert touched == []            # no ESPN id -> no request
    assert st["games"] == 0 and st["done"] is True
    assert con.execute("SELECT COUNT(*) c FROM collection_log WHERE status='fail'"
                       ).fetchone()["c"] == 0


def test_espn_backfill_adopts_existing_bref_row_instead_of_duplicating(con, monkeypatch):
    """Run 35553997534 raised 5 `duplicate-game` warnings: the BRef bootstrap
    row and the ESPN row for the same game both existed under different ids,
    and the BRef one could never be joined to box scores or odds."""
    db.insert(con, "games", {
        # 2025-01-14T00:30Z is the 13th in ET, which is the ET date ESPN reports
        "game_id": "bref:2025-01-13-LAL-BOS", "source": "basketball-reference",
        "season": "2024-25", "game_date_et": "2025-01-13", "tipoff_utc": "2025-01-14T00:30:00Z",
        "home_team": "BOS", "away_team": "LAL", "home_score": 112, "away_score": 105,
        "status": "final", "captured_utc": "x", "verified": 1})
    monkeypatch.setattr(collect.espn, "scoreboard",
                        lambda day: espn_scoreboard_ok(day, gid="401799999"))
    db.insert(con, "meta", {"key": collect.BACKFILL_CURSOR_KEY, "value": "20250115",
                            "updated_utc": "x"})
    collect.espn_backfill_resumable(con, days_per_run=1)

    rows = list(con.execute("SELECT * FROM games"))
    assert len(rows) == 1                                  # one game, not two
    assert rows[0]["game_id"] == "espn:401799999"          # ESPN identity wins
    assert rows[0]["source"] == "espn"
    assert rows[0]["verified"] == 1                        # verification preserved
    assert con.execute("SELECT COUNT(*) c FROM audit_log WHERE action='game-merge'"
                       ).fetchone()["c"] == 1
    summary = audit.run_checks(con)
    assert "duplicate-game" not in {c["check"] for c in summary["checks"]}


def test_espn_backfill_is_idempotent_for_its_own_rows(con, monkeypatch):
    monkeypatch.setattr(collect.espn, "scoreboard",
                        lambda day: espn_scoreboard_ok(day, gid="401799999"))
    db.insert(con, "meta", {"key": collect.BACKFILL_CURSOR_KEY, "value": "20250115",
                            "updated_utc": "x"})
    collect.espn_backfill_resumable(con, days_per_run=1)
    db.insert(con, "meta", {"key": collect.BACKFILL_CURSOR_KEY, "value": "20250115",
                            "updated_utc": "x"}, replace=True)
    collect.espn_backfill_resumable(con, days_per_run=1)
    assert con.execute("SELECT COUNT(*) c FROM games").fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM audit_log WHERE action='game-merge'"
                       ).fetchone()["c"] == 0


def test_espn_forward_window_adds_upcoming_games_without_blanketing_existing(con, monkeypatch):
    """probe7 proved past ESPN scoreboards carry NO odds, so upcoming dates are
    the only source of tipoffs and pre-game lines. The daily job used to look
    8 days ahead, so the 2026-10-20 openers (the only games with live Kalshi
    markets) had no row for the forward engine to join to."""
    # the fixture dates its event 00:30Z on the requested day; espn._et_date
    # subtracts 5h, so the ET date is the previous UTC day. Day 1 of the
    # forward window is tomorrow, i.e. ET date == today (UTC).
    et_day = collect.datetime.now(collect.timezone.utc).strftime("%Y-%m-%d")
    db.insert(con, "games", {
        "game_id": "bref:keepme", "source": "basketball-reference",
        "season": "2024-25", "game_date_et": et_day, "tipoff_utc": "2025-01-14T00:30:00Z",
        "home_team": "BOS", "away_team": "LAL", "home_score": 112, "away_score": 105,
        "status": "final", "captured_utc": "x", "verified": 1})
    days = []
    monkeypatch.setattr(collect.espn, "scoreboard",
                        lambda day: (days.append(day), espn_scoreboard_ok(day, gid="401800001"))[1])
    st = collect.espn_forward_window(con, days_ahead=2)
    assert st["days"] == 2
    # day 1 matches the existing (date, away, home) -> skipped; day 2 is new
    assert st["skipped"] == 1 and st["inserted"] == 1
    rows = {r["game_id"]: r for r in con.execute("SELECT * FROM games")}
    assert rows["bref:keepme"]["home_score"] == 112      # untouched
    assert rows["bref:keepme"]["status"] == "final"
    assert len([r for r in rows.values() if r["source"] == "espn"]) == 1


def test_espn_forward_window_never_overwrites_a_final_score(con, monkeypatch):
    # day 1 of the forward window is tomorrow 00:30Z -> ET date == today (UTC)
    et_day = collect.datetime.now(collect.timezone.utc).strftime("%Y-%m-%d")
    db.insert(con, "games", {
        "game_id": "bref:x", "source": "basketball-reference", "season": "2025-26",
        "game_date_et": et_day, "tipoff_utc": "2025-01-14T00:30:00Z",
        "home_team": "BOS", "away_team": "LAL", "home_score": 130, "away_score": 90,
        "status": "final", "captured_utc": "x", "verified": 1})
    monkeypatch.setattr(collect.espn, "scoreboard",
                        lambda day: espn_scoreboard_ok(day, gid="401800002"))
    st = collect.espn_forward_window(con, days_ahead=1)
    assert st["inserted"] == 0 and st["skipped"] == 1
    row = con.execute("SELECT * FROM games WHERE game_id='bref:x'").fetchone()
    assert row["home_score"] == 130 and row["status"] == "final"
    assert con.execute("SELECT COUNT(*) c FROM games").fetchone()["c"] == 1


# ------------------------------------------------- team vocabulary (probe8)
def test_canon_team_maps_espn_abbrevs_to_the_kalshi_bref_set():
    """Run 35554765928 stored the same game twice because ESPN says NY/GS/SA/
    UTAH/WSH/NO while BRef and Kalshi event tickers say NYK/GSW/SAS/UTA/WAS/
    NOP — 17 duplicate-game anomalies and no Kalshi market could be joined."""
    from nbacomp import engine
    assert engine.canon_team("NY") == "NYK" and engine.canon_team("GS") == "GSW"
    assert engine.canon_team("SA") == "SAS" and engine.canon_team("UTAH") == "UTA"
    assert engine.canon_team("WSH") == "WAS" and engine.canon_team("NO") == "NOP"
    assert engine.canon_team("BOS") == "BOS" and engine.canon_team(None) is None
    assert engine.is_nba_team("NYK") and engine.is_nba_team("WSH")
    for junk in ("STARS", "STRIPES", "WORLD", "GUANGZHOU", "HAPOEL", "LON", "MEL"):
        assert not engine.is_nba_team(junk)
    assert len(engine.TEAM_NAMES) == 30


def test_espn_collectors_canonicalize_and_reject_non_nba_games(con, monkeypatch):
    monkeypatch.setattr(collect.espn, "scoreboard",
                        lambda day: espn_scoreboard_ok(day, home="NY", away="GS", gid="401900001"))
    collect.collect_espn_day(con, "20261020")
    row = con.execute("SELECT * FROM games").fetchone()
    assert row["home_team"] == "NYK" and row["away_team"] == "GSW"

    monkeypatch.setattr(collect.espn, "scoreboard",
                        lambda day: espn_scoreboard_ok(day, home="STARS", away="STRIPES",
                                                       gid="401900002"))
    collect.collect_espn_day(con, "20261021")
    assert con.execute("SELECT COUNT(*) c FROM games").fetchone()["c"] == 1
    anoms = [r["check_name"] for r in con.execute("SELECT check_name FROM anomalies")]
    assert "non-nba-game-skipped" in anoms


def test_repair_team_vocab_canonicalizes_dedups_and_purges_non_nba(con):
    """Executed for real against the committed DB from run 35554765928:
    637 game rows + 366 gamelog rows renamed, 497 duplicate games merged,
    10 non-NBA rows deleted -> 2,886 games, 0 duplicates, 30 abbreviations."""
    for gid, src, home, away, hs in (
            ("bref:2026-05-11-CLE-DET", "basketball-reference", "DET", "CLE", 100),
            ("espn:401871336", "espn", "DET", "CLE", 100),
            ("espn:401900003", "espn", "STARS", "STRIPES", 99),
            ("espn:401900004", "espn", "NY", "GS", None)):
        db.insert(con, "games", {
            "game_id": gid, "source": src, "season": "2025-26",
            "game_date_et": "2026-05-11" if gid != "espn:401900004" else "2026-10-20",
            "tipoff_utc": "2026-05-12T00:00:00Z", "home_team": home, "away_team": away,
            "home_score": hs, "away_score": 90 if hs else None,
            "status": "final" if hs else "scheduled", "captured_utc": "x"})
    stats = collect.repair_team_vocab(con)
    assert stats["deleted_non_nba"] == 1
    assert stats["merged_duplicates"] == 1
    assert stats["renamed_games"] >= 2
    rows = {r["game_id"]: r for r in con.execute("SELECT * FROM games")}
    assert "espn:401871336" in rows and "bref:2026-05-11-CLE-DET" not in rows
    assert rows["espn:401900004"]["home_team"] == "NYK"
    assert rows["espn:401900004"]["away_team"] == "GSW"
    assert all(r["home_team"] in engine_teams() for r in rows.values())
    assert con.execute("SELECT COUNT(*) c FROM audit_log WHERE action='merge-duplicate-game'"
                       ).fetchone()["c"] == 1


def engine_teams():
    from nbacomp import engine
    return set(engine.TEAM_NAMES)


def test_drop_incomplete_gamelogs_removes_null_pts_rows(con):
    db.insert(con, "team_gamelogs", {
        "season": "2025-26", "game_id": "espn:1", "game_date_et": "2026-05-11", "team": "DET",
        "opp": "CLE", "is_home": 1, "pts": None, "opp_pts": 112, "source": "espn:summary",
        "captured_utc": "x"})
    db.insert(con, "team_gamelogs", {
        "season": "2025-26", "game_id": "espn:2", "game_date_et": "2026-05-12", "team": "BOS",
        "opp": "LAL", "is_home": 1, "pts": 110, "opp_pts": 100, "source": "espn:summary",
        "captured_utc": "x"})
    assert collect.drop_incomplete_gamelogs(con) == 1
    left = [r["team"] for r in con.execute("SELECT team FROM team_gamelogs")]
    assert left == ["BOS"]
    assert collect.drop_incomplete_gamelogs(con) == 0      # idempotent


def test_audit_flags_non_nba_and_aliased_team_abbreviations(con):
    db.insert(con, "games", {
        "game_id": "espn:bad1", "source": "espn", "season": "2026-27",
        "game_date_et": "2026-10-05", "tipoff_utc": "2026-10-05T23:00:00Z",
        "home_team": "STARS", "away_team": "NY", "status": "scheduled", "captured_utc": "x"})
    summary = audit.run_checks(con)
    names = {c["check"] for c in summary["checks"]}
    assert "non-nba-team-in-games" in names
    assert "non-canonical-team-abbreviation" in names
