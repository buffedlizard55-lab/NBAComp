"""Anomaly detection + data-quality checks. Flags, never silently fixes."""
from __future__ import annotations

import json

from . import util


def run_checks(con) -> dict:
    summary = {"critical": 0, "warn": 0, "info": 0, "checks": []}

    def record(sev, name, detail):
        summary[sev] += 1
        summary["checks"].append({"severity": sev, "check": name, "detail": detail})
        from . import db
        db.log_anomaly(con, sev, name, detail)

    # --- look-ahead guards on every bet
    bad = con.execute(
        "SELECT bet_id, decision_utc, tipoff_utc FROM bets WHERE tipoff_utc IS NOT NULL "
        "AND decision_utc > tipoff_utc").fetchall()
    for r in bad:
        record("critical", "lookahead-decision-after-tipoff",
               {"bet_id": r["bet_id"], "decision": r["decision_utc"], "tipoff": r["tipoff_utc"]})
    bad2 = con.execute(
        "SELECT bet_id, source_ts, decision_utc FROM bets WHERE source_ts IS NOT NULL "
        "AND source_ts > decision_utc").fetchall()
    for r in bad2:
        record("critical", "lookahead-price-after-decision",
               {"bet_id": r["bet_id"], "source_ts": r["source_ts"], "decision": r["decision_utc"]})

    # --- duplicate bets (same strategy/run/game/market/selection)
    dups = con.execute(
        "SELECT strategy_id, run_id, game_id, market, selection, COUNT(*) c FROM bets "
        "GROUP BY 1,2,3,4,5 HAVING c>1").fetchall()
    for r in dups:
        record("warn", "duplicate-bet", {k: r[k] for k in
                                         ("strategy_id", "run_id", "game_id", "market", "selection", "c")})

    # --- settlement recomputation for score-settled bets
    rows = con.execute(
        "SELECT b.bet_id, b.result, b.market, b.selection, b.side, g.home_score, g.away_score "
        "FROM bets b JOIN games g ON g.game_id=b.game_id "
        "WHERE b.result IN ('win','loss') AND g.home_score IS NOT NULL").fetchall()
    from . import engine
    for b in rows:
        if b["market"] == "kalshi:winner":
            expect = engine.settle_score_based("winner", b["home_score"], b["away_score"], b["side"], None)
            if expect != b["result"]:
                # Kalshi result takes precedence when recorded; flag conflicts for review
                record("warn", "settlement-vs-score-conflict",
                       {"bet_id": b["bet_id"], "recorded": b["result"], "score_implied": expect})
        elif b["market"] == "total":
            line = None
            try:
                line = float(b["selection"].split()[-1])
            except (ValueError, IndexError):
                pass
            if line is not None:
                expect = engine.settle_score_based("total", b["home_score"], b["away_score"],
                                                   b["selection"].split()[0], line)
                if expect not in (b["result"], "push") and not (expect == "push"):
                    record("warn", "total-settlement-vs-score-conflict",
                           {"bet_id": b["bet_id"], "recorded": b["result"], "score_implied": expect})

    # --- games quality
    dup_games = con.execute(
        "SELECT game_date_et, home_team, away_team, COUNT(*) c FROM games "
        "GROUP BY 1,2,3 HAVING c>1").fetchall()
    for r in dup_games:
        record("warn", "duplicate-game", dict(r))
    stale = con.execute(
        "SELECT game_id, status, game_date_et FROM games WHERE status IN ('scheduled','in') "
        "AND game_date_et < date('now','-2 days')").fetchall()
    for r in stale:
        record("info", "stale-unfinalized-game", dict(r))
    neg = con.execute(
        "SELECT game_id FROM games WHERE home_score<0 OR away_score<0").fetchall()
    for r in neg:
        record("critical", "impossible-score", dict(r))

    # --- odds quality
    badp = con.execute(
        "SELECT id, price, price_format FROM odds_snapshots WHERE price_format='american' "
        "AND (price BETWEEN -100 AND 100 AND price != 0)").fetchall()
    for r in badp:
        record("warn", "implausible-american-odds", dict(r))
    badk = con.execute(
        "SELECT ticker, yes_ask FROM kalshi_markets WHERE status='active' AND yes_ask IS NOT NULL "
        "AND (yes_ask < 1 OR yes_ask > 99)").fetchall()
    for r in badk:
        record("warn", "implausible-kalshi-price", dict(r))

    # --- verification mismatches
    mism = con.execute("SELECT COUNT(*) c FROM verifications WHERE status='mismatch'").fetchone()["c"]
    if mism:
        record("critical", "score-verification-mismatch", {"count": mism})

    # --- injuries missing timestamps
    miss = con.execute(
        "SELECT COUNT(*) c FROM injuries WHERE captured_utc IS NULL OR (published_utc IS NULL "
        "AND source='espn')").fetchone()["c"]
    if miss:
        record("info", "injury-missing-publish-ts", {"count": miss})

    # --- negative bankrolls
    neg_br = con.execute("SELECT strategy_id, current FROM bankroll_events WHERE current < 0").fetchall()
    for r in neg_br:
        record("critical", "negative-bankroll", dict(r))

    return summary
