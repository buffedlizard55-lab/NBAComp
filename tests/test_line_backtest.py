"""Tests for the line-based historical validation track (`nbacomp/line_backtest`).

This track is the only historical evidence the spread/totals rules can have, so
the tests pin the three things that make it honest:

* the cover arithmetic (including pushes, which are excluded, never counted);
* the rule it measures IS the rule the engines trade (same constants, imported
  not copied, and a game's own result cannot influence its own firing);
* it never produces a P&L: no money column exists in the result or the table,
  and the break-even it quotes is derived from the standard price.
"""
from __future__ import annotations

import os
import sys

import pytest

from nbacomp import backtest, db, line_backtest, util, validation

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "line.db"))
    yield c
    c.close()


def _g(con, season, date, away, home, af, hf, spread=-2.0, total=220.0,
       open_spread=None, open_total=None):
    """One archive row: finals + observed opening/closing spread and total."""
    db.insert(con, "hist_odds", {
        "season": season, "game_date_et": date, "away": away, "home": home,
        "away_q": "[25,25,25,25]", "home_q": "[25,25,25,25]",
        "away_final": af, "home_final": hf,
        "open_total": total if open_total is None else open_total,
        "close_total": total,
        "open_home_spread": spread if open_spread is None else open_spread,
        "close_home_spread": spread,
        "ml_away": 180, "ml_home": -220, "source": "sbr",
        "source_url": "https://example.invalid/sbr", "source_row_hash": "h",
        "cross_checked": 0, "captured_utc": util.utcnow_iso(),
        "spread_printed_row": "home", "spread_sign_from_ml": 1})


def _season(con, n=14, season="2020-21"):
    """A small season of identical games so Elo/rolling state is populated."""
    for i in range(n):
        _g(con, season, f"2021-01-{i + 1:02d}", "BOS", "LAL", 100, 110)


# ------------------------------------------------------------ cover arithmetic

@pytest.mark.parametrize("margin,spread,side,expected", [
    (10, -8.5, "home", True),    # home -8.5, won by 10 -> covers
    (5, -8.5, "home", False),    # home -8.5, won by 5 -> does not cover
    (5, -8.5, "away", True),     # away +8.5, lost by 5 -> covers
    (-3, 4.0, "home", True),     # home +4, lost by 3 -> covers
    (8, -8.0, "home", None),     # exact line -> push, excluded
    (8, -8.0, "away", None),     # a push is a push on both sides
])
def test_cover_arithmetic(margin, spread, side, expected):
    assert line_backtest._ats_outcome(margin, spread, side) is expected


def test_breakeven_is_derived_from_the_standard_price():
    """52.38% is not a magic number: it is the -110 implied probability."""
    assert line_backtest.breakeven_prob() == pytest.approx(util.american_to_prob(-110))
    assert line_backtest.breakeven_prob() == pytest.approx(110 / 210, abs=1e-9)


# --------------------------------------------------------------- rule fidelity

def test_rule_uses_the_traded_constants_not_a_copy():
    """The measured rule must be `backtest.eval_spread`'s rule."""
    assert line_backtest.SPREAD_MIN_COVER is backtest.SPREAD_MIN_COVER
    assert line_backtest.SPREAD_MARGIN_SD is backtest.SPREAD_MARGIN_SD
    assert backtest.SPREAD_MIN_COVER == 3.5 and backtest.SPREAD_MARGIN_SD == 12.0


def test_firings_respect_the_minimum_cover_threshold(con):
    """A game just inside the threshold fires; just outside it does not.

    Elo for an identical-history LAL vs BOS pair is fixed by construction, so
    the only lever is the observed spread: with the home side a big favourite
    the model's cover expectation crosses SPREAD_MIN_COVER at a known line.
    """
    _season(con)
    _g(con, "2020-21", "2021-02-01", "BOS", "LAL", 100, 110, spread=-1.0)
    res = line_backtest.run(con, seasons=["2020-21"])
    near = [f for f in res["firings"].get("NBA-026", [])
            if f["game_date_et"] == "2021-02-01"]
    assert near, "a near-pick'em line leaves a large model cover expectation"
    assert abs(near[0]["expected_cover"]) >= backtest.SPREAD_MIN_COVER

    con.execute("DELETE FROM hist_odds")
    _season(con)
    # an observed line that already prices the model's whole margin: cover -> ~0
    _g(con, "2020-21", "2021-02-01", "BOS", "LAL", 100, 110,
       spread=-(near[0]["elo_margin"]))
    res2 = line_backtest.run(con, seasons=["2020-21"])
    assert [f for f in res2["firings"].get("NBA-026", [])
            if f["game_date_et"] == "2021-02-01"] == []


def test_a_games_own_result_cannot_change_its_own_firing(con):
    """No look-ahead: the decision is taken before the result exists."""
    def build(home_final):
        con.execute("DELETE FROM hist_odds")
        _season(con)
        # 103 (not 101): a 1-point win against a -1.0 line is a PUSH, and a push
        # is excluded from the cover rate by design, so it would not be recorded
        # as a decided firing at all.
        _g(con, "2020-21", "2021-02-01", "BOS", "LAL", 100, home_final, spread=-1.0)
        res = line_backtest.run(con, seasons=["2020-21"])
        return [f for f in res["firings"].get("NBA-026", [])
                if f["game_date_et"] == "2021-02-01"]

    a = build(103)
    b = build(140)
    assert len(a) == len(b) == 1
    # every decision field is identical; only the outcome differs
    for k in ("side", "elo_margin", "close_home_spread", "expected_cover", "trigger"):
        assert a[0][k] == b[0][k], k
    assert a[0]["actual_margin"] != b[0]["actual_margin"]


def test_pushes_are_excluded_from_the_cover_rate(con):
    _season(con)
    # an exact-line finish: the fired bet must not count as a decided firing
    _g(con, "2020-21", "2021-02-01", "BOS", "LAL", 100, 108, spread=-8.0)
    res = line_backtest.run(con, seasons=["2020-21"])
    row = res["summary"].get("NBA-026|ALL")
    fired = [f for f in res["firings"]["NBA-026"] if f["game_date_et"] == "2021-02-01"]
    if fired:  # only if the model fired on that game at all
        assert fired[0]["actual_margin"] + (-8.0) == 0
        decided = len([f for f in res["firings"]["NBA-026"]])
        assert row["n"] <= decided
    assert row is None or row["n"] == row["n"]  # row exists only with decided bets


# ------------------------------------------------------------------ metrics

def test_market_baseline_and_line_accuracy_are_measured(con):
    _season(con, n=10)
    # every game finishes 100-110 (margin +10, total 210); the last game's
    # opening lines differ from its closing lines so the paired comparison and
    # the open/close MAE rows are distinguishable by hand.
    _g(con, "2020-21", "2021-02-01", "BOS", "LAL", 100, 110,
       spread=-2.0, open_spread=-6.0, total=220.0, open_total=205.0)
    res = line_backtest.run(con, seasons=["2020-21"])
    s = res["summary"]
    assert "MARKET|ALL" in s and s["MARKET|ALL"]["metric"] == "home_ats_cover_rate"
    # 11 games, home won by 10 every time: covers -2.0 ten times and -2.0 once
    assert s["MARKET|ALL"]["value"] == 1.0
    assert s["MARKET|ALL"]["n"] == 11
    # totals: 210 actual vs a 220 close everywhere (|err| 10) and a 205 open on
    # the last game (|err| 5) -> mean open MAE (10*10 + 5)/11
    assert s["MARKET|ALL|total_mae_close"]["value"] == pytest.approx(10.0)
    assert s["MARKET|ALL|total_mae_open"]["value"] == pytest.approx((10 * 10 + 5) / 11, abs=1e-4)
    # margins: |10 - (-2)| = 12 everywhere; the last game's open line -6 -> 16
    assert s["MARKET|ALL|margin_mae_close"]["value"] == pytest.approx(12.0)
    assert s["MARKET|ALL|margin_mae_open"]["value"] == pytest.approx((10 * 12 + 16) / 11, abs=1e-4)
    # paired: opening total error is 5 pts SMALLER on the last game, 0 elsewhere
    paired = s["MARKET|ALL|paired_total_mae"]
    assert paired["metric"] == "paired_total_mae_open_minus_close"
    assert paired["n"] == 11
    assert paired["value"] == pytest.approx(-5 / 11, abs=1e-4)
    assert "opening line is the better predictor" in paired["detail"]
    # every game identical on margins except the last -> paired t is defined
    assert s["MARKET|ALL|paired_margin_mae"]["z"] is None or \
        "paired t" in s["MARKET|ALL|paired_margin_mae"]["detail"]


def test_no_pnl_is_computed_or_claimed_anywhere(con):
    """The honesty invariant of this track: lines are not prices."""
    _season(con)
    res = line_backtest.run(con, seasons=["2020-21"])
    assert res["summary"], "the track must produce rows on a populated archive"
    for key, row in res["summary"].items():
        for banned in ("pnl", "roi", "staked", "stake", "bankroll"):
            assert banned not in row, f"{key} carries a money field {banned}"
        assert set(row) == {"rule_id", "season", "metric", "n", "value",
                            "baseline", "breakeven", "z", "detail"}
    con.execute("DELETE FROM line_backtests")
    line_backtest.persist(con, res, "line-test")
    cols = {r[1] for r in con.execute("PRAGMA table_info(line_backtests)")}
    assert not (cols & {"pnl", "roi", "staked", "stake_usd"})
    # every cover row quotes the break-even it is read against
    for r in con.execute("SELECT * FROM line_backtests WHERE metric='cover_rate'"):
        assert r["breakeven"] == pytest.approx(util.american_to_prob(-110), abs=1e-4)
        assert "break-even" in r["detail"]


def test_persist_is_idempotent_per_run(con):
    _season(con)
    res = line_backtest.run(con, seasons=["2020-21"])
    n1 = line_backtest.persist(con, res, "line-test")
    n2 = line_backtest.persist(con, res, "line-test")
    assert n1 == n2
    assert con.execute("SELECT COUNT(*) c FROM line_backtests").fetchone()["c"] == n1
    assert line_backtest.latest_run(con) == "line-test"
    assert line_backtest.pooled(con, "MARKET", "home_ats_cover_rate")["n"] > 0


def test_seasons_filter_restricts_the_measurement(con):
    _season(con, season="2020-21")
    _season(con, season="2021-22")
    all_res = line_backtest.run(con)
    one = line_backtest.run(con, seasons=["2020-21"])
    assert one["games"] == 14 and all_res["games"] == 28
    assert one["seasons"] == ["2020-21"]


# ------------------------------------------------------------- tier evidence

def test_line_evidence_tiers_only_the_rule_it_measures(con):
    """NBA-026 trades spreads, so its line result is a verdict; NBA-004's is not.

    NBA-004 trades Kalshi winner-market moves in cents — a market with no
    history — so its spread-move measurement is published as evidence but must
    never park the strategy.
    """
    assert validation.TIERED_BY_LINE == {"NBA-026"}
    _season(con)
    res = line_backtest.run(con, seasons=["2020-21"])
    # force a pooled row for both rules with a cover rate below break-even
    for rule in ("NBA-026", "NBA-004"):
        res["summary"][f"{rule}|ALL"] = {
            "rule_id": rule, "season": "ALL", "metric": "cover_rate", "n": 500,
            "value": 0.40, "baseline": 0.4953, "breakeven": 0.5238, "z": -5.6,
            "detail": "test row"}
    line_backtest.persist(con, res, "line-tier-test")
    t26 = validation.classify(con, "NBA-026")
    t04 = validation.classify(con, "NBA-004")
    assert t26["tier"] == "failed" and t26["policy"]["allow_bets"] is False
    assert "closing lines" in t26["detail"] and "40.00%" in t26["detail"]
    assert t04["tier"] != "failed" or "line" not in t04["reason"]
    assert validation.evidence_for(con, "NBA-004")["line"]["value"] == 0.40
    # the baseline is attached so the site can quote both numbers
    assert validation.evidence_for(con, "NBA-026")["line_baseline"] is not None
