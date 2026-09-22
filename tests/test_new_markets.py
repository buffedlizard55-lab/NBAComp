"""Pass-3 coverage: NBA-025/026/027 rules, settlement, no look-ahead."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import backtest, db, engine, hist_backtest, paper, strategies as S, util
from nbacomp.model import EloModel


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "n.db"))
    yield c
    c.close()


def test_registry_has_new_families():
    assert "NBA-025" in S.STRATEGIES
    assert "NBA-026" in S.STRATEGIES
    assert "NBA-027" in S.STRATEGIES
    assert S.STRATEGIES["NBA-025"]["username"] == "LongshotLarry"
    assert all(S.STRATEGIES[s]["hypothesis"] for s in ("NBA-025", "NBA-026", "NBA-027"))
    usernames = [m["username"] for m in S.STRATEGIES.values()]
    assert len(usernames) == len(set(usernames))


def test_longshot_hist_rule_fires_on_plus_200(con):
    db.insert(con, "hist_odds", {
        "season": "2020-21", "game_date_et": "2021-01-01", "away": "BOS", "home": "LAL",
        "away_q": "[25,25,25,25]", "home_q": "[25,25,25,25]",
        "away_final": 90, "home_final": 120,
        "open_total": 220, "close_total": 220,
        "open_home_spread": -8.0, "close_home_spread": -8.0,
        "ml_away": 250, "ml_home": -300, "source": "sbr",
        "source_url": "https://example.invalid/sbr", "source_row_hash": "h",
        "cross_checked": 0, "captured_utc": util.utcnow_iso(),
        "spread_printed_row": "home", "spread_sign_from_ml": 1,
    })
    res = hist_backtest.run(con, seasons=["2020-21"])
    sids = {b["strategy_id"] for b in res["bets"]}
    # May or may not clear the shrunk-edge gate; the rule must at least be evaluated.
    rules = hist_backtest._rules(hist_backtest._State(), {
        "home": "LAL", "away": "BOS", "game_date_et": "2021-01-01",
        "ml_home": -300, "ml_away": 250})
    assert any(r["sid"] == "NBA-025" and r["side"] == "home" for r in rules)


def test_spread_settlement_home_cover():
    # home 110-100 vs -4: covers
    assert engine.settle_score_based("spread", 110, 100, "home -4", -4) == "win"
    assert engine.settle_score_based("spread", 103, 100, "home -4", -4) == "loss"
    assert engine.settle_score_based("spread", 104, 100, "home -4", -4) == "push"


def test_team_total_settle_path(con):
    db.insert(con, "games", {
        "game_id": "g1", "source": "espn", "season": "2025-26",
        "game_date_et": "2026-01-14", "tipoff_utc": "2026-01-15T01:00:00Z",
        "home_team": "BOS", "away_team": "LAL", "home_score": 118, "away_score": 110,
        "status": "final", "neutral_site": 0, "source_updated_utc": None,
        "captured_utc": util.utcnow_iso(), "verified": 1}, replace=True)
    b = {"bet_id": "x", "strategy_id": "NBA-027", "market": "team_total",
         "selection": "home over 110.0", "side": "over", "strike": 110.0,
         "price": -110, "stake_usd": 10, "model_prob": 0.6}
    r, src = paper._settle_one(con, b, {}, {}, dict(con.execute(
        "SELECT * FROM games WHERE game_id='g1'").fetchone()), False)
    assert r == "win"
    b2 = dict(b); b2["side"] = "under"; b2["selection"] = "home under 110.0"
    r2, _ = paper._settle_one(con, b2, {}, {}, dict(con.execute(
        "SELECT * FROM games WHERE game_id='g1'").fetchone()), False)
    assert r2 == "loss"


def test_eval_spread_needs_line():
    elo = EloModel()
    ctx = {"elo": elo, "home": "BOS", "away": "LAL", "spread_line": None,
           "game": {"game_id": "g", "game_date_et": "2026-01-01", "tipoff_utc": None}}
    assert backtest.eval_spread(ctx) == []
    ctx["spread_line"] = -12.0
    # default Elo is 1500/1500 so margin is home-adv only (~2-3 pts) vs -12
    # cover = margin + (-12) is largely negative -> away side
    sigs = backtest.eval_spread(ctx)
    assert sigs
    assert sigs[0].strategy_id == "NBA-026"
    assert sigs[0].market == "spread"
    assert sigs[0].selection.startswith("away") or sigs[0].selection.startswith("home")


def test_eval_team_total_needs_both_lines():
    ctx = {"elo": EloModel(), "home": "BOS", "away": "LAL",
           "total_line": 220, "spread_line": None,
           "h_roll": {"pts": 130},
           "game": {"game_id": "g", "game_date_et": "2026-01-01", "tipoff_utc": None}}
    assert backtest.eval_team_total(ctx) == []
    ctx["spread_line"] = -6.0  # implied TT = (220 - -6)/2 = 113
    sigs = backtest.eval_team_total(ctx)
    assert sigs and sigs[0].side == "over"
    assert sigs[0].market == "team_total"


def test_bet_shape_accepts_team_total():
    ok, why = engine.bet_shape({
        "market": "team_total", "side": "over", "selection": "home over 110",
        "price": -110, "model_prob": 0.6, "stake_usd": 10})
    assert ok, why
