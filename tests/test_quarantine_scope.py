"""Quarantine-scoping tests (2026-09-22 defect fix).

The defect: `paper.quarantine_bets` measured `team_gamelogs` age for BOTH teams
of EVERY forward bet, so the four NBA-026 spread bets were flagged
`stale-state-at-decision` (critical) even though `backtest.eval_spread` reads
only `ctx["elo"]` and `ctx["spread_line"]`. A critical flag excludes a bet from
exposure and from the competition's ranking P&L, so the misapplied flag removed
four live bets from the competition for a defect their decision did not have.

These tests pin the fix and the correction mechanism:
  * the check is scoped to the rolling state the rule actually declares;
  * the table measured is the table the rule reads (player logs for prop and
    injury rules, which were previously measured against team logs);
  * a misapplied flag is RETRACTED by an appended row, never edited or deleted
    (`bet_flags` stays append-only at the database level);
  * an undeclared strategy is still checked, never silently skipped.
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

from nbacomp import audit, backtest, db, paper, util
from nbacomp import strategies as S

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "scope.db"))
    yield c
    c.close()


def _game(con, game_id="espn:scope1", gdate="2026-10-21", home="HOU", away="DAL",
          season="2026-27"):
    db.insert(con, "games", {
        "game_id": game_id, "source": "espn", "season": season,
        "season_type": "regular", "game_date_et": gdate,
        "tipoff_utc": gdate + "T00:00:00Z", "home_team": home, "away_team": away,
        "home_score": None, "away_score": None, "status": "scheduled",
        "neutral_site": 0, "verified": 0, "captured_utc": util.utcnow_iso()})


def _teamlog(con, team, gdate, season="2025-26"):
    db.insert(con, "team_gamelogs", {
        "game_id": f"gl:{team}:{gdate}", "team": team, "season": season,
        "game_date_et": gdate, "opp": "OPP", "is_home": 1, "pts": 110,
        "opp_pts": 104, "wl": "W", "minutes": 240, "fgm": 40, "fga": 88,
        "fg3m": 12, "fg3a": 34, "ftm": 16, "fta": 20, "oreb": 10, "dreb": 32,
        "reb": 42, "ast": 24, "stl": 8, "blk": 5, "tov": 13, "pf": 19,
        "plus_minus": 6, "source": "espn", "captured_utc": util.utcnow_iso()})


def _playerlog(con, team, player, gdate, season="2025-26"):
    db.insert(con, "player_gamelogs", {
        "season": season, "game_id": f"pg:{team}:{gdate}", "game_date_et": gdate,
        "player_id": f"p:{player}", "player": player, "team": team,
        "status": "started", "minutes": 34.0, "pts": 22, "reb": 7, "ast": 5,
        "stl": 1, "blk": 1, "tov": 2, "fg3m": 2, "fgm": 8, "fga": 18,
        "ftm": 4, "fta": 5, "plus_minus": 4, "source": "espn",
        "captured_utc": util.utcnow_iso()})


def _bet(con, bet_id="fo-scope1", sid="NBA-026", game_id="espn:scope1",
         market="spread", selection="home -8.5", side="home", edge=0.05,
         stake=6.5, verification="verified", price=-110.0):
    db.insert(con, "bets", {
        "bet_id": bet_id, "run_id": "forward", "kind": "forward",
        "strategy_id": sid, "strategy_version": S.STRATEGIES[sid]["version"],
        "username": S.STRATEGIES[sid]["username"],
        "decision_utc": util.utcnow_iso(), "game_id": game_id,
        "game_label": "DAL @ HOU 2026-10-21", "tipoff_utc": "2026-10-21T00:00:00Z",
        "market": market, "selection": selection, "side": side, "price": price,
        "price_format": "american", "source": "test", "source_url": None,
        "source_ts": util.utcnow_iso(), "model_prob": 0.574, "market_prob": 0.524,
        "edge": edge, "stake_usd": stake, "to_win_usd": round(stake * 10 / 11, 2),
        "ev_usd": 0.0, "execution_status": "simulated_fill", "fill_price": price,
        "fee_usd": 0, "contracts": 0, "result": "pending",
        "verification": verification})


def _stale_logs(con, home="HOU", away="DAL"):
    """Box scores from January 2026 for an October 2026 game (>> 14 days)."""
    _teamlog(con, home, "2026-01-05")
    _teamlog(con, away, "2026-01-06")


# ------------------------------------------------------- declaration integrity

def test_every_registered_strategy_declares_its_state_inputs():
    assert S.undeclared_state_inputs() == []
    known = {"team_gamelogs", "player_gamelogs"}
    for sid, tables in S.STATE_INPUTS.items():
        assert sid in S.STRATEGIES, f"{sid} is declared but not registered"
        assert set(tables) <= known, f"{sid} declares unknown tables {tables}"


def test_declaration_matches_what_the_evaluators_actually_read():
    """Derived from source, so the declaration cannot drift from the rules."""
    tokens = ("h_roll", "a_roll", "rolling_history", "_total_setup", "team_rolling")
    for sid, fn in backtest.EVALUATORS.items():
        src = inspect.getsource(fn)
        reads_team_state = any(t in src for t in tokens)
        declared = "team_gamelogs" in S.state_inputs(sid)
        assert reads_team_state == declared, (
            f"{sid}: evaluator reads rolling team state={reads_team_state} but "
            f"declares {S.state_inputs(sid)}")


def test_prop_and_injury_rules_read_player_logs_not_team_logs():
    assert "player_gamelogs" in inspect.getsource(paper._prop_signals)
    for sid in ("NBA-008", "NBA-009", "NBA-014"):
        assert S.state_inputs(sid) == ("player_gamelogs",)
    assert "player_gamelogs" in inspect.getsource(backtest._recent_minutes)
    assert S.state_inputs("NBA-005") == ("player_gamelogs",)


# ------------------------------------------------------------- the fix itself

def test_spread_bet_is_not_quarantined_for_stale_box_state(con):
    """The 2026-09-22 defect: NBA-026 reads no box-score state at all."""
    _game(con)
    _stale_logs(con)
    _bet(con, sid="NBA-026")
    row = dict(con.execute("SELECT b.*, g.home_team, g.away_team, g.game_date_et "
                           "FROM bets b LEFT JOIN games g ON g.game_id=b.game_id").fetchone())
    assert paper._stale_state_evidence(con, row) == []
    assert S.state_inputs("NBA-026") == ()
    counts = paper.quarantine_bets(con, run_id="q1")
    assert "stale-state-at-decision" not in counts
    assert con.execute("SELECT COUNT(*) c FROM bet_flags WHERE severity='critical'"
                       ).fetchone()["c"] == 0
    # the bet stays a live position: exposure and ranking keep it
    assert paper._open_exposure(con, "NBA-026") == pytest.approx(6.5)


def test_totals_bet_on_the_same_stale_state_is_still_quarantined(con):
    """Scoping the check must not weaken it for rules that DO read the state."""
    _game(con)
    _stale_logs(con)
    _bet(con, bet_id="fo-scope2", sid="NBA-002", market="total",
         selection="under 230.5", side="under")
    counts = paper.quarantine_bets(con, run_id="q1")
    assert counts["stale-state-at-decision"] == 1
    flags = con.execute("SELECT flag, severity, detail_json FROM bet_flags").fetchall()
    assert [f["flag"] for f in flags] == ["stale-state-at-decision"]
    assert flags[0]["severity"] == "critical"
    assert '"state_table": "team_gamelogs"' in flags[0]["detail_json"]
    assert paper._open_exposure(con, "NBA-002") == 0.0


def test_prop_bet_is_measured_against_player_logs(con):
    """Prop rules read player_gamelogs; team-log freshness says nothing."""
    _game(con)
    _teamlog(con, "HOU", "2026-10-19", season="2026-27")   # fresh team logs
    _teamlog(con, "DAL", "2026-10-19", season="2026-27")
    _playerlog(con, "HOU", "Someone", "2026-01-04")          # stale player logs
    _bet(con, bet_id="fo-scope3", sid="NBA-008", market="kalshi:prop:rebounds",
         selection="Over 7.5", side="over")
    row = dict(con.execute("SELECT b.*, g.home_team, g.away_team, g.game_date_et "
                           "FROM bets b LEFT JOIN games g ON g.game_id=b.game_id").fetchone())
    ev = paper._stale_state_evidence(con, row)
    assert len(ev) == 1 and ev[0]["state_table"] == "player_gamelogs"
    assert ev[0]["state_age_days"] > 14
    counts = paper.quarantine_bets(con, run_id="q1")
    assert counts["stale-state-at-decision"] == 1


def test_prop_bet_on_fresh_player_logs_is_clean(con):
    """The same rule, one day's difference in the logs it actually reads."""
    _game(con, game_id="espn:scope2", gdate="2026-10-21")
    _teamlog(con, "HOU", "2026-10-19", season="2026-27")
    _teamlog(con, "DAL", "2026-10-19", season="2026-27")
    _playerlog(con, "HOU", "Someone", "2026-10-18", season="2026-27")
    _playerlog(con, "DAL", "Other", "2026-10-18", season="2026-27")
    _bet(con, bet_id="fo-scope3b", sid="NBA-008", game_id="espn:scope2",
         market="kalshi:prop:rebounds", selection="Over 7.5", side="over")
    assert paper.quarantine_bets(con, run_id="q2") == {}
    assert paper._open_exposure(con, "NBA-008") == pytest.approx(6.5)


def test_undeclared_strategy_is_checked_conservatively(con):
    """A new rule cannot escape the check by forgetting its declaration."""
    _game(con)
    _stale_logs(con)
    db.insert(con, "bets", {
        "bet_id": "fo-scope9", "run_id": "forward", "kind": "forward",
        "strategy_id": "NBA-999", "strategy_version": "1.0.0", "username": "x",
        "decision_utc": util.utcnow_iso(), "game_id": "espn:scope1",
        "game_label": "DAL @ HOU", "tipoff_utc": "2026-10-21T00:00:00Z",
        "market": "total", "selection": "under 220.5", "side": "under",
        "price": -110.0, "price_format": "american", "source": "test",
        "source_ts": util.utcnow_iso(), "model_prob": 0.55, "market_prob": 0.524,
        "edge": 0.02, "stake_usd": 5.0, "to_win_usd": 4.5, "ev_usd": 0.0,
        "execution_status": "simulated_fill", "fill_price": -110.0, "fee_usd": 0,
        "contracts": 0, "result": "pending", "verification": "verified"})
    assert "NBA-999" not in S.STATE_INPUTS
    row = dict(con.execute("SELECT b.*, g.home_team, g.away_team, g.game_date_et "
                           "FROM bets b LEFT JOIN games g ON g.game_id=b.game_id").fetchone())
    ev = paper._stale_state_evidence(con, row)
    assert ev and ev[0]["state_table"] == "team_gamelogs"


# ------------------------------------------------------------- retraction path

def test_misapplied_flag_is_retracted_never_deleted(con):
    """The correction: an appended retraction row, original flag still visible."""
    _game(con)
    _stale_logs(con)
    _bet(con, sid="NBA-026")
    # reproduce what the over-broad check wrote before the fix
    assert paper.flag_bet(con, "fo-scope1", "NBA-026", "stale-state-at-decision",
                          "critical", {"team": "HOU", "state_age_days": 289,
                                       "max_state_age_days": 14}, "q-legacy")
    assert paper._open_exposure(con, "NBA-026") == 0.0  # excluded while it stands

    counts = paper.quarantine_bets(con, run_id="q-new")
    assert counts["stale-state-at-decision-retracted"] == 1
    rows = {r["flag"]: r for r in con.execute(
        "SELECT * FROM bet_flags ORDER BY id")}
    assert set(rows) == {"stale-state-at-decision", "stale-state-at-decision-retracted"}
    # the original critical row is untouched and still there (append-only)
    assert rows["stale-state-at-decision"]["severity"] == "critical"
    assert rows["stale-state-at-decision"]["run_id"] == "q-legacy"
    assert rows["stale-state-at-decision-retracted"]["severity"] == "info"
    assert "reads no rolling box-score state" in \
        rows["stale-state-at-decision-retracted"]["detail_json"]
    # and the bet is a live position again
    assert paper._open_exposure(con, "NBA-026") == pytest.approx(6.5)

    # idempotent: a second pass appends nothing
    assert paper.quarantine_bets(con, run_id="q-newer") == {}
    assert con.execute("SELECT COUNT(*) c FROM bet_flags").fetchone()["c"] == 2


def test_a_flag_that_applies_is_never_retracted(con):
    _game(con)
    _stale_logs(con)
    _bet(con, bet_id="fo-scope4", sid="NBA-002", market="total",
         selection="under 230.5", side="under")
    paper.quarantine_bets(con, run_id="q1")
    paper.quarantine_bets(con, run_id="q2")
    flags = [r["flag"] for r in con.execute("SELECT flag FROM bet_flags")]
    assert flags == ["stale-state-at-decision"]
    assert paper._open_exposure(con, "NBA-002") == 0.0


def test_bet_flags_stays_append_only(con):
    """The retraction must not weaken the table's guarantees."""
    _game(con)
    _bet(con, sid="NBA-026")
    paper.flag_bet(con, "fo-scope1", "NBA-026", "stale-state-at-decision",
                   "critical", {"team": "HOU"}, "q-legacy")
    with pytest.raises(Exception):
        con.execute("UPDATE bet_flags SET severity='info'")
    with pytest.raises(Exception):
        con.execute("DELETE FROM bet_flags")


def test_price_not_observed_names_the_market_it_priced(con):
    """The flag detail used to say 'totals' on spread bets."""
    _game(con)
    _bet(con, sid="NBA-026", verification="PRICED-ASSUMPTION: -110")
    paper.quarantine_bets(con, run_id="q1")
    row = con.execute("SELECT detail_json FROM bet_flags "
                      "WHERE flag='price-not-observed'").fetchone()
    assert row is not None
    assert "spread" in row["detail_json"] and "totals" not in row["detail_json"]


# ------------------------------------------------------------------- the audit

def test_audit_stale_check_is_scoped_the_same_way(con):
    _game(con)
    _stale_logs(con)
    _bet(con, sid="NBA-026")
    summary = audit.run_checks(con)
    checks = {c["check"] for c in summary["checks"]}
    assert "stale-model-state-at-decision" not in checks

    _bet(con, bet_id="fo-scope5", sid="NBA-002", market="total",
         selection="under 230.5", side="under")
    summary2 = audit.run_checks(con)
    stale = [c for c in summary2["checks"]
             if c["check"] == "stale-model-state-at-decision"]
    assert stale and stale[0]["severity"] == "critical"
    assert stale[0]["detail"]["examples"][0]["state_table"] == "team_gamelogs"
