"""Live-shape regression tests — pinned to REAL Kalshi/BRef payloads observed
2026-09-20 (see research log). These prove the parsers match the wire, not
guesses. Kalshi fixtures below are byte-faithful excerpts of actual API
responses (irrelevant keys trimmed); BRef fixtures mirror the monthly-page
<table> structure the tolerant parser consumes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import backtest, collect, db, engine, http, util
from nbacomp.sources import bref, kalshi


def test_http_ok_vs_ok_body():
    # .ok requires parseable JSON; HTML pages are never .ok (this silently
    # broke ALL BRef fetching until ok_body existed). Never regress.
    html = http.HttpResult(200, b"<html>x</html>", "mock://bref")
    assert html.ok is False
    assert html.ok_body is True
    js = http.HttpResult(200, b'{"a":1}', "mock://api")
    assert js.ok is True and js.ok_body is True
    err = http.HttpResult(404, b"nope", "mock://x", error="HTTP 404")
    assert err.ok is False and err.ok_body is False

# --- REAL market payload: GET /markets?series_ticker=KXNBAGAME&status=open,
# first row, observed 2026-09-20 (SAS @ OKC opener) -------------------------
SAS_MARKET = {
    "ticker": "KXNBAGAME-26OCT20OKCSAS-SAS",
    "series_ticker": "KXNBAGAME",
    "event_ticker": "KXNBAGAME-26OCT20OKCSAS",
    "title": "San Antonio wins",
    "yes_sub_title": "San Antonio",
    "no_sub_title": "San Antonio",
    "market_type": "binary",
    "strike_type": "structured",
    "custom_strike": {"basketball_team": "ad36c3e8-4194-4e63-920f-7c50f46191a6"},
    "status": "active",
    "close_time": "2026-10-23T01:30:00Z",
    "expected_expiration_time": "2026-10-21T04:30:00Z",
    "yes_bid_dollars": "0.5400",
    "yes_ask_dollars": "0.5500",
    "last_price_dollars": "0.5500",
    "volume_fp": "9924.08",
    "open_interest_fp": "7632.77",
    "result": "",
    "settled_time": None,
}

OKC_MARKET = {
    "ticker": "KXNBAGAME-26OCT20OKCSAS-OKC",
    "series_ticker": "KXNBAGAME",
    "event_ticker": "KXNBAGAME-26OCT20OKCSAS",
    "title": "Oklahoma City wins",
    "yes_sub_title": "Oklahoma City",
    "market_type": "binary",
    "strike_type": "structured",
    "custom_strike": {"basketball_team": "a85f6eca-2f3b-4f61-83ab-8d049f59ce2c"},
    "status": "active",
    "close_time": "2026-10-23T01:30:00Z",
    "expected_expiration_time": "2026-10-21T04:30:00Z",
    "yes_bid_dollars": "0.4600",
    "yes_ask_dollars": "0.4800",
    "last_price_dollars": "0.4600",
    "volume_fp": "3448.76",
    "open_interest_fp": "1850.93",
    "result": "",
    "settled_time": None,
}

# --- REAL orderbook payload: GET /markets/...-SAS/orderbook?depth=10 ------
SAS_BOOK = {"orderbook_fp": {
    "no_dollars": [["0.2500", "80.40"], ["0.2900", "3.40"], ["0.3800", "750.00"],
                   ["0.3900", "3386.00"], ["0.4000", "2500.00"], ["0.4100", "1138.00"],
                   ["0.4200", "669.86"], ["0.4300", "877.00"], ["0.4400", "2671.21"],
                   ["0.4500", "289.24"]],
    "yes_dollars": [["0.2600", "129.00"], ["0.4100", "2120.00"], ["0.4200", "700.00"],
                    ["0.4300", "27.19"], ["0.4900", "3023.68"], ["0.5000", "889.06"],
                    ["0.5100", "1134.00"], ["0.5200", "143.44"], ["0.5300", "100.00"],
                    ["0.5400", "0.17"]]}}

# --- REAL candle row: GET /markets/candlesticks period_interval=1440 ------
PHI_CANDLE = {
    "end_period_ts": 1788235200,
    "open_interest_fp": "8684.37",
    "price": {"close_dollars": "0.3300", "high_dollars": "0.3900",
              "low_dollars": "0.3300", "mean_dollars": "0.3786",
              "open_dollars": "0.3800", "previous_dollars": "0.3900"},
    "volume_fp": "707.69",
    "yes_ask": {"close_dollars": "0.3700", "high_dollars": "0.4000",
                "low_dollars": "0.3700", "open_dollars": "0.3900"},
    "yes_bid": {"close_dollars": "0.3500", "high_dollars": "0.3800",
                "low_dollars": "0.3300", "open_dollars": "0.3600"},
}


def test_parse_market_real_sas_payload():
    row = kalshi.parse_market(SAS_MARKET, "2026-09-20T00:00:00Z")
    assert row["ticker"] == "KXNBAGAME-26OCT20OKCSAS-SAS"
    assert row["market_type"] == "winner"
    assert row["subtitle"] == "San Antonio"
    assert row["yes_bid"] == 54 and row["yes_ask"] == 55
    assert row["last_price"] == 55
    assert row["volume"] == 9924 and row["open_interest"] == 7632
    assert "ad36c3e8" in (row["strike_values"] or "")
    assert "structured" in (row["strike_values"] or "")
    assert row["result"] in (None, "")
    assert row["close_time"] == "2026-10-23T01:30:00Z"


def test_parse_market_real_okc_payload():
    row = kalshi.parse_market(OKC_MARKET, "2026-09-20T00:00:00Z")
    assert row["subtitle"] == "Oklahoma City"
    assert row["yes_bid"] == 46 and row["yes_ask"] == 48
    assert row["volume"] == 3448


def test_get_candlesticks_uses_verified_params(monkeypatch):
    seen = {}

    class FakeResp:
        ok = True
        json = {"markets": [{"market_ticker": "T", "candlesticks": []}]}

    def fake_get(path, params=None):
        seen["path"] = path
        seen["params"] = params
        return FakeResp()

    monkeypatch.setattr(kalshi, "_get", fake_get)
    out = kalshi.get_candlesticks(["T"], 1788220800000, 1789862400000, 60)
    assert seen["params"]["market_tickers"] == "T"
    assert seen["params"]["period_interval"] == 60
    assert seen["params"]["start_ts"] == 1788220800  # ms -> seconds
    assert out and out[0]["market_ticker"] == "T"  # 'markets' key, not 'candlesticks'


def test_candles_window_stores_real_row_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kalshi, "get_candlesticks",
        lambda tickers, s, e, i=60: [{"market_ticker": "T", "candlesticks": [PHI_CANDLE]}])
    with db.get_db(str(tmp_path / "c.db")) as con:
        db.insert(con, "kalshi_markets", {
            "ticker": "T", "series_ticker": "KXNBAGAME", "event_ticker": "E",
            "title": "t", "subtitle": None, "market_type": "winner",
            "strike_values": None, "status": "active",
            "close_time": "2026-10-21T04:30:00Z", "expected_expiration_time": None,
            "yes_bid": None, "yes_ask": None, "last_price": None, "volume": None,
            "open_interest": None, "result": None, "settled_time": None,
            "captured_utc": util.utcnow_iso()}, replace=True)
        n = collect.kalshi_candles_window(con, None, "2026-09-01T00:00:00Z",
                                          "2026-10-22T00:00:00Z", 60)
        assert n == 1
        r = con.execute("SELECT * FROM kalshi_candles").fetchone()
        assert (r["open"], r["high"], r["low"], r["close"]) == (38, 39, 33, 33)
        assert r["volume"] == 707
        # end_period_ts 04:00, interval 60 -> stored candle OPEN 03:00
        assert r["ts_utc"] == "2026-09-01T03:00:00Z"


def test_candles_forward_picks_open_markets(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kalshi, "get_candlesticks",
        lambda tickers, s, e, i=60: [{"market_ticker": t, "candlesticks": [PHI_CANDLE]}
                                     for t in tickers])
    with db.get_db(str(tmp_path / "f.db")) as con:
        for t, status in (("OPEN-T", "active"), ("SHUT-T", "settled")):
            db.insert(con, "kalshi_markets", {
                "ticker": t, "series_ticker": "KXNBAGAME", "event_ticker": "E",
                "title": "t", "subtitle": None, "market_type": "winner",
                "strike_values": None, "status": status,
                "close_time": "2026-10-21T04:30:00Z", "expected_expiration_time": None,
                "yes_bid": None, "yes_ask": None, "last_price": None, "volume": None,
                "open_interest": None, "result": None, "settled_time": None,
                "captured_utc": util.utcnow_iso()}, replace=True)
        n = collect.kalshi_candles_forward(con, days=3)
        assert n == 1
        tickers = [r["ticker"] for r in con.execute("SELECT DISTINCT ticker FROM kalshi_candles")]
        assert tickers == ["OPEN-T"]


def test_norm_side_dollar_strings_and_best_levels():
    yes = collect._norm_side(SAS_BOOK["orderbook_fp"]["yes_dollars"])
    assert yes[0] == [26, 129] and yes[-1] == [54, 0]
    assert collect._norm_side([[54, 3]]) == [[54, 3]]  # legacy ints pass through


def test_snapshot_stores_best_bid_not_first_level(tmp_path, monkeypatch):
    class FakeBook:
        ok = True
        json = SAS_BOOK

    monkeypatch.setattr(kalshi, "get_markets", lambda s, status=None, max_pages=10: [SAS_MARKET])
    monkeypatch.setattr(kalshi, "get_orderbook", lambda t: FakeBook())
    with db.get_db(str(tmp_path / "s.db")) as con:
        import json as _json
        db.insert(con, "meta", {"key": "kalshi_series_discovery",
                                "value": _json.dumps({"KXNBAGAME": {"exists": True}}),
                                "updated_utc": util.utcnow_iso()}, replace=True)
        collect.kalshi_snapshot(con)
        m = con.execute("SELECT * FROM kalshi_markets").fetchone()
        assert m["yes_bid"] == 54 and m["subtitle"] == "San Antonio"
        b = con.execute("SELECT * FROM kalshi_orderbooks").fetchone()
        assert b["yes_bid"] == 54  # best (last) level, not the 26c first level
        assert b["yes_ask"] == 55  # 100 - best no-bid 45


def test_snapshot_fills_series_and_skips_identity_less_rows(tmp_path, monkeypatch):
    no_series = dict(SAS_MARKET)
    no_series["series_ticker"] = None
    no_event = dict(SAS_MARKET)
    no_event["ticker"] = "KXNBAGAME-NOEVENT"
    no_event["event_ticker"] = None

    class FakeBook:
        ok = True
        json = SAS_BOOK

    monkeypatch.setattr(kalshi, "get_markets",
                        lambda s, status=None, max_pages=10: [no_series, no_event])
    monkeypatch.setattr(kalshi, "get_orderbook", lambda t: FakeBook())
    with db.get_db(str(tmp_path / "i.db")) as con:
        import json as _json
        db.insert(con, "meta", {"key": "kalshi_series_discovery",
                                "value": _json.dumps({"KXNBAGAME": {"exists": True}}),
                                "updated_utc": util.utcnow_iso()}, replace=True)
        collect.kalshi_snapshot(con)  # must not raise IntegrityError
        rows = con.execute("SELECT ticker, series_ticker FROM kalshi_markets").fetchall()
        assert [(r["ticker"], r["series_ticker"]) for r in rows] == [
            ("KXNBAGAME-26OCT20OKCSAS-SAS", "KXNBAGAME")]
        anom = con.execute("SELECT COUNT(*) c FROM anomalies WHERE "
                           "check_name='kalshi-market-missing-identity'").fetchone()["c"]
        assert anom == 1


def test_market_team_suffix_text_and_ambiguous():
    assert engine.market_team("KXNBAGAME-26OCT20OKCSAS-SAS", "San Antonio wins",
                              "San Antonio") == "SAS"
    assert engine.market_team("KXNBAGAME-26OCT20OKCSAS-OKC", "Oklahoma City wins",
                              "Oklahoma City") == "OKC"
    # legacy fixture tickers end in a digit -> falls back to text; two teams -> None
    assert engine.market_team("KXNBAGAME-TEST-BOS-0", "Celtics vs Lakers",
                              "Lakers at Celtics") is None
    # single-team text fallback
    assert engine.market_team("ODD", "Oklahoma City wins", "") == "OKC"


def test_store_winner_prefers_home_team_market():
    def info(team, home="SAS", away="OKC"):
        return engine.KalshiMarketInfo(
            ticker=f"T-{team}", event_ticker="E", series_ticker="KXNBAGAME",
            market_type="winner", game_id="g", home=home, away=away,
            team=team, title=f"{team} wins")
    entry: dict = {}
    engine.store_winner(entry, info("OKC"))  # away stored first
    engine.store_winner(entry, info("SAS"))
    assert entry["winner"].team == "SAS"
    assert entry["winner_away"].team == "OKC"
    legacy: dict = {}
    engine.store_winner(legacy, info(None))
    assert legacy["winner"].team is None and "winner_away" not in legacy
    # an away-team market alone must NEVER become the priced leg
    lone: dict = {}
    engine.store_winner(lone, info("OKC"))
    assert "winner" not in lone and lone["winner_away"].team == "OKC"


def test_settlement_is_team_aware(tmp_path):
    def info(team, result):
        return engine.KalshiMarketInfo(
            ticker=f"T-{team}", event_ticker="E", series_ticker="KXNBAGAME",
            market_type="winner", game_id="g", home="SAS", away="OKC",
            team=team, result=result)

    with db.get_db(str(tmp_path / "st.db")) as con:
        home_bet = {"side": "home"}
        # betting the home team on the AWAY team's market = took NO: 'no' wins
        assert engine.apply_settlement_kalshi(con, info("OKC", "no"), home_bet,
                                              None, None) == "win"
        # same bet, market resolves 'yes' -> loss (old code called this a win)
        assert engine.apply_settlement_kalshi(con, info("OKC", "yes"), home_bet,
                                              None, None) == "loss"
        # legacy unknown-team market keeps home=YES behavior
        assert engine.apply_settlement_kalshi(con, info(None, "yes"), home_bet,
                                              None, None) == "win"
        # cross-check: agreeing score raises no anomaly, still wins
        assert engine.apply_settlement_kalshi(con, info("SAS", "yes"), home_bet,
                                              110, 105) == "win"
        n_anom = con.execute("SELECT COUNT(*) c FROM anomalies").fetchone()["c"]
        assert n_anom == 0


def test_pricebook_ask_at_uses_snapshot_history(tmp_path):
    with db.get_db(str(tmp_path / "p.db")) as con:
        for ts, ask in (("2026-09-18T00:00:00Z", 52), ("2026-09-19T00:00:00Z", 55)):
            db.insert(con, "kalshi_orderbooks", {
                "ticker": "T", "captured_utc": ts, "yes_bid": 50,
                "yes_ask": ask, "bids": "[]", "asks": "[]"})
        book = engine.PriceBook(con)
        assert book.kalshi_live_ask("T").price_cents == 55
        assert book.kalshi_ask_at("T", "2026-09-18T12:00:00Z").price_cents == 52
        assert book.kalshi_ask_at("T", "2026-09-17T00:00:00Z") is None


def test_px_live_override_and_backtest_purity():
    live = engine.PricePoint("2026-09-20T00:00:00Z", 55.0, "orderbook_ask")

    class DeadBook:
        def kalshi_price_at(self, *a):
            raise AssertionError("backtest path must not be consulted when live_price is set")

    assert backtest._px({"live_price": live, "book": DeadBook()}) is live
    # ...and without live_price the candle path is used (backtest never sets it)
    assert backtest._px({"book": DeadBook(), "winner": None}) is None


BREF_FIXTURE_HTML = """
<table><tbody>
<tr><th scope="row">Tue, Oct 20, 2026</th>
<td data-stat="visitor_team_name"><a href="/teams/BOS/2027.html">Boston Celtics</a></td>
<td data-stat="visitor_pts">110</td>
<td data-stat="home_team_name"><a href="/teams/DET/2027.html">Detroit Pistons</a></td>
<td data-stat="home_pts">108</td>
<td data-stat="box_score_text"><a href="/boxscores/202610200DET.html">Box Score</a></td></tr>
<tr><th scope="row">Tue, Oct 20, 2026</th>
<td data-stat="visitor_team_name"><a href="/teams/PHI/2027.html">Philadelphia 76ers</a></td>
<td data-stat="visitor_pts"></td>
<td data-stat="home_team_name"><a href="/teams/NYK/2027.html">New York Knicks</a></td>
<td data-stat="home_pts"></td>
<td data-stat="game_start_time">7:30 pm</td></tr>
</tbody></table>"""


def test_bref_schedule_parser_final_and_scheduled_rows():
    rows = bref.parse_schedule_page(BREF_FIXTURE_HTML)
    assert len(rows) == 2
    final, sched = rows
    assert final["status"] == "final" and sched["status"] == "scheduled"
    assert (final["away_team"], final["home_team"]) == ("BOS", "DET")
    assert (final["away_score"], final["home_score"]) == (110, 108)
    assert final["game_date_et"] == "2026-10-20"  # from boxscore URL
    assert (sched["away_team"], sched["home_team"]) == ("PHI", "NYK")
    assert sched["game_date_et"] == "2026-10-20"  # from row header
    assert sched["start_et"] == "7:30p"


def test_bref_url_abbr_normalization():
    # BRef boxscore URLs use CHO/BRK/PHO; our rows use CHA/BKN/PHX.
    # A Phoenix home final must NOT be dropped as a mismatch.
    html = """
<tr><th scope="row">Mon, Jan 5, 2026</th>
<td data-stat="visitor_team_name"><a href="/teams/DAL/2026.html">Dallas Mavericks</a></td>
<td data-stat="visitor_pts">100</td>
<td data-stat="home_team_name"><a href="/teams/PHO/2026.html">Phoenix Suns</a></td>
<td data-stat="home_pts">110</td>
<td data-stat="box_score_text"><a href="/boxscores/202601050PHO.html">Box Score</a></td></tr>"""
    rows = bref.parse_schedule_page(html)
    assert len(rows) == 1
    assert (rows[0]["away_team"], rows[0]["home_team"]) == ("DAL", "PHX")
    assert rows[0]["game_date_et"] == "2026-01-05"


def test_bref_tipoff_conversion_dst_aware():
    assert bref.et_tipoff_to_utc("2026-10-20", "7:30p") == "2026-10-20T23:30:00Z"  # EDT
    assert bref.et_tipoff_to_utc("2026-01-15", "7:30p") == "2026-01-16T00:30:00Z"  # EST
    assert bref.et_tipoff_to_utc("2026-10-20", None) is None
    assert bref.et_tipoff_to_utc("2026-10-20", "") is None


def test_bref_season_and_calendar_year():
    assert bref.season_for(2026, "october") == "2026-27"
    assert bref.season_for(2026, "january") == "2025-26"
    assert bref.calendar_year_for(2027, "october") == 2026
    assert bref.calendar_year_for(2026, "january") == 2026


def test_bref_backfill_month_inserts_and_dedups(tmp_path, monkeypatch):
    from nbacomp import http

    class FakeResp:
        ok = False  # HTML is never .ok (JSON-only property)
        ok_body = True
        status = 200
        body = BREF_FIXTURE_HTML.encode()
        error = None

    monkeypatch.setattr(http, "get", lambda *a, **k: FakeResp())
    with db.get_db(str(tmp_path / "b.db")) as con:
        stats = bref.backfill_month(con, 2027, "october")
        assert stats == {"parsed": 2, "inserted": 2, "merged": 0, "skipped": 0}
        rows = con.execute("SELECT * FROM games ORDER BY game_id").fetchall()
        assert [r["game_id"] for r in rows] == [
            "bref:2026-10-20-BOS-DET", "bref:2026-10-20-PHI-NYK"]
        assert all(r["season"] == "2026-27" for r in rows)
        nyk = rows[1]
        assert nyk["status"] == "scheduled" and nyk["tipoff_utc"] == "2026-10-20T23:30:00Z"
        # an ESPN row for the same matchup wins: backfill must not duplicate it
        db.insert(con, "games", {
            "game_id": "espn:999", "source": "espn", "season": "2026-27",
            "game_date_et": "2026-10-20", "tipoff_utc": "2026-10-20T23:30:00Z",
            "home_team": "NYK", "away_team": "PHI", "home_score": None,
            "away_score": None, "status": "scheduled", "neutral_site": 0,
            "source_updated_utc": None, "captured_utc": util.utcnow_iso(),
            "verified": 0}, replace=True)
        stats2 = bref.backfill_month(con, 2027, "october")
        assert stats2["inserted"] == 1  # BOS-DET bref row refreshed (replace)
        assert stats2["merged"] == 0 and stats2["skipped"] == 1  # PHI-NYK kept ESPN
        n = con.execute("SELECT COUNT(*) c FROM games WHERE game_date_et='2026-10-20' "
                        "AND home_team='NYK'").fetchone()["c"]
        assert n == 2  # the stale bref row + the ESPN row (no third duplicate)
