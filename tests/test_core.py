import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from nbacomp import audit, db, engine, sitegen, strategies as S, util


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


# ---------------------------------------------------------------- odds math

def test_american_decimal_roundtrip():
    for o in (-250, -110, 100, 150, 300):
        assert abs(util.american_to_decimal(util.decimal_to_american(util.american_to_decimal(o))) -
                   util.american_to_decimal(o)) < 1e-9


def test_american_to_prob():
    assert util.american_to_prob(-100) == pytest.approx(0.5)
    assert util.american_to_prob(100) == pytest.approx(0.5)
    assert util.american_to_prob(-200) == pytest.approx(2 / 3)
    with pytest.raises(ValueError):
        util.american_to_prob(0)


def test_devig_two_way():
    h, a = util.devig_two_way(0.6, 0.45)
    assert h + a == pytest.approx(1.0)
    assert h > a


def test_cents_to_prob_bounds():
    assert util.cents_to_prob(50) == 0.5
    with pytest.raises(ValueError):
        util.cents_to_prob(0)
    with pytest.raises(ValueError):
        util.cents_to_prob(100)


# ---------------------------------------------------------------- kalshi fees

def test_kalshi_fees_symmetric_and_positive():
    assert util.kalshi_fees_dollars(100, 50) >= 0
    assert util.kalshi_fees_dollars(100, 10) == util.kalshi_fees_dollars(100, 90)
    assert util.kalshi_fees_dollars(0, 50) == 0
    # formula check: ceil(0.07 * C * P * (1-P))
    import math
    expected = math.ceil(round(0.07 * 10 * 0.65 * 0.35, 6) - 1e-9)
    assert util.kalshi_fees_dollars(10, 65) == expected


def test_kalshi_payoff():
    # win 10 contracts at 65c: payout 10 - 6.5 - fee
    pnl = util.kalshi_payoff(10, 65, True)
    assert pnl == pytest.approx(10 - 6.5 - util.kalshi_fees_dollars(10, 65))
    loss = util.kalshi_payoff(10, 65, False)
    assert loss == pytest.approx(-6.5 - util.kalshi_fees_dollars(10, 65))


# ---------------------------------------------------------------- sizing

def test_kelly_and_caps():
    # positive edge -> positive stake
    s = S.stake_for(1000, 0.55, 2.0)
    assert 0 < s <= 1000 * S.MAX_STAKE_PCT
    # no edge -> no stake
    assert S.stake_for(1000, 0.5, 2.0) == 0
    # tiny bankroll -> below min stake -> 0
    assert S.stake_for(50, 0.9, 2.0) == 0


# ---------------------------------------------------------------- settlement

def test_settle_winner_and_spread_and_total():
    assert engine.settle_score_based("winner", 110, 100, "home", None) == "win"
    assert engine.settle_score_based("winner", 110, 100, "away", None) == "loss"
    assert engine.settle_score_based("spread", 110, 105, "home -4", 4.0) == "win"  # margin +(-4) > 0
    assert engine.settle_score_based("spread", 108, 104, "home -4", 4.0) == "push"
    assert engine.settle_score_based("total", 110, 100, "over 210", 210) == "push"
    assert engine.settle_score_based("total", 111, 100, "over 210", 210) == "win"
    assert engine.settle_score_based("total", 100, 100, "under 210", 210) == "win"  # 200 < 210
    assert engine.settle_score_based("total", 105, 105, "under 210", 210) == "push"
    assert engine.settle_score_based("total", 100, 100, "over 210", 210) == "loss"


def test_push_returns_no_pnl():
    assert engine.american_pnl(50, -110, "push") == 0.0
    assert engine.kalshi_bet_pnl(10, 50, "push") == 0.0


def test_overtime_included_in_final_scores():
    """Final scores include OT by definition of the source (documented); a game
    tied after regulation but won in OT must settle as a ML win, not push."""
    assert engine.settle_score_based("winner", 112, 110, "home", None) == "win"


def test_american_pnl_math():
    assert engine.american_pnl(100, -110, "win") == pytest.approx(90.909, abs=0.01)
    assert engine.american_pnl(100, -110, "loss") == -100
    assert engine.american_pnl(100, 150, "win") == 150


# ---------------------------------------------------------------- time

def test_parse_iso_and_et():
    dt = util.parse_iso("2026-01-15T00:30:00Z")
    assert dt.tzinfo is not None
    assert util.et_game_date(dt) == "2026-01-14"
    assert util.parse_iso(None) is None
    assert util.parse_iso("garbage") is None


def test_hours_between():
    a = util.parse_iso("2026-01-15T00:00:00Z")
    b = util.parse_iso("2026-01-15T06:00:00Z")
    assert util.hours_between(a, b) == 6.0


# ---------------------------------------------------------------- pricing

def _seed_game(con, game_id="espn:1", tipoff="2026-01-15T01:00:00Z", status="final",
               hs=112, as_=105):
    db.insert(con, "games", {
        "game_id": game_id, "source": "espn", "season": "2025-26",
        "game_date_et": "2026-01-14", "tipoff_utc": tipoff,
        "home_team": "BOS", "away_team": "LAL", "home_score": hs, "away_score": as_,
        "status": status, "neutral_site": 0, "source_updated_utc": None,
        "captured_utc": util.utcnow_iso(), "verified": 1}, replace=True)


def _seed_kalshi(con, ticker="KXNBAGAME-26JAN15BOSLAL-BOS"):
    db.insert(con, "kalshi_markets", {
        "ticker": ticker, "series_ticker": "KXNBAGAME", "event_ticker": "KXNBAGAME-26JAN15BOSLAL",
        "title": "Celtics vs Lakers", "subtitle": "BOS @ LAL".replace("BOS @ LAL", "Lakers @ Celtics"),
        "market_type": "winner", "strike_values": None, "status": "settled",
        "close_time": "2026-01-15T03:30:00Z", "expected_expiration_time": None,
        "yes_bid": None, "yes_ask": None, "last_price": 100, "volume": 5000,
        "open_interest": 1000, "result": "yes", "settled_time": "2026-01-15T03:35:00Z",
        "captured_utc": util.utcnow_iso()}, replace=True)
    return ticker


def test_pricebook_excludes_partially_open_candles(con):
    """A candle that OPENS before the decision but CLOSES after it must be
    excluded (its close price doesn't exist yet at decision time)."""
    _seed_game(con)
    t = _seed_kalshi(con)
    # hourly candle at 23:00 closes at 00:00; decision is 23:30 -> must be excluded
    for ts, close in [("2026-01-14T21:00:00Z", 60),
                      ("2026-01-14T22:00:00Z", 62),
                      ("2026-01-14T23:00:00Z", 99)]:
        db.insert(con, "kalshi_candles", {
            "ticker": t, "interval": 60, "ts_utc": ts, "open": close, "high": close,
            "low": close, "close": close, "volume": 10, "captured_utc": util.utcnow_iso()})
    book = engine.PriceBook(con)
    p = book.kalshi_price_at(t, "2026-01-14T23:30:00Z")
    assert p is not None and p.price_cents == 63  # 22:00 candle closes exactly 23:00


def test_settlement_kalshi_side_aware(con):
    """Away bets are NO on the home market: result 'yes' must settle them as
    LOSS and 'no' as WIN. Home bets are the YES side. Regression for the
    side-blind settlement bug."""
    _seed_game(con)
    info = engine.KalshiMarketInfo(
        ticker="KXNBAGAME-26JAN14BOSMIA", event_ticker="KXNBAGAME-26JAN14BOSMIA",
        series_ticker="KXNBAGAME", market_type="winner", game_id="g1",
        home="MIA", away="BOS", strike={}, result="yes", title="", subtitle="")
    # home bet (YES) vs result yes -> win
    assert engine.apply_settlement_kalshi(con, info, {"side": "home"}, 110, 100) == "win"
    # away bet (NO) vs result yes -> LOSS (was wrongly 'win' before the fix)
    assert engine.apply_settlement_kalshi(con, info, {"side": "away"}, 110, 100) == "loss"
    # result flips to 'no': home loses, away wins
    info2 = engine.KalshiMarketInfo(**{**info.__dict__, "result": "no"})
    assert engine.apply_settlement_kalshi(con, info2, {"side": "home"}, 110, 100) == "loss"
    assert engine.apply_settlement_kalshi(con, info2, {"side": "away"}, 110, 100) == "win"
    # no recorded Kalshi result -> score inference, side-aware
    info3 = engine.KalshiMarketInfo(**{**info.__dict__, "result": None})
    assert engine.apply_settlement_kalshi(con, info3, {"side": "home"}, 95, 120) == "loss"
    assert engine.apply_settlement_kalshi(con, info3, {"side": "away"}, 95, 120) == "win"
    # mismatch between kalshi result and score must be logged, not hidden
    info4 = engine.KalshiMarketInfo(**{**info.__dict__, "result": "no"})
    r = engine.apply_settlement_kalshi(con, info4, {"side": "home"}, 120, 95)
    assert r == "loss"  # kalshi says home lost; score says home won -> anomaly logged
    a = con.execute("SELECT COUNT(*) FROM anomalies WHERE check_name='kalshi-settlement-mismatch'").fetchone()[0]
    assert a >= 1


def test_paper_bets_dedup_across_runs(con):
    """The same strategy may hold only ONE bet per game/market/selection:
    bet_ids embed the decision timestamp, so re-running collection on the
    same edge must NOT stack duplicate positions."""
    from nbacomp import paper, strategies as S
    _seed_game(con, game_id="espn:f1", tipoff="2026-01-15T01:00:00Z", status="scheduled")
    winner = engine.KalshiMarketInfo(
        ticker="KXNBAGAME-26JAN15BOSLAL-BOS", event_ticker="KXNBAGAME-26JAN15BOSLAL",
        series_ticker="KXNBAGAME", market_type="winner", game_id="espn:f1",
        home="BOS", away="LAL", strike={}, result=None, title="", subtitle="")
    live = engine.PricePoint("2026-01-14T20:00:00Z", 55.0, "orderbook_ask")
    sig = S.Signal(strategy_id="NBA-003", game_id="espn:f1", market="kalshi:winner",
                   selection="BOS", side="home", price=55.0, price_format="kalshi_cents",
                   source="kalshi:orderbook", model_prob=0.65, market_prob=0.55,
                   trigger="elo edge", game_label="LAL @ BOS 2026-01-14",
                   tipoff_utc="2026-01-15T01:00:00Z", source_ts=live.ts_utc)
    ctx = {"game": con.execute("SELECT * FROM games WHERE game_id='espn:f1'").fetchone(),
           "decision": "2026-01-14T20:00:00Z", "home": "BOS", "away": "LAL"}
    n1 = paper._place_forward_bet(con, ctx, sig, winner, live)
    assert n1 == 1
    row = con.execute("SELECT result, verification FROM bets WHERE bet_id LIKE 'fo-%'").fetchone()
    assert row["result"] == "pending"
    assert "DERIVED" not in row["verification"]  # home side is the observed ask
    # second collection run (new decision ts, same edge) must not add a position
    sig2 = S.Signal(**{**sig.__dict__, "trigger": "elo edge (run 2)"})
    n2 = paper._place_forward_bet(con, ctx, sig2, winner, live)
    assert n2 == 0
    assert con.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 1


def test_espn_ml_probs_staleness(con):
    """Divergence signals must not trade against stale ESPN snapshots."""
    from nbacomp import paper
    _seed_game(con, game_id="espn:f2", tipoff="2026-01-15T01:00:00Z", status="scheduled")
    decision = "2026-01-14T20:00:00Z"
    db.insert(con, "odds_snapshots", {
        "game_id": "espn:f2", "captured_utc": "2026-01-13T12:00:00Z",  # 32h stale
        "source": "espn:consensus", "market": "ml", "selection": "home",
        "price": -150, "line": None, "price_format": "american",
        "source_url": "x", "source_updated_utc": None})
    db.insert(con, "odds_snapshots", {
        "game_id": "espn:f2", "captured_utc": "2026-01-13T12:00:00Z",
        "source": "espn:consensus", "market": "ml", "selection": "away",
        "price": 130, "line": None, "price_format": "american",
        "source_url": "x", "source_updated_utc": None})
    assert paper.espn_ml_probs(con, "espn:f2", decision, max_age_hours=24) is None
    assert paper.espn_ml_probs(con, "espn:f2", decision, max_age_hours=48) is not None


def test_kelly_sizing_uses_paid_odds_not_fair_odds():
    """Regression: passing the MODEL fair decimal as payout odds makes Kelly
    f* identically zero (b*p - q = 0) -> no bets anywhere. Payout must come
    from the odds actually paid."""
    b = 100.0 / 55.0 - 1.0  # paid 55c -> decimal 1.818
    f = util.kelly_fraction(0.65, b + 1.0, 0.25)
    assert f > 0.01
    stake = S.stake_for(1000.0, 0.65, 100.0 / 55.0)
    assert stake > 0
    # the degenerate call that silently zeroed every stake:
    assert util.kelly_fraction(0.65, util.prob_to_fair_decimal(0.65), 0.25) == 0.0


def test_subtitle_dates_and_probe5_mapping(con):
    """probe5: settled Kalshi subtitles look like 'NYK at SAS (Jun 13)' with no
    year; full titles use city names. Mapping must resolve both without
    mis-assigning to the wrong meeting of the same two teams."""
    assert engine.subtitle_dates("NYK at SAS (Jun 13)") == ["2026-06-13", "2025-06-13"]
    assert engine.subtitle_dates("(Foo 13)") == []
    assert engine.match_teams_in_text("Game 5: New York at San Antonio") == {"NYK", "SAS"}
    # two BOS/LAL meetings in the same season: a year-less subtitle must pick
    # the one matching the subtitle month/day (Jun 13 -> the June game)
    _seed_game(con, game_id="g-jan", tipoff="2026-01-15T01:00:00Z", status="final")
    db.insert(con, "games", {
        "game_id": "g-jun", "source": "espn", "season": "2025-26",
        "game_date_et": "2026-06-13", "tipoff_utc": "2026-06-13T23:30:00Z",
        "home_team": "BOS", "away_team": "LAL", "home_score": 100, "away_score": 98,
        "status": "final", "neutral_site": 0, "source_updated_utc": None,
        "captured_utc": util.utcnow_iso(), "verified": 1}, replace=True)
    db.insert(con, "kalshi_markets", {
        "ticker": "KXNBAGAME-26JUN13BOSLAL-BOS", "series_ticker": "KXNBAGAME",
        "event_ticker": "KXNBAGAME-26JUN13BOSLAL", "title": "Game 5: Los Angeles at Boston",
        "subtitle": "LAL at BOS (Jun 13)", "market_type": "winner", "strike_values": None,
        "status": "settled", "close_time": None, "expected_expiration_time": None,
        "yes_bid": None, "yes_ask": None, "last_price": 100, "volume": 10,
        "open_interest": 5, "result": "yes", "settled_time": None,
        "captured_utc": util.utcnow_iso()}, replace=True)
    infos = engine.map_kalshi_markets(con)
    assert infos["KXNBAGAME-26JUN13BOSLAL-BOS"].game_id == "g-jun"


def test_bets_append_only_trigger(con):
    """Historical bet records are immutable at the DB level: only settlement
    columns may be updated; everything else aborts (constraint: corrections
    require audit entries, never silent rewrites)."""
    _seed_game(con)
    db.insert(con, "bets", {
        "bet_id": "fo-x", "run_id": "r", "kind": "forward", "strategy_id": "NBA-003",
        "strategy_version": "1.0", "username": "u", "decision_utc": "2026-01-14T21:00:00Z",
        "game_id": "espn:1", "game_label": "g", "tipoff_utc": "2026-01-15T01:00:00Z",
        "market": "kalshi:winner", "selection": "BOS", "side": "home",
        "price": 55.0, "price_format": "kalshi_cents", "source": "kalshi:orderbook",
        "stake_usd": 30.0, "execution_status": "simulated_fill"}, replace=True)
    # settlement path: allowed
    con.execute("UPDATE bets SET result='win', settlement_utc='z', settlement_source='s', "
                "pnl_usd=24.5, roi=0.8 WHERE bet_id='fo-x'")
    # rewriting history: aborted by trigger
    import sqlite3
    try:
        con.execute("UPDATE bets SET price=10.0 WHERE bet_id='fo-x'")
        raise SystemExit("trigger failed to block decision-column rewrite")
    except sqlite3.IntegrityError as e:
        assert "append-only" in str(e)
    try:
        con.execute("UPDATE bets SET model_prob=0.9 WHERE bet_id='fo-x'")
        raise SystemExit("trigger failed to block model_prob rewrite")
    except sqlite3.IntegrityError:
        pass


def test_parse_event_ticker():
    date, teams = engine.parse_event_ticker("KXNBAGAME-26OCT20OKCSAS")
    assert date == "2026-10-20"
    assert teams == {"OKC", "SAS"}
    date2, teams2 = engine.parse_event_ticker("KXNBAGAME-25JAN15BOSLAL")
    assert date2 == "2025-01-15" and teams2 == {"BOS", "LAL"}
    assert engine.parse_event_ticker("KXNBAGAME-GARBAGE")[1] == set()
    assert engine.parse_event_ticker("KXNBAGAME-26OCT20XXXYYY")[1] == set()


def test_pricebook_no_lookahead(con):
    _seed_game(con)
    t = _seed_kalshi(con)
    # candle exactly at decision time is excluded; only strictly-before is used
    for i, (ts, close) in enumerate([
        ("2026-01-14T20:00:00Z", 60),
        ("2026-01-14T22:00:00Z", 64),
        ("2026-01-14T23:00:00Z", 66),  # last one strictly before decision
        ("2026-01-14T23:30:00Z", 90),  # AFTER decision (decision = 23:00) -> excluded
    ]):
        db.insert(con, "kalshi_candles", {
            "ticker": t, "interval": 60, "ts_utc": ts, "open": close, "high": close,
            "low": close, "close": close, "volume": 10,
            "captured_utc": util.utcnow_iso()})
    book = engine.PriceBook(con)
    decision = "2026-01-14T23:00:00Z"
    p = book.kalshi_price_at(t, decision)
    assert p is not None
    # only candles fully closed by 23:00 qualify: the 22:00 candle closes AT
    # 23:00 (included); the 23:00/23:30 candles close later (excluded)
    assert p.ts_utc == "2026-01-14T23:00:00Z"  # close time of the 22:00 candle
    assert p.price_cents == 64 + 1  # 1-tick slippage, never the later candles
    # decision after all candles -> latest available (23:30 candle closes 00:30)
    p2 = book.kalshi_price_at(t, "2026-01-15T10:00:00Z")
    assert p2.price_cents == 90 + 1
    assert p2.ts_utc == "2026-01-15T00:30:00Z"


def test_team_matching_from_text():
    assert engine.match_teams_in_text("Celtics vs Lakers") == {"BOS", "LAL"}
    assert engine.match_teams_in_text("Nuggets @ Trail Blazers") == {"DEN", "POR"}
    # a single-team mention is never mistaken for a matchup (mapping requires exactly 2)
    assert engine.match_teams_in_text("weather in chicago") == {"CHI"}
    assert len(engine.match_teams_in_text("blizzard hits chicago")) == 1


def test_kalshi_market_mapping_to_game(con):
    _seed_game(con)
    _seed_kalshi(con)
    infos = engine.map_kalshi_markets(con)
    match = [i for i in infos.values() if i.market_type == "winner"]
    assert match and match[0].game_id == "espn:1"
    assert {match[0].home, match[0].away} == {"BOS", "LAL"}


# ---------------------------------------------------------------- audit

def test_audit_flags_duplicate_bets_and_lookahead(con):
    _seed_game(con)
    meta = S.STRATEGIES["NBA-003"]
    for _ in range(2):
        db.insert(con, "bets", {
            "bet_id": engine.make_bet_id("backtest", "NBA-003", "espn:1", "m", "s", "d", "r"),
            "run_id": "r", "kind": "backtest", "strategy_id": "NBA-003",
            "strategy_version": meta["version"], "username": meta["username"],
            "decision_utc": "2026-01-15T05:00:00Z",  # AFTER tipoff -> lookahead flag
            "tipoff_utc": "2026-01-15T01:00:00Z",
            "game_id": "espn:1", "market": "kalshi:winner", "selection": "home",
            "side": "home", "price": 60, "price_format": "kalshi_cents",
            "source": "t", "stake_usd": 10, "execution_status": "simulated_fill",
            "result": "win", "pnl_usd": 5, "verification": "test"})
    db.insert(con, "bets", {
        "bet_id": engine.make_bet_id("backtest", "NBA-003", "espn:1", "m2", "s", "d", "r"),
        "run_id": "r", "kind": "backtest", "strategy_id": "NBA-003",
        "strategy_version": meta["version"], "username": meta["username"],
        "decision_utc": "2026-01-14T20:00:00Z", "tipoff_utc": "2026-01-15T01:00:00Z",
        "game_id": "espn:1", "market": "kalshi:winner", "selection": "home", "side": "home",
        "price": 60, "price_format": "kalshi_cents", "source": "t",
        "stake_usd": 10, "execution_status": "simulated_fill", "result": "win",
        "pnl_usd": 5, "verification": "test"})
    summary = audit.run_checks(con)
    names = [c["check"] for c in summary["checks"]]
    assert "lookahead-decision-after-tipoff" in names
    assert "duplicate-bet" in names


def test_audit_flags_settlement_conflict(con):
    _seed_game(con, hs=120, as_=90)
    db.insert(con, "bets", {
        "bet_id": "b1", "run_id": "r", "kind": "backtest", "strategy_id": "NBA-003",
        "strategy_version": "1", "username": "u", "decision_utc": "2026-01-14T20:00:00Z",
        "game_id": "espn:1", "market": "kalshi:winner", "selection": "home",
        "side": "home", "price": 60, "price_format": "kalshi_cents", "source": "t",
        "stake_usd": 10, "execution_status": "simulated_fill", "result": "loss",
        "pnl_usd": -10, "verification": "test"})
    summary = audit.run_checks(con)
    names = [c["check"] for c in summary["checks"]]
    assert "settlement-vs-score-conflict" in names


# ---------------------------------------------------------------- registry

def test_strategy_registry_complete():
    assert len(S.STRATEGIES) >= 12
    for sid, m in S.STRATEGIES.items():
        assert m["username"] and m["name"] and m["thesis"] and m["entry_rules"]
        assert m["version"].count(".") == 2
        assert m["data_sources"] and m["failure_modes"]
        assert m["lookahead_controls"]


# ---------------------------------------------------------------- sitegen

def test_sitegen_builds_all_pages(con, tmp_path):
    _seed_game(con)
    sitegen.build_all(con, out_dir=str(tmp_path))
    for f in ("index.html", "leaderboard.html", "strategies.html", "upcoming.html",
              "positions.html", "history.html", "sources.html", "research.html",
              "methodology.html", "style.css", "data/history.json"):
        assert (tmp_path / f).exists(), f
    html = (tmp_path / "index.html").read_text()
    for section in ("leaderboard" .title(), "Backtest", "Upcoming"):
        assert section.lower() in html.lower()
    hist = json.loads((tmp_path / "data/history.json").read_text())
    assert isinstance(hist, list)


def test_leaderboard_separates_kinds(con, tmp_path):
    _seed_game(con)
    meta = S.STRATEGIES["NBA-003"]
    db.insert(con, "bets", {
        "bet_id": "bt1", "run_id": "r", "kind": "backtest", "strategy_id": "NBA-003",
        "strategy_version": meta["version"], "username": meta["username"],
        "decision_utc": "2026-01-14T20:00:00Z", "tipoff_utc": "2026-01-15T01:00:00Z",
        "game_id": "espn:1", "market": "kalshi:winner", "selection": "home", "side": "home",
        "price": 60, "price_format": "kalshi_cents", "source": "t", "stake_usd": 10,
        "execution_status": "simulated_fill", "result": "win", "pnl_usd": 6,
        "settlement_utc": "2026-01-15T04:00:00Z", "verification": "test"})
    sitegen.build_all(con, out_dir=str(tmp_path))
    idx = (tmp_path / "index.html").read_text()
    assert "Backtest snapshot" in idx
    assert "forward paper trades" in idx
