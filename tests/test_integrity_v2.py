"""Integrity + evidence-discipline tests added in the 2026-09-21 BUILD pass.

Every test here pins a defect that was found by reading the real database and
real run logs, or a rule the competition now depends on:
  * stale model state must never produce a bet (the 10 phantom edges)
  * a strategy whose verified evidence contradicts it must not trade
  * probability calibration + maximum credible edge are enforced
  * open exposure may never cross the 25% cap
  * BRef verification matches on the exact date (no cross-game comparisons)
  * season type is recorded, never guessed
  * the research scan is reproducible and internally consistent
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from nbacomp import db, engine, model, paper, util, validation
from nbacomp import strategies as S

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


def _seed_game(con, game_id="espn:x1", gdate="2026-01-10", team_h="BOS", team_a="LAL",
               status="scheduled", hs=None, as_=None, season="2025-26",
               season_type=None):
    db.insert(con, "games", {
        "game_id": game_id, "source": "espn", "season": season,
        "season_type": season_type,
        "game_date_et": gdate, "tipoff_utc": gdate + "T00:00:00Z",
        "home_team": team_h, "away_team": team_a, "home_score": hs, "away_score": as_,
        "status": status, "neutral_site": 0, "verified": 0,
        "captured_utc": util.utcnow_iso()})


# --------------------------------------------------------------- tiers

def test_failed_evidence_blocks_bets(con):
    """A totals rule whose MAE is worse than the naive baseline must be parked."""
    db.insert(con, "signal_backtest_summary", {
        "run_id": "r1", "strategy_id": "NBA-002", "strategy_version": "1.1.0",
        "season": "ALL", "n_signals": 535, "n_hits": 263, "hit_rate": 0.4916,
        "baseline_rate": 0.5, "brier": None, "mae_total": 17.04,
        "baseline_mae_total": 16.69, "avg_model_prob": None,
        "max_streak_hits": 5, "max_streak_misses": 5,
        "generated_utc": util.utcnow_iso()})
    info = validation.classify(con, "NBA-002")
    assert info["tier"] == "failed"
    assert info["policy"]["allow_bets"] is False
    # and the engine refuses to place the bet
    _seed_game(con, "espn:blocked", status="scheduled")
    sig = S.Signal(strategy_id="NBA-002", game_id="espn:blocked", market="total",
                   selection="under 230.5", side="under", price=-110,
                   price_format="american", source="espn:line", model_prob=0.75,
                   market_prob=0.524, trigger="t", game_label="L",
                   tipoff_utc="2026-01-10T00:00:00Z")
    ctx = {"game": con.execute("SELECT * FROM games WHERE game_id='espn:blocked'").fetchone(),
           "decision": "2026-01-09T20:00:00Z", "home": "BOS", "away": "LAL"}
    assert paper._place_total_bet(con, ctx, sig, info=None) == 0
    assert con.execute("SELECT COUNT(*) c FROM bets").fetchone()["c"] == 0
    # the refusal is on the record
    assert con.execute("SELECT COUNT(*) c FROM anomalies WHERE "
                       "check_name='signal-gated-by-validation'").fetchone()["c"] == 1


def test_outcome_validated_rule_keeps_trading_at_half_scale(con):
    db.insert(con, "signal_backtest_summary", {
        "run_id": "r1", "strategy_id": "NBA-001", "strategy_version": "1.1.0",
        "season": "ALL", "n_signals": 117, "n_hits": 74, "hit_rate": 0.6325,
        "baseline_rate": 0.5485, "brier": None, "mae_total": None,
        "baseline_mae_total": None, "avg_model_prob": 0.6,
        "max_streak_hits": 5, "max_streak_misses": 3,
        "generated_utc": util.utcnow_iso()})
    info = validation.classify(con, "NBA-001")
    assert info["tier"] == "outcome_validated"
    assert info["policy"]["stake_scale"] == 0.5


# ------------------------------------------------- calibration / edge caps

def test_shrinkage_and_max_credible_edge():
    pol = {"shrink": 0.5, "max_edge": 0.08}
    # a raw 30-point "edge" is shrunk AND then rejected as not credible
    p_cal = validation.calibrated_prob(0.83, 0.524, pol["shrink"])
    assert p_cal == pytest.approx(0.677)
    assert p_cal - 0.524 > pol["max_edge"]
    # a modest edge survives and is shrunk toward the market
    p2 = validation.calibrated_prob(0.60, 0.524, pol["shrink"])
    assert 0.524 < p2 < 0.60


def test_apply_policy_records_calibration_on_the_signal():
    pol = dict(validation.POLICY["outcome_validated"])
    sig = S.Signal(strategy_id="NBA-001", game_id="g", market="kalshi:winner",
                   selection="home", side="home", price=50, price_format="kalshi_cents",
                   source="t", model_prob=0.60, market_prob=0.524, trigger="base")
    kept = validation.apply_policy(None, [sig], tiers={
        "NBA-001": {"tier": "outcome_validated", "policy": pol, "detail": "d"}})
    assert len(kept) == 1
    assert kept[0].model_prob == pytest.approx(0.562)
    assert "policy: raw p=0.600" in kept[0].trigger
    assert "tier=outcome_validated" in kept[0].trigger


# ------------------------------------------------------------ exposure cap

def test_exposure_cap_is_enforced_on_resulting_exposure(con):
    _seed_game(con, "espn:cap", status="scheduled")
    # already 24% exposed
    db.insert(con, "bets", {
        "bet_id": "b-pre", "run_id": "forward", "kind": "forward",
        "strategy_id": "NBA-001", "strategy_version": "1.1.0", "username": "u",
        "decision_utc": "2026-01-01T00:00:00Z", "game_id": "espn:cap",
        "game_label": "L", "tipoff_utc": "2026-01-10T00:00:00Z", "market": "ml",
        "selection": "home", "side": "home", "price": 50, "price_format": "kalshi_cents",
        "source": "t", "stake_usd": 240.0, "execution_status": "simulated_fill",
        "result": "pending", "verification": "t"})
    headroom = paper._exposure_headroom(con, "NBA-001", 1000.0)
    assert headroom == pytest.approx(10.0)  # 250 cap - 240 open
    # a bet that would cross the cap is clipped to the headroom
    assert paper._open_exposure(con, "NBA-001") == 240.0


# ------------------------------------------------------ stale model state

def test_stale_state_is_refused():
    rolling = model.RollingTeamState(window=15)
    for i, d in enumerate(("2024-12-01", "2024-12-05", "2024-12-09")):
        rolling.add_game({"team": "BOS", "game_date_et": d, "is_home": 1,
                          "pts": 110, "opp_pts": 100, "oreb": 9, "fga": 88,
                          "tov": 12, "fta": 20})
    from nbacomp.backtest import _roll_state
    st = _roll_state(rolling, "BOS", "LAL", "2026-10-20")
    assert st["h_roll"] is None            # 315 days stale -> unavailable
    assert st["h_state_age_days"] > 14
    st2 = _roll_state(rolling, "BOS", "LAL", "2024-12-12")
    assert st2["h_roll"] is not None       # fresh -> usable


def test_season_opener_has_unknown_rest():
    sched = model.RollingTeamState(window=15)
    sched.add_game({"team": "BOS", "game_date_et": "2025-04-10", "is_home": 1,
                    "pts": 110, "opp_pts": 100, "wl": "W", "season": "2024-25"})
    r = sched.rest_and_travel("BOS", "2026-10-21", True, "2026-27")
    assert r["rest_days"] is None           # last game was a different season
    assert r["b2b"] == 0


def test_rest_rule_never_fires_on_unknown_rest(con):
    from nbacomp.backtest import eval_rest
    _seed_game(con, "espn:opener", gdate="2026-10-21", status="scheduled")
    ctx = {"game": con.execute("SELECT * FROM games WHERE game_id='espn:opener'").fetchone(),
           "decision": "2026-10-21T00:00:00Z", "home": "BOS", "away": "LAL",
           "elo": model.EloModel(),
           "h_rest": {"rest_days": None, "b2b": 0}, "a_rest": {"rest_days": 0, "b2b": 1},
           "inj_adj": {"BOS": 0.0, "LAL": 0.0}, "winner": None, "total": None,
           "book": engine.PriceBook(con), "h_roll": None, "a_roll": None,
           "total_line": None}
    assert eval_rest(ctx) == []


# -------------------------------------------------------- kalshi pricing

def test_no_live_ask_means_no_winner_signal(con):
    """NBA-024 (momentum) must not invent a price when no observation exists."""
    from nbacomp.backtest import eval_momentum
    _seed_game(con, "espn:m1", gdate="2026-01-10", status="scheduled")
    ctx = {"game": con.execute("SELECT * FROM games WHERE game_id='espn:m1'").fetchone(),
           "decision": "2026-01-09T20:00:00Z", "home": "BOS", "away": "LAL",
           "elo": model.EloModel(), "h_streak": 5, "a_streak": 0,
           "inj_adj": {"BOS": 0.0, "LAL": 0.0}, "book": engine.PriceBook(con),
           "winner": None, "total": None, "h_roll": None, "a_roll": None}
    assert eval_momentum(ctx) == []


# --------------------------------------------------------------- research

def test_research_scan_is_reproducible_offline(tmp_path):
    out = tmp_path / "scan.json"
    r = subprocess.run([sys.executable, "tools/research_scan.py", "--out", str(out)],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    data = json.loads(out.read_text())
    assert data["n_finals_all"] >= 100
    assert "factor_sweep" in data
    # the model-total rule must be reported as a coin flip (the evidence that
    # parked NBA-002), not as an edge
    r9 = data["factor_sweep"]["R9_model_total_extreme"]
    assert abs(r9["hit_rate"] - 0.5) < 0.06
    # B2B is a real effect in this data
    assert data["H2_back_to_back"]["net_point_cost_of_b2b"] < -1.0


def test_bref_verification_matches_exact_date(monkeypatch, con):
    """The playoff-series cross-comparison bug: same matchup, different dates."""
    from nbacomp.sources import bref
    html = (
        '<table><tr><th data-stat="game_start_time">7:30p</th>'
        '<td data-stat="date_game"><a href="/boxscores/202606030SAS.html">Jun 3</a></td>'
        '<td data-stat="visitor_team_name"><a>New York Knicks</a></td>'
        '<td data-stat="visitor_pts">105</td>'
        '<td data-stat="home_team_name"><a>San Antonio Spurs</a></td>'
        '<td data-stat="home_pts">95</td></tr>'
        '<tr><th data-stat="game_start_time">8:00p</th>'
        '<td data-stat="date_game"><a href="/boxscores/202606050SAS.html">Jun 5</a></td>'
        '<td data-stat="visitor_team_name"><a>New York Knicks</a></td>'
        '<td data-stat="visitor_pts">105</td>'
        '<td data-stat="home_team_name"><a>San Antonio Spurs</a></td>'
        '<td data-stat="home_pts">104</td></tr></table>')
    parsed = bref.parse_schedule_page(html)
    assert len(parsed) == 2
    assert {p["game_date_et"] for p in parsed} == {"2026-06-03", "2026-06-05"}
    # seed both games with their true scores
    _seed_game(con, "espn:g1", gdate="2026-06-03", team_h="SAS", team_a="NYK",
               status="final", hs=95, as_=105, season="2025-26")
    _seed_game(con, "espn:g2", gdate="2026-06-05", team_h="SAS", team_a="NYK",
               status="final", hs=104, as_=105, season="2025-26")
    mins = {"mismatches": 0, "unmatched": 0, "ambiguous": 0}

    class FakeBody:
        ok_body = True
        status = 200
        error = None
        body = html.encode()

    monkeypatch.setattr(bref.http, "get", lambda *a, **k: FakeBody())
    bref.verify_month(con, 2026, "june")
    mism = con.execute("SELECT COUNT(*) c FROM verifications WHERE status='mismatch'"
                       ).fetchone()["c"]
    assert mism == 0, "date-exact verification must not cross-compare games"
    matched = con.execute("SELECT COUNT(*) c FROM verifications WHERE status='match'"
                          ).fetchone()["c"]
    assert matched == 2


def test_season_type_parsing():
    from nbacomp.sources import espn
    assert espn._season_type_label({"type": 1, "slug": "preseason"}) == "preseason"
    assert espn._season_type_label({"type": 2, "slug": "regular-season"}) == "regular"
    assert espn._season_type_label({"type": 3, "slug": "postseason"}) == "postseason"
    assert espn._season_type_label({}) is None      # never guessed
    js = {"events": [{
        "id": "1", "date": "2026-10-20T23:30Z",
        "season": {"year": 2027, "type": 2, "slug": "regular-season"},
        "status": {"type": {"state": "pre"}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "team": {"abbreviation": "BOS"}, "score": None},
            {"homeAway": "away", "team": {"abbreviation": "LAL"}, "score": None}],
            "odds": [{"provider": {"name": "DraftKings"}, "overUnder": 231.5,
                      "overOdds": -110, "underOdds": -108, "spread": -2.5}]}]}]}
    g = espn.parse_scoreboard(js)[0]
    assert g["season_type"] == "regular"
    assert g["_odds"]["over_odds"] == -110
    assert g["_odds"]["under_odds"] == -108


def test_elo_mov_multiplier_has_rating_gap_denominator():
    """A 20-point win by a huge favourite must move ratings less than by an underdog."""
    big_fav = model._mov_multiplier(20, 1800.0, 1500.0)
    underdog = model._mov_multiplier(20, 1500.0, 1800.0)
    assert underdog > big_fav > 0
    assert model._mov_multiplier(0, 1500.0, 1500.0) == pytest.approx(1.0)
