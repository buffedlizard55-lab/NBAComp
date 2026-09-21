"""ADVERSARIAL-pass tests (2026-09-21).

These tests try to break the system rather than confirm it: they feed the
engines defective state, missing data, duplicate writes, future timestamps and
hand-computed money, and assert that the system refuses, flags or books exactly
the right number. A test here failing is a defect report, not a style opinion.

Covered: quarantine of bets placed on invalid state, append-only guarantees for
the new bet_flags table, exposure accounting, settlement without a verified
source, no-look-ahead pricing, money math against hand-computed values,
timezone/ET-date handling, duplicate suppression, and strategy metadata
completeness.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

from nbacomp import db, engine, model, paper, sitegen, util, validation
from nbacomp import strategies as S

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "adv.db"))
    yield c
    c.close()


def _game(con, game_id="espn:adv1", gdate="2026-01-10", home="BOS", away="LAL",
          status="scheduled", hs=None, as_=None, season="2025-26",
          season_type="regular", tipoff=None):
    db.insert(con, "games", {
        "game_id": game_id, "source": "espn", "season": season,
        "season_type": season_type, "game_date_et": gdate,
        "tipoff_utc": tipoff or (gdate + "T00:00:00Z"),
        "home_team": home, "away_team": away, "home_score": hs, "away_score": as_,
        "status": status, "neutral_site": 0, "verified": 0,
        "captured_utc": util.utcnow_iso()})


def _box(con, team, gdate, season="2025-26", pts=110, opp=104):
    db.insert(con, "team_gamelogs", {
        "game_id": f"gl:{team}:{gdate}", "team": team, "season": season,
        "game_date_et": gdate, "opp": "OPP", "is_home": 1, "pts": pts,
        "opp_pts": opp, "wl": "W" if pts > opp else "L", "minutes": 240,
        "fgm": 40, "fga": 88, "fg3m": 12, "fg3a": 34, "ftm": 16, "fta": 20,
        "oreb": 10, "dreb": 32, "reb": 42, "ast": 24, "stl": 8, "blk": 5,
        "tov": 13, "pf": 19, "plus_minus": pts - opp, "source": "espn",
        "captured_utc": util.utcnow_iso()})


def _bet(con, bet_id="fo-adv1", sid="NBA-002", game_id="espn:adv1", edge=0.05,
         stake=10.0, result="pending", price=-110.0, verification="verified",
         market="total", selection="under 220.5", model_prob=0.55, kind="forward",
         side="under"):
    db.insert(con, "bets", {
        "bet_id": bet_id, "run_id": "forward", "kind": kind, "strategy_id": sid,
        "strategy_version": "1.1.0", "username": S.STRATEGIES[sid]["username"],
        "decision_utc": util.utcnow_iso(), "game_id": game_id,
        "game_label": "LAL @ BOS", "tipoff_utc": "2026-01-11T00:00:00Z",
        "market": market, "selection": selection, "side": side,
        "price": price, "price_format": "american", "source": "test",
        "source_url": None, "source_ts": util.utcnow_iso(),
        "model_prob": model_prob, "market_prob": 0.5238, "edge": edge,
        "stake_usd": stake, "to_win_usd": round(stake * 10 / 11, 2),
        "ev_usd": 0.0, "execution_status": "simulated_fill", "fill_price": price,
        "fee_usd": 0, "contracts": 0, "result": result, "verification": verification})


# ---------------------------------------------------------------- quarantine

def test_quarantine_flags_stale_state_and_excludes_from_exposure(con):
    """A bet priced off months-old team state is flagged and un-exposed."""
    _game(con, "espn:adv1", "2026-10-20")
    _box(con, "BOS", "2025-01-05")  # 288 days stale
    _box(con, "LAL", "2025-01-06")
    _bet(con, edge=0.30)
    assert paper._open_exposure(con, "NBA-002") == pytest.approx(10.0)
    counts = paper.quarantine_bets(con, run_id="q1")
    assert counts["stale-state-at-decision"] == 1
    assert counts["implausible-edge"] == 1  # 30% > IMPLAUSIBLE_EDGE
    # critical flags remove the position from exposure
    assert paper._open_exposure(con, "NBA-002") == 0.0
    flags = con.execute("SELECT flag, severity FROM bet_flags ORDER BY flag").fetchall()
    assert ("stale-state-at-decision", "critical") in [(f["flag"], f["severity"]) for f in flags]
    assert ("price-not-observed", "info") not in [(f["flag"], f["severity"]) for f in flags]


def test_quarantine_is_idempotent_and_never_rewrites_flags(con):
    _game(con, "espn:adv1", "2026-10-20")
    _box(con, "BOS", "2025-01-05")
    _box(con, "LAL", "2025-01-06")
    _bet(con, edge=0.03)
    first = paper.quarantine_bets(con, run_id="q1")
    n_flags = con.execute("SELECT COUNT(*) c FROM bet_flags").fetchone()["c"]
    n_anom = con.execute("SELECT COUNT(*) c FROM anomalies").fetchone()["c"]
    second = paper.quarantine_bets(con, run_id="q2")
    assert second == {} and first  # nothing new the second time
    assert con.execute("SELECT COUNT(*) c FROM bet_flags").fetchone()["c"] == n_flags
    assert con.execute("SELECT COUNT(*) c FROM anomalies").fetchone()["c"] == n_anom


def test_quarantine_table_is_append_only(con):
    _bet(con)
    paper.flag_bet(con, "fo-adv1", "NBA-002", "unit-test", "info", {"x": 1})
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE bet_flags SET severity='info' WHERE flag='unit-test'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM bet_flags WHERE flag='unit-test'")


def _seed_failed_evidence(con, sid="NBA-021"):
    db.insert(con, "signal_backtest_summary", {
        "run_id": "adv-run", "strategy_id": sid, "strategy_version": "1.1.0",
        "season": "ALL", "n_signals": 900, "n_hits": 300, "hit_rate": 0.333,
        "baseline_rate": 0.499, "brier": None, "mae_total": None,
        "baseline_mae_total": None, "avg_model_prob": None,
        "max_streak_hits": 3, "max_streak_misses": 6,
        "generated_utc": util.utcnow_iso()})


def test_parked_strategy_bets_are_disclosed_but_not_silently_dropped(con):
    _seed_failed_evidence(con, "NBA-021")
    _bet(con, sid="NBA-021", edge=0.02, verification="verified")
    counts = paper.quarantine_bets(con)
    assert counts.get("strategy-parked") == 1
    # warn-level disclosure does not remove the position from exposure
    assert paper._open_exposure(con, "NBA-021") == pytest.approx(10.0)
    row = con.execute("SELECT result, stake_usd FROM bets WHERE bet_id='fo-adv1'").fetchone()
    assert row["result"] == "pending" and row["stake_usd"] == 10.0


def test_assumed_price_is_flagged_and_not_ranked(con):
    _bet(con, verification="PRICED-ASSUMPTION: no free historical totals price source")
    counts = paper.quarantine_bets(con)
    assert counts.get("price-not-observed") == 1
    flags = con.execute("SELECT severity FROM bet_flags WHERE bet_id='fo-adv1'").fetchall()
    assert [f["severity"] for f in flags] == ["info"]


def test_settled_quarantined_bet_keeps_its_own_pnl_but_not_the_ranking(con):
    _game(con, "espn:adv1", "2026-10-20", status="final", hs=100, as_=95)
    _box(con, "BOS", "2025-01-05")
    _box(con, "LAL", "2025-01-06")
    _bet(con, edge=0.30, result="loss")
    con.execute("UPDATE bets SET pnl_usd=-10.0, roi=-1.0, settlement_utc=?, "
                "settlement_source='test' WHERE bet_id='fo-adv1'", (util.utcnow_iso(),))
    paper.quarantine_bets(con)
    perf = sitegen.strategy_performance(con, "NBA-002", "forward")
    assert perf["bets"] == 0                      # not a strategy measurement
    assert perf["quarantined"] == 1
    assert perf["quarantine_pnl"] == -10.0        # still published
    qhtml = sitegen.quarantine_section_html(con)
    assert "fo-adv1" in qhtml and "-10.00" in qhtml


# ------------------------------------------------------------ money / math

def test_american_pnl_matches_hand_computation():
    # -110 stake 55 -> win pays 50.00, loss loses 55.00, push returns 0
    assert engine.american_pnl(55.0, -110, "win") == pytest.approx(50.0, abs=0.01)
    assert engine.american_pnl(55.0, -110, "loss") == pytest.approx(-55.0, abs=0.01)
    assert engine.american_pnl(55.0, -110, "push") == 0.0
    assert engine.american_pnl(55.0, -110, "void") == 0.0
    # +150 stake 20 -> win pays 30.00
    assert engine.american_pnl(20.0, 150, "win") == pytest.approx(30.0, abs=0.01)
    # probabilities implied by those prices
    assert util.american_to_prob(-110) == pytest.approx(0.5238, abs=1e-4)
    assert util.american_to_prob(150) == pytest.approx(0.4, abs=1e-6)


def test_kelly_never_exceeds_configured_caps():
    stake = S.stake_for(bankroll=1000.0, model_prob=0.99,
                        decimal_odds=util.american_to_decimal(-110))
    assert stake <= S.STARTING_BANKROLL * S.MAX_STAKE_PCT + 1e-9
    assert stake >= S.MIN_STAKE
    # no edge -> below minimum stake (no bet)
    assert S.stake_for(bankroll=1000.0, model_prob=0.40,
                       decimal_odds=util.american_to_decimal(-110)) < S.MIN_STAKE


def test_drawdown_and_roi_match_hand_computation(con):
    _game(con)
    pnls = [12.0, -20.0, -15.0, 30.0]
    for i, p in enumerate(pnls):
        _bet(con, bet_id=f"fo-dd{i}", stake=20.0, result="win" if p > 0 else "loss")
        con.execute("UPDATE bets SET pnl_usd=?, settlement_utc=? WHERE bet_id=?",
                    (p, f"2026-01-1{i + 1}T00:00:00Z", f"fo-dd{i}"))
    perf = sitegen.strategy_performance(con, "NBA-002", "forward")
    assert perf["pnl"] == pytest.approx(7.0)
    assert perf["staked"] == pytest.approx(80.0)
    assert perf["roi"] == pytest.approx(7.0 / 80.0)
    assert perf["max_dd"] == pytest.approx(35.0)  # 12 -> -8 -> -23 -> +7 peak-to-trough
    assert perf["largest_win"] == pytest.approx(30.0)
    assert perf["largest_loss"] == pytest.approx(-20.0)


# -------------------------------------------------------- settlement safety

def test_settlement_requires_a_final_game(con):
    """A scheduled (not final) game must never settle a bet."""
    _game(con, "espn:adv1", "2026-01-10", status="scheduled", hs=99, as_=98)
    _bet(con)
    n = paper.settle_finished(con)
    assert n == 0
    assert con.execute("SELECT result FROM bets WHERE bet_id='fo-adv1'").fetchone()["result"] == "pending"


def test_settlement_voids_without_a_verified_source(con):
    """No box score and no market result 48h on -> void, never a guessed result."""
    _game(con, "espn:adv1", "2026-01-10", status="final", hs=None, as_=None,
          tipoff="2026-01-01T00:00:00Z")
    _bet(con, market="kalshi:winner", selection="BOS", price=55.0, side="home")
    paper.settle_finished(con)
    row = con.execute("SELECT result FROM bets WHERE bet_id='fo-adv1'").fetchone()
    assert row["result"] in ("void", "pending")


def test_impossible_bet_shape_is_refused_not_booked_as_a_loss(con):
    """Adversarial: a winner bet carrying side='under' must never settle as win/loss."""
    _game(con, "espn:adv1", "2026-01-10", status="final", hs=100, as_=95,
          tipoff="2026-01-01T00:00:00Z")
    _bet(con, market="kalshi:winner", selection="BOS", price=55.0)  # side='under': impossible
    paper.settle_finished(con)
    row = con.execute("SELECT result FROM bets WHERE bet_id='fo-adv1'").fetchone()
    assert row["result"] == "pending", "impossible row must never be booked as a result"
    flag = con.execute("SELECT flag, severity FROM bet_flags WHERE bet_id='fo-adv1'").fetchone()
    assert flag["flag"] == "invalid-bet-shape" and flag["severity"] == "critical"
    assert engine.bet_shape({"market": "kalshi:winner", "side": "under",
                             "selection": "BOS", "price": 55, "model_prob": 0.5,
                             "stake_usd": 10})[0] is False
    assert engine.settle_score_based("winner", 100, 95, "under", None) == "void"


def test_no_lookahead_in_total_price_selection(con):
    """A price timestamped after the decision can never be selected."""
    _game(con, "espn:adv1", "2026-01-10")
    db.insert(con, "odds_snapshots", {
        "game_id": "espn:adv1", "market": "total", "selection": "under 220.5",
        "line": 220.5, "price": -108, "price_format": "american",
        "source": "espn", "source_url": None,
        "source_updated_utc": "2026-01-10T18:00:00Z",
        "captured_utc": "2026-01-10T18:00:00Z"})
    ctx = {"game": {"game_id": "espn:adv1"}, "decision": "2026-01-10T12:00:00Z"}

    class _Sig:
        market = "total"
        selection = "under 220.5"
        side = "under"
        model_prob = 0.55
    price, real = paper._total_price_for(con, ctx, _Sig())
    assert (price, real) == (-110, False), "future price leaked into the decision"
    # ... and a price captured BEFORE the decision is used (proving the check
    # is not simply always falling back)
    con.execute("UPDATE odds_snapshots SET captured_utc='2026-01-10T09:00:00Z', "
                "source_updated_utc='2026-01-10T09:00:00Z'")
    price2, real2 = paper._total_price_for(con, ctx, _Sig())
    assert (price2, real2) == (-108.0, True)


# ------------------------------------------------------------------ timezone

def test_et_date_rollover_for_late_tipoffs():
    """01:30Z belongs to the previous ET calendar day (both EDT and EST)."""
    assert util.et_game_date(util.parse_iso("2026-10-21T01:30:00Z")) == "2026-10-20"
    assert util.et_game_date(util.parse_iso("2026-01-15T02:00:00Z")) == "2026-01-14"
    assert util.et_game_date(util.parse_iso("2026-01-15T03:30:00Z")) == "2026-01-14"
    assert util.et_game_date(util.parse_iso("2026-01-15T20:00:00Z")) == "2026-01-15"


def test_timestamps_are_utc_and_parse_round_trips():
    s = util.utcnow_iso()
    assert s.endswith("Z")
    dt = util.parse_iso(s)
    assert dt is not None and dt.tzinfo is not None
    # documented convention: a naive timestamp (no offset) is interpreted as
    # UTC; our own writers always emit a trailing Z, and the audit pass reports
    # any stored value without an explicit zone marker.
    naive = util.parse_iso("2026-01-10T00:00:00")
    assert naive is not None and naive.tzinfo is not None
    assert util.to_iso(naive) == "2026-01-10T00:00:00Z"
    assert util.parse_iso("") is None and util.parse_iso(None) is None


def test_audit_finds_stored_timestamps_without_a_zone(con):
    """A naive stored timestamp is a decision-time risk, so the audit reports it."""
    from nbacomp import audit
    _game(con, "espn:adv1")
    con.execute("UPDATE games SET captured_utc='2026-01-10T00:00:00' "
                "WHERE game_id='espn:adv1'")
    summary = audit.run_checks(con)
    assert any(c["check"] == "naive-timestamp-stored" for c in summary["checks"])
    ok = con.execute("SELECT captured_utc FROM games WHERE game_id='espn:adv1'").fetchone()
    assert not ok["captured_utc"].endswith("Z")  # what we detected is still there


# ------------------------------------------------------------------- writes

def test_duplicate_bet_insert_is_ignored(con):
    _bet(con, bet_id="fo-dup")
    _bet(con, bet_id="fo-dup")
    assert con.execute("SELECT COUNT(*) c FROM bets WHERE bet_id='fo-dup'").fetchone()["c"] == 1


def test_append_only_trigger_protects_decision_columns(con):
    _bet(con, bet_id="fo-ao")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE bets SET price=999 WHERE bet_id='fo-ao'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE bets SET stake_usd=1 WHERE bet_id='fo-ao'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM bets WHERE bet_id='fo-ao'")
    # settlement columns are the only permitted mutation
    con.execute("UPDATE bets SET result='win', pnl_usd=1.0, settlement_utc=?, "
                "settlement_source='test' WHERE bet_id='fo-ao'", (util.utcnow_iso(),))


def test_missing_home_score_prevents_score_verification_noise(con):
    """A final game with NULL scores must not be written as 0-0 anywhere."""
    _game(con, "espn:adv1", "2026-01-10", status="final", hs=None, as_=None)
    row = con.execute("SELECT home_score, away_score FROM games WHERE game_id='espn:adv1'").fetchone()
    assert row["home_score"] is None and row["away_score"] is None


# ---------------------------------------------------------- registry / meta

def test_every_strategy_publishes_complete_versioned_metadata():
    required = {"name", "username", "category", "thesis", "description", "market_types",
                "data_sources", "entry_rules", "exit_rules", "sizing_rules", "version",
                "historical_window", "expected_edge", "failure_modes", "data_limitations",
                "lookahead_controls"}
    assert len(S.STRATEGIES) >= 24
    for sid, m in S.STRATEGIES.items():
        missing = required - set(m)
        assert not missing, f"{sid} missing {missing}"
        assert m["entry_rules"] and m["failure_modes"] and m["data_limitations"]
        assert m["version"].count(".") == 2
        assert m.get("hypothesis"), f"{sid} has no stated hypothesis"
    usernames = [m["username"] for m in S.STRATEGIES.values()]
    assert len(usernames) == len(set(usernames))


def test_version_history_records_preserved_rules():
    """Every bumped strategy keeps the rule text its old version traded."""
    bumped = [sid for sid, m in S.STRATEGIES.items() if m["version"] != "1.0.0"]
    assert bumped, "expected the 2026-09-21 evidence pass to have bumped versions"
    for sid in bumped:
        hist = S.STRATEGIES[sid].get("history") or []
        assert hist, f"{sid} was bumped but has no version history"
        for h in hist:
            assert h.get("note") and h.get("changed_utc") and h.get("superseded_by")
            assert len(h["note"]) > 40, f"{sid} version note is not informative"


def test_uncertainty_is_published_not_hidden():
    """The site must state the price limitations, not imply an unqualified edge.

    Updated 2026-09-21 after the SBR archive landed: 'no free historical price
    series exists' became FALSE, and a test that keeps asserting a false
    statement would push the site back toward it. The invariant that survives is
    the one that matters: published history must carry its limitations, and the
    price-based result (including its baseline) must be visible.
    """
    index_path = os.path.join(REPO, "index.html")
    sources_path = os.path.join(REPO, "sources.html")
    for path in (index_path, sources_path):
        if not os.path.exists(path):
            continue
        html = open(path, encoding="utf-8").read()
        assert ("per-side prices" in html or "PRICED-ASSUMPTION" in html
                or "no free historical" in html.lower())
    if os.path.exists(index_path):
        html = open(index_path, encoding="utf-8").read()
        # every published price-based number must sit next to its baseline
        assert "MARKET baseline" in html or "PRICED-ASSUMPTION" in html


def test_validation_policy_is_conservative():
    """No tier may ever allow more than the base stake, and 'failed' forbids betting."""
    for tier, pol in validation.POLICY.items():
        assert pol["stake_scale"] <= 1.0
        if tier == "failed":
            assert pol["allow_bets"] is False
            assert pol["stake_scale"] == 0.0
        assert 0.0 < pol["shrink"] <= 1.0
        assert 0.0 <= pol["max_edge"] <= 0.25
        if pol["allow_bets"]:
            assert pol["max_edge"] > 0.0 and pol["stake_scale"] > 0.0


def test_research_scan_file_is_self_consistent():
    path = os.path.join(REPO, "data", "research_scan.json")
    if not os.path.exists(path):
        pytest.skip("scan not generated in this checkout")
    scan = json.load(open(path))
    for key in ("n_finals_regular_season", "regular_season_baseline",
                "H1_opening_week_low_scoring", "H2_back_to_back"):
        assert key in scan
    base = scan["regular_season_baseline"]
    assert 200 < base["avg_total"] < 260
    assert 0.40 < base["home_win_pct"] < 0.70
    sweep = scan.get("factor_sweep", {})
    for rule, v in sweep.items():
        assert v["n"] >= 0 and 0.0 <= v["hit_rate"] <= 1.0
        if v.get("baseline_rate") is not None:
            assert 0.0 <= v["baseline_rate"] <= 1.0
