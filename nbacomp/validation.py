"""Evidence-based strategy tiering and bet-gating.

Purpose: the competition must let *verified data* decide which strategies get
to trade. This module turns the outcome-only validation evidence
(`signal_backtest_summary`) plus any real price-verified history (`bets` with
kind='backtest') into an explicit tier per strategy, and from that tier into a
placement policy (whether bets are allowed at all, how much the model's
probability is shrunk toward the market, the maximum credible edge, and a
stake scale).

Everything here is derived from stored rows. Nothing is hand-set per
strategy, and the evidence used for each decision is returned for publication
on the site, so a reader can see exactly why a strategy is trading or parked.

Tiers
-----
price_verified     at least one settled/backtest bet priced from observed
                   market data (Kalshi candles) exists; the tier is the only
                   one that permits full-size trading.
outcome_validated  no price history, but the decision rule beat its
                   side-weighted base rate by the required margin on a large
                   enough sample (or, for total rules, beat the naive
                   league-average MAE).
weak               evidence exists but is inconclusive (small sample, or a
                   hit rate inside the noise band). Small "learning" size.
failed             evidence actively contradicts the rule (hit rate below
                   base rate, or MAE worse than the naive baseline). No bets:
                   the strategy is parked and stays visible on the site.
forward_only       no historical evidence is possible from free data
                   (injuries, props, live markets). Small learning size.

Shrinkage: a model probability is blended toward the market probability by the
tier's shrink weight before any edge is computed. This is a standard,
conservative correction for the fact that our model has *no* measured
calibration advantage over the market, and it is what stops a stale or
mis-specified model from claiming a 30-point edge on a liquid market.
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------- constants

#: Winner rules must beat their side-weighted base rate by this much.
WINNER_HIT_MARGIN = 0.03
#: Minimum signal count before outcome evidence is treated as usable at all.
MIN_N_OUTCOME = 30
#: Total rules must beat the naive league-average MAE by this many points.
TOTAL_MAE_MARGIN = 0.25
#: No single bet may claim an edge above this after shrinkage (sanity bound:
#: an edge this large on a liquid market is a model error, not an opportunity).
MAX_CREDIBLE_EDGE = 0.08

POLICY = {
    "price_verified":    {"allow_bets": True,  "shrink": 0.25, "stake_scale": 1.0,
                          "max_edge": 0.10},
    "outcome_validated": {"allow_bets": True,  "shrink": 0.50, "stake_scale": 0.5,
                          "max_edge": MAX_CREDIBLE_EDGE},
    "forward_only":      {"allow_bets": True,  "shrink": 0.60, "stake_scale": 0.25,
                          "max_edge": 0.06},
    "weak":              {"allow_bets": True,  "shrink": 0.60, "stake_scale": 0.25,
                          "max_edge": 0.06},
    "failed":            {"allow_bets": False, "shrink": 1.00, "stake_scale": 0.0,
                          "max_edge": 0.0},
}

#: Strategies whose historical evaluation is structurally impossible from free
#: keyless sources (documented per strategy in the registry). They are
#: forward-only experiments and are labelled as such on the site.
FORWARD_ONLY = {
    "NBA-005",  # injuries: no free dated injury archive
    "NBA-008",  # props: no free historical prop prices
    "NBA-009",
    "NBA-012",  # cross-market: needs two live price feeds
    "NBA-013",  # 1H markets: series not historically exposed
    "NBA-014",
    "NBA-016",  # foul-rate totals: needs free historical lines
    "NBA-017",
    "NBA-018",
    "NBA-019",
    "NBA-023",
    "NBA-024",
    "NBA-025",
    "NBA-026",
}

REASON = {
    "price_verified": "priced from observed market data; the only tier trading full size",
    "outcome_validated": "decision rule beat its base rate on verified results (outcome-only, no prices)",
    "forward_only": "no historical evidence obtainable from free sources; forward experiment",
    "weak": "evidence present but inconclusive (small sample or inside the noise band)",
    "failed": "verified evidence contradicts the rule; parked, not hidden",
}


def _latest_run(con) -> str | None:
    row = con.execute("SELECT run_id FROM signal_backtest_summary "
                      "ORDER BY generated_utc DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


def _hist_run(con) -> str | None:
    row = con.execute("SELECT run_id FROM hist_backtests ORDER BY generated_utc "
                      "DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


def _line_run(con) -> str | None:
    from . import line_backtest
    return line_backtest.latest_run(con)


#: Rules whose TRADED market is the market the line-based track measures, and
#: which may therefore be tiered by it. NBA-026 trades spreads against an
#: observed line, which is exactly what `line_backtests` measures. NBA-004's
#: spread-move result is published as evidence for its momentum hypothesis but
#: does NOT tier it: the live rule trades Kalshi winner-market moves in cents,
#: a market with no history, so the spread analogue is suggestive, not verdict.
TIERED_BY_LINE = {"NBA-026"}


def evidence_for(con, strategy_id: str) -> dict:
    """Raw validation evidence for one strategy.

    Four independent evidence streams, kept separate on purpose:
      * `summary` — outcome-only signal backtest (no prices exist for that rule)
      * `backtest_bets` — bets priced from observed market data
      * `hist` — the price-based simulation over the SBR archive (real moneylines)
      * `line` — the rule measured against the archive's OBSERVED closing lines
        (cover rate; no per-side price exists for those markets, so no P&L)
    """
    run_id = _latest_run(con)
    hist_run = _hist_run(con)
    line_run = _line_run(con)
    ev: dict = {"run_id": run_id, "summary": None, "backtest_bets": 0,
                "backtest_settled": 0, "hist_run": hist_run, "hist": None,
                "hist_all": None, "line_run": line_run, "line": None,
                "line_baseline": None}
    if line_run:
        row = con.execute(
            "SELECT * FROM line_backtests WHERE run_id=? AND rule_id=? "
            "AND metric='cover_rate' AND season='ALL'", (line_run, strategy_id)).fetchone()
        if row:
            ev["line"] = dict(row)
        base = con.execute(
            "SELECT * FROM line_backtests WHERE run_id=? AND rule_id='MARKET' "
            "AND metric='home_ats_cover_rate' AND season='ALL'", (line_run,)).fetchone()
        if base:
            ev["line_baseline"] = dict(base)
    if hist_run:
        row = con.execute(
            "SELECT * FROM hist_backtests WHERE run_id=? AND strategy_id=? "
            "AND UPPER(season)='ALL'", (hist_run, strategy_id)).fetchone()
        if row:
            ev["hist"] = dict(row)
        allrow = con.execute(
            "SELECT * FROM hist_backtests WHERE run_id=? AND strategy_id='MARKET' "
            "AND UPPER(season)='ALL'", (hist_run,)).fetchone()
        if allrow:
            ev["hist_all"] = dict(allrow)
    if run_id:
        row = con.execute(
            "SELECT * FROM signal_backtest_summary WHERE run_id=? AND strategy_id=? "
            "AND season='ALL'", (run_id, strategy_id)).fetchone()
        if row:
            ev["summary"] = dict(row)
    for r in con.execute(
            "SELECT COUNT(*) c, SUM(result IN ('win','loss','push')) settled "
            "FROM bets WHERE strategy_id=? AND kind='backtest'", (strategy_id,)):
        ev["backtest_bets"] = r["c"] or 0
        ev["backtest_settled"] = r["settled"] or 0
    return ev


def classify(con, strategy_id: str, ev: dict | None = None) -> dict:
    """Tier + human-readable reason for a strategy, derived from stored evidence."""
    ev = ev or evidence_for(con, strategy_id)
    summary = ev.get("summary") or {}
    out = {"strategy_id": strategy_id, "evidence": ev}

    hist = ev.get("hist")
    if hist and hist.get("bets", 0) >= 30:
        roi, n = hist["roi"], hist["bets"]
        base_roi = (ev.get("hist_all") or {}).get("roi")
        tier, detail = "price_verified", (
            f"simulated on {n} games at real archive moneylines: "
            f"P&L ${hist['pnl']:.2f}, ROI {roi:+.1%}, win rate "
            f"{(hist['win_rate'] or 0):.1%}"
            + (f" (market baseline ROI {base_roi:+.1%})" if base_roi is not None else ""))
        if roi is not None and roi < -0.05:
            tier = "failed"
            detail += (" — LOSS at real prices beyond the vig, so the rule is "
                       "parked by its own price-verified result")
        return {**out, "tier": tier, "policy": POLICY[tier],
                "reason": ("price-based simulation over the historical odds "
                           "archive" if tier == "price_verified"
                           else REASON["failed"]),
                "detail": detail}

    if ev.get("backtest_bets"):
        tier, detail = "price_verified", (
            f"{ev['backtest_bets']} price-verified backtest bets "
            f"({ev['backtest_settled']} settled) from observed market data")
        return {**out, "tier": tier, "policy": POLICY[tier], "reason": REASON[tier],
                "detail": detail}

    line = ev.get("line")
    if line and strategy_id in TIERED_BY_LINE and (line.get("n") or 0) >= MIN_N_OUTCOME:
        # Measured against the archive's own closing lines: a cover rate, not a
        # P&L. The economic bar is the break-even frequency at standard -110
        # juice, quoted as a reference because no per-side price exists.
        rate, be = line["value"], line["breakeven"]
        base = (ev.get("line_baseline") or {}).get("value")
        cmp_txt = (f"; market baseline (home ATS every game) {base:.2%}"
                   if base is not None else "")
        if rate >= be + WINNER_HIT_MARGIN:
            tier, detail = "outcome_validated", (
                f"covered {rate:.2%} of {line['n']} firings at the archive's own "
                f"closing lines vs the {be:.2%} standard-juice break-even"
                f"{cmp_txt}")
        elif rate <= be - 0.02:
            tier, detail = "failed", (
                f"covered {rate:.2%} of {line['n']} firings at the archive's own "
                f"closing lines, BELOW the {be:.2%} standard-juice break-even"
                f"{cmp_txt} — no cover edge, so the rule is parked by its own "
                f"line evidence (no P&L is claimed either way)")
        else:
            tier, detail = "weak", (
                f"covered {rate:.2%} of {line['n']} firings vs the {be:.2%} "
                f"break-even{cmp_txt}: inside the noise band")
        return {**out, "tier": tier, "policy": POLICY[tier],
                "reason": ("decision rule measured against observed historical "
                           "lines (cover rate vs standard-juice break-even; "
                           "no per-side prices exist, so no P&L)"),
                "detail": detail}

    n = summary.get("n_signals") or 0
    if summary.get("mae_total") is not None:
        if not n:
            row = con.execute(
                "SELECT COUNT(*) c FROM signal_backtests WHERE run_id=? AND strategy_id=? "
                "AND market='total'", (ev.get("run_id"), strategy_id)).fetchone()
            n = (row["c"] if row else 0) or 0
        mae, bmae = summary.get("mae_total"), summary.get("baseline_mae_total")
        if bmae is None:
            tier, detail = "forward_only", "no baseline MAE available"
        elif mae <= bmae - TOTAL_MAE_MARGIN:
            tier, detail = "outcome_validated", (
                f"model MAE {mae:.2f} beats naive league-mean MAE {bmae:.2f} "
                f"over {n} evaluated games")
        elif mae > bmae:
            tier, detail = "failed", (
                f"model MAE {mae:.2f} is WORSE than the naive league-mean MAE "
                f"{bmae:.2f} over {n} evaluated games")
        else:
            tier, detail = "weak", (
                f"model MAE {mae:.2f} vs naive {bmae:.2f}: inside the "
                f"{TOTAL_MAE_MARGIN}pt margin over {n} evaluated games")
        return {**out, "tier": tier, "policy": POLICY[tier], "reason": REASON[tier],
                "detail": detail}

    if n >= MIN_N_OUTCOME and summary.get("hit_rate") is not None:
        hit, base = summary["hit_rate"], summary.get("baseline_rate")
        if base is None:
            tier, detail = "weak", "no base rate available"
        elif hit >= base + WINNER_HIT_MARGIN:
            tier, detail = "outcome_validated", (
                f"{hit:.1%} hits vs {base:.1%} base rate on {n} firings "
                f"(+{hit - base:.1%})")
        elif hit <= base - 0.02:
            tier, detail = "failed", (
                f"{hit:.1%} hits vs {base:.1%} base rate on {n} firings "
                f"({hit - base:+.1%})")
        else:
            tier, detail = "weak", (
                f"{hit:.1%} vs {base:.1%} base rate on {n} firings "
                f"(inside the {WINNER_HIT_MARGIN:.0%} required margin)")
        return {**out, "tier": tier, "policy": POLICY[tier], "reason": REASON[tier],
                "detail": detail}

    if strategy_id in FORWARD_ONLY:
        tier = "forward_only"
        detail = "no outcome-only evaluation exists for this rule"
    elif n > 0:
        tier, detail = "weak", f"only {n} rule firings (< {MIN_N_OUTCOME} required)"
    else:
        tier, detail = "weak", "no rule firings recorded"
    return {**out, "tier": tier, "policy": POLICY[tier], "reason": REASON[tier],
            "detail": detail}


def all_tiers(con, strategy_ids: list[str] | None = None) -> dict[str, dict]:
    from . import strategies as S
    ids = strategy_ids or list(S.STRATEGIES)
    return {sid: classify(con, sid) for sid in ids}


# ------------------------------------------------------- placement decisions

def calibrated_prob(p_model: float, p_market: float, shrink: float) -> float:
    """Blend the model probability toward the market price (shrinkage)."""
    s = max(0.0, min(1.0, shrink))
    return (1.0 - s) * p_model + s * p_market


def gate(sig_prob: float, market_prob: float, policy: dict) -> tuple[float, str | None]:
    """Return (calibrated_prob, rejection_reason).

    Applies the tier's shrinkage and then the maximum credible edge. A signal
    that still claims more than `max_edge` after shrinkage is REJECTED (and the
    caller records why) rather than quietly traded at an absurd size.
    """
    p = calibrated_prob(sig_prob, market_prob, policy["shrink"])
    edge = p - market_prob
    if edge > policy["max_edge"] + 1e-9:
        return p, (f"edge {edge:.3f} exceeds the tier's credible maximum "
                   f"{policy['max_edge']:.2f} after shrinkage ")
    return p, None


def apply_policy(con, signals: list, tiers: dict | None = None,
                 on_drop=None, cache: dict | None = None) -> list:
    """Filter + recalibrate a batch of Signals according to each strategy's tier.

    - ``failed`` (or any tier with allow_bets False): signal dropped.
    - otherwise the model probability is blended toward the market by the
      tier's shrink weight, and the signal is DROPPED when the resulting edge
      still exceeds the tier's credible maximum (a claimed edge that large on a
      liquid market is treated as model error, not opportunity).
    - kept signals carry the calibrated probability and a trigger suffix that
      records the raw probability, the weight and the tier, so the audit trail
      shows exactly what was traded.

    ``cache`` lets a caller reuse tier lookups across many games.
    """
    tiers = tiers if tiers is not None else (cache if cache is not None else None)
    kept = []
    for s in signals:
        key = getattr(s, "strategy_id", None)
        info = (tiers or {}).get(key)
        if info is None:
            info = classify(con, key)
            if tiers is not None:
                tiers[key] = info
        pol = info["policy"]
        if not pol["allow_bets"]:
            if on_drop:
                on_drop(s, f"tier={info['tier']} ({info['detail']})")
            continue
        raw = s.model_prob
        p_cal = calibrated_prob(raw, s.market_prob, pol["shrink"])
        edge = p_cal - s.market_prob
        if edge > pol["max_edge"] + 1e-9:
            if on_drop:
                on_drop(s, (f"implausible edge {edge:+.3f} after shrinkage "
                            f"{pol['shrink']:.2f} (raw {raw:.3f} vs market "
                            f"{s.market_prob:.3f}); tier={info['tier']} "
                            f"max_edge={pol['max_edge']:.2f}"))
            continue
        s.model_prob = p_cal
        s.trigger = (f"{s.trigger} | policy: raw p={raw:.3f}, shrink="
                     f"{pol['shrink']:.2f} -> p={p_cal:.3f} (edge {edge:+.3f}), "
                     f"tier={info['tier']}")
        kept.append(s)
    return kept


def stake_scale(con, strategy_id: str) -> float:
    return classify(con, strategy_id)["policy"]["stake_scale"]


def policy_json(policy: dict) -> str:
    return json.dumps(policy, sort_keys=True)
