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
    # the 23:00 candle is NOT strictly before the decision -> 22:00 candle used
    assert p.ts_utc == "2026-01-14T22:00:00Z"
    assert p.price_cents == 64 + 1  # 1-tick slippage, never the 23:30 candle
    # decision after all candles -> latest available (23:30 close = 90)
    p2 = book.kalshi_price_at(t, "2026-01-15T10:00:00Z")
    assert p2.price_cents == 90 + 1


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
