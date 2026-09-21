"""Tests for the price-based historical simulation (SBR archive).

These pin the two properties that make the simulation trustworthy: a game's own
result can never influence the bet placed on it (no look-ahead), and the money
math is the same arithmetic used everywhere else in the repository.
"""
from __future__ import annotations

import os
import sys

import pytest

from nbacomp import db, hist_backtest, util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "hist.db"))
    yield c
    c.close()


def _hg(con, season, date, away, home, af, hf, ml_a, ml_h, total=220.0):
    db.insert(con, "hist_odds", {
        "season": season, "game_date_et": date, "away": away, "home": home,
        "away_q": "[25,25,25,25]", "home_q": "[25,25,25,25]",
        "away_final": af, "home_final": hf,
        "open_total": total, "close_total": total,
        "open_home_spread": -2.0, "close_home_spread": -2.0,
        "ml_away": ml_a, "ml_home": ml_h, "source": "sbr",
        "source_url": "https://example.invalid/sbr", "source_row_hash": "h",
        "cross_checked": 0, "captured_utc": util.utcnow_iso(),
        "spread_printed_row": "home", "spread_sign_from_ml": 1,
    })


def test_own_result_never_influences_own_bet(con):
    """The single most important property: no look-ahead.

    Two identical seasons are built, differing ONLY in the final score of the
    last game. Every bet placed on earlier games must be byte-identical, and the
    bet on the last game itself must be identical too (it is decided before the
    result exists).
    """
    def build(home_final: int):
        con.execute("DELETE FROM hist_odds")
        # 12 games of identical history so rolling state is populated
        for i in range(12):
            _hg(con, "2020-21", f"2021-01-{i + 1:02d}", "BOS", "LAL", 100, 108, 180, -220)
        _hg(con, "2020-21", "2021-01-20", "BOS", "LAL", 100, home_final, 150, -170)
        return hist_backtest.run(con, seasons=["2020-21"])

    a = build(99)
    bets_a = [b for b in a["bets"] if b["game_date_et"] == "2021-01-20"]
    b = build(120)
    bets_b = [b for b in b["bets"] if b["game_date_et"] == "2021-01-20"]
    assert bets_a and bets_b
    assert [x["side"] for x in bets_a] == [x["side"] for x in bets_b]
    assert [x["model_prob"] for x in bets_a] == [x["model_prob"] for x in bets_b]
    assert [x["edge"] for x in bets_a] == [x["edge"] for x in bets_b]
    # ... while the settlement differs, because the result differs
    assert [x["won"] for x in bets_a] != [x["won"] for x in bets_b]


def test_shrinkage_and_credible_edge_bound_are_applied(con):
    """A rule may only bet when the SHRUNK edge is between MIN_EDGE and 8%."""
    for i in range(10):
        _hg(con, "2020-21", f"2021-02-{i + 1:02d}", "BOS", "LAL", 100, 110, 100, -120)
    res = hist_backtest.run(con, seasons=["2020-21"])
    assert all(hist_backtest.MIN_EDGE - 1e-9 <= b["edge"] <= 0.08 + 1e-9
               for b in res["bets"])
    for b in res["bets"]:
        # calibrated probability is the shrunk one used for the edge
        # both figures are stored rounded to 4dp, so compare at that resolution
        assert b["edge"] == pytest.approx(b["calibrated_prob"] - b["market_prob"], abs=1e-3)


def test_money_math_and_baseline(con):
    """P&L is stake x (decimal-1) on a win at the archive's price, -stake on a loss."""
    _hg(con, "2020-21", "2021-03-01", "BOS", "LAL", 100, 120, 150, -170)
    res = hist_backtest.run(con, seasons=["2020-21"])
    baseline = res["summary"]["MARKET|all"]
    assert baseline["bets"] == 1 and baseline["wins"] == 1
    assert baseline["pnl"] == pytest.approx(
        hist_backtest.FLAT_STAKE * (util.american_to_decimal(-170) - 1.0), abs=0.01)
    assert baseline["roi"] == pytest.approx(baseline["pnl"] / baseline["staked"], abs=1e-3)
    assert hist_backtest.engine_american_pnl(10, -110, "win") == pytest.approx(9.09, abs=0.01)
    assert hist_backtest.engine_american_pnl(10, -110, "loss") == -10.0
    assert hist_backtest.engine_american_pnl(10, -110, "push") == 0.0


def test_persist_rows_are_replaced_not_duplicated(con):
    for i in range(6):
        _hg(con, "2020-21", f"2021-04-{i + 1:02d}", "BOS", "LAL", 100, 108, 180, -220)
    res = hist_backtest.run(con, seasons=["2020-21"])
    n1 = hist_backtest.persist(con, res, "run-a")
    n2 = hist_backtest.persist(con, res, "run-a")
    assert n1 == n2
    assert con.execute("SELECT COUNT(*) c FROM hist_backtests WHERE run_id='run-a'"
                       ).fetchone()["c"] == n1
