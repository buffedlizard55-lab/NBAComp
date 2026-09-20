import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from nbacomp import db, util
from nbacomp.sources import espn


SCOREBOARD_FIXTURE = {
    "events": [{
        "id": "401700001",
        "date": "2026-01-15T00:30Z",
        "season": {"year": 2026, "type": 2},
        "status": {"type": {"state": "post"}},
        "competitions": [{
            "neutral": False,
            "competitors": [
                {"homeAway": "home", "id": "2", "team": {"abbreviation": "BOS",
                                                          "displayName": "Boston Celtics"},
                 "score": "112", "winner": True},
                {"homeAway": "away", "id": "13", "team": {"abbreviation": "LAL",
                                                           "displayName": "Los Angeles Lakers"},
                 "score": "105", "winner": False},
            ],
            "odds": [{
                "provider": {"name": "ESPN BET"},
                "details": "BOS -6.5",
                "overUnder": 221.5,
                "spread": -6.5,
                "homeTeamOdds": {"moneyLine": -260},
                "awayTeamOdds": {"moneyLine": 215},
                "open": {"overUnder": 219.5, "spread": -5.5},
            }],
        }],
    }]
}


def test_parse_scoreboard_full():
    games = espn.parse_scoreboard(SCOREBOARD_FIXTURE)
    assert len(games) == 1
    g = games[0]
    assert g["game_id"] == "espn:401700001"
    assert g["home_team"] == "BOS" and g["away_team"] == "LAL"
    assert g["home_score"] == 112 and g["away_score"] == 105
    assert g["status"] == "final"
    # 2026-01-15T00:30Z is Jan 14 in ET
    assert g["game_date_et"] == "2026-01-14"
    odds = g["_odds"]
    assert odds["total"] == 221.5
    assert odds["ml_home"] == -260 and odds["ml_away"] == 215
    assert odds["open_total"] == 219.5


def test_parse_injuries():
    js = {"items": [{
        "team": {"abbreviation": "BOS"},
        "injuries": [{
            "athlete": {"id": "1", "displayName": "Jayson Tatum"},
            "status": {"name": "Out", "date": "2026-01-15T18:00Z"},
            "longComment": "Ankle",
        }],
    }]}
    rows = espn.parse_injuries(js)
    assert rows[0]["player"] == "Jayson Tatum"
    assert rows[0]["status"] == "Out"
    assert rows[0]["team"] == "BOS"


def test_collect_espn_day_stores_games_and_odds(tmp_path, monkeypatch):
    from nbacomp import collect
    class FakeResp:
        ok = True
        status = 200
        json = SCOREBOARD_FIXTURE
        error = None

    monkeypatch.setattr(espn, "scoreboard", lambda date=None: FakeResp())

    class FakeVerify:
        ok = False
        status = 0
        error = "unreachable"
        json = None

    monkeypatch.setattr(collect.nba, "scoreboard_v2", lambda d: FakeVerify())

    with db.get_db(str(tmp_path / "c.db")) as con:
        n = collect.collect_espn_day(con, "20260115", verify=True)
        assert n == 1
        g = con.execute("SELECT * FROM games").fetchone()
        assert g["home_team"] == "BOS"
        odds = con.execute("SELECT COUNT(*) c FROM odds_snapshots").fetchone()["c"]
        assert odds >= 4  # total, spread, 2 MLs (+ opening rows)


def test_backtest_decision_always_before_tipoff(tmp_path):
    """Structural look-ahead test: run the engine on a tiny synthetic universe
    and assert every bet row satisfies decision_utc < tipoff_utc and that the
    price source timestamp <= decision timestamp."""
    from nbacomp import backtest, engine

    with db.get_db(str(tmp_path / "bt.db")) as con:
        # two teams, five games, alternating winners
        dates = ["2026-01-05", "2026-01-07", "2026-01-09", "2026-01-11", "2026-01-13"]
        for i, d in enumerate(dates):
            db.insert(con, "games", {
                "game_id": f"espn:{i}", "source": "espn", "season": "2025-26",
                "game_date_et": d, "tipoff_utc": f"{d}T23:30:00Z",
                "home_team": "BOS" if i % 2 == 0 else "LAL",
                "away_team": "LAL" if i % 2 == 0 else "BOS",
                "home_score": 110 + (i % 3), "away_score": 100 + (i % 2),
                "status": "final", "neutral_site": 0, "source_updated_utc": None,
                "captured_utc": util.utcnow_iso(), "verified": 1}, replace=True)
            for team in ("BOS", "LAL"):
                db.insert(con, "team_gamelogs", {
                    "season": "2025-26", "game_id": f"espn:{i}", "game_date_et": d,
                    "team": team, "opp": "x", "is_home": 0, "pts": 100, "opp_pts": 100,
                    "wl": "W", "minutes": 240, "fgm": 40, "fga": 90, "fg3m": 12,
                    "fg3a": 35, "ftm": 15, "fta": 20, "oreb": 10, "dreb": 30,
                    "reb": 40, "ast": 25, "stl": 7, "blk": 5, "tov": 13, "pf": 20,
                    "plus_minus": 0, "source": "fixture", "captured_utc": util.utcnow_iso()},
                    replace=True)
        ticker = "KXNBAGAME-TEST-BOS"
        for i, d in enumerate(dates):
            db.insert(con, "kalshi_markets", {
                "ticker": f"{ticker}-{i}", "series_ticker": "KXNBAGAME",
                "event_ticker": f"EV-{i}", "title": "Celtics vs Lakers",
                "subtitle": "Lakers at Celtics", "market_type": "winner",
                "strike_values": None, "status": "settled", "close_time": f"{d}T03:00:00Z",
                "expected_expiration_time": None, "yes_bid": None, "yes_ask": None,
                "last_price": 100, "volume": 100, "open_interest": 10, "result": "yes",
                "settled_time": f"{d}T03:10:00Z", "captured_utc": util.utcnow_iso()}, replace=True)
            # candles up to 4 hours before tipoff
            for h in range(20, 23):
                ts = f"{d}T{h:02d}:00:00Z"
                db.insert(con, "kalshi_candles", {
                    "ticker": f"{ticker}-{i}", "interval": 60, "ts_utc": ts,
                    "open": 55, "high": 55, "low": 55, "close": 55 + (h - 20),
                    "volume": 5, "captured_utc": util.utcnow_iso()})

        res = backtest.run_backtest(con, ["2025-26"], run_id="test")
        rows = con.execute("SELECT * FROM bets").fetchall()
        for b in rows:
            assert b["decision_utc"] < b["tipoff_utc"]
            if b["source_ts"]:
                assert b["source_ts"] <= b["decision_utc"]
            assert b["stake_usd"] > 0


def test_pricebook_around_window():
    # kalshi_price_around picks nearest candle within window
    class FakeCon:
        pass

    import nbacomp.engine as eng
    book = eng.PriceBook.__new__(eng.PriceBook)
    book._candles = {"T": [("2026-01-13T00:00:00Z", 50.0),
                           ("2026-01-15T00:00:00Z", 70.0)]}
    book._books = {}
    p = book.kalshi_price_around("T", "2026-01-13T02:00:00Z", window_hours=12)
    assert p.price_cents == 51.0
    p2 = book.kalshi_price_around("T", "2026-01-16T00:00:00Z", window_hours=4)
    assert p2 is None
