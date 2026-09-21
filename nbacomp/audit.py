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

    # --- pipeline coverage: an empty database is a FAILURE, not a clean run.
    # (2026-09-21 defect: every collector task was silently producing zero
    # rows — snapshot crashed, history backfill never ran, BRef 404'd — while
    # the pipeline still exited 0 and published a green-looking site.)
    counts = {t: con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("games", "odds_snapshots", "kalshi_markets", "kalshi_candles",
                        "kalshi_orderbooks", "injuries", "team_gamelogs",
                        "player_gamelogs", "bets")}
    if counts["games"] == 0:
        record("critical", "empty-games-table",
               {"detail": "no games collected — backtests and forward betting are impossible",
                "counts": counts})
    if counts["kalshi_markets"] == 0:
        record("critical", "empty-kalshi-markets-table",
               {"detail": "no Kalshi markets stored — no verifiable prices exist",
                "counts": counts})
    if counts["games"] and counts["kalshi_markets"] and counts["bets"] == 0:
        record("warn", "no-bets-despite-data",
               {"detail": "games and prices exist but no bets were generated",
                "counts": counts})

    # --- collection health: crashes and repeated failures must surface.
    # Windowed to the last 24h: the collection_log is append-only, so an
    # all-time scan would keep a defect fixed yesterday on the dashboard
    # forever (and hide the fact that today's run was clean).
    window_start = util.to_iso(util.parse_iso(util.utcnow_iso()) - util.timedelta(hours=24))
    recent = con.execute(
        "SELECT task, source, status, detail, ts_utc FROM collection_log "
        "WHERE status IN ('crash','fail') AND ts_utc >= ? ORDER BY id DESC LIMIT 40",
        (window_start,)).fetchall()
    crash_tasks = sorted({r["task"] for r in recent if r["status"] == "crash"})
    if crash_tasks:
        record("critical", "collector-crash",
               {"tasks": crash_tasks,
                "latest": [{k: r[k] for k in ("task", "source", "ts_utc", "detail")}
                           for r in recent if r["status"] == "crash"][:3]})
    fail_counts: dict[str, int] = {}
    for r in recent:
        if r["status"] == "fail":
            fail_counts[f"{r['task']}:{r['source']}"] = fail_counts.get(f"{r['task']}:{r['source']}", 0) + 1
    persistent = {k: v for k, v in fail_counts.items() if v >= 5}
    if persistent:
        record("warn", "persistent-source-failure", {"failures": persistent})

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

    # --- probability / odds math
    outofrange = con.execute(
        "SELECT bet_id, model_prob, market_prob, edge FROM bets WHERE model_prob IS NOT NULL "
        "AND (model_prob <= 0 OR model_prob >= 1 OR (market_prob IS NOT NULL AND "
        "(market_prob <= 0 OR market_prob >= 1)))").fetchall()
    for r in outofrange:
        record("critical", "impossible-probability", dict(r))

    # --- exposure cap: any strategy whose open exposure exceeds 25% of bankroll
    exp_rows = con.execute(
        "SELECT b.strategy_id, COALESCE(SUM(b.stake_usd),0) s, br.current FROM bets b "
        "JOIN (SELECT strategy_id, current FROM bankroll_events "
        "       GROUP BY strategy_id HAVING as_of_utc=MAX(as_of_utc)) br "
        "ON br.strategy_id=b.strategy_id WHERE b.kind='forward' AND b.result='pending' "
        "GROUP BY b.strategy_id").fetchall()
    for r in exp_rows:
        if r["current"] and r["s"] > 0.25 * r["current"] + 0.01:
            record("critical", "exposure-cap-violated",
                   {"strategy": r["strategy_id"], "open_pnl": round(r["s"], 2),
                    "bankroll": round(r["current"], 2)})

    # --- missing strategy / unknown strategy id
    unknown = con.execute(
        "SELECT DISTINCT strategy_id FROM bets WHERE strategy_id NOT IN "
        "(SELECT strategy_id FROM strategies)").fetchall()
    for r in unknown:
        record("warn", "bet-by-unknown-strategy", dict(r))

    # --- source-status reachability: at least one src hasn't been verified in 7d
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_src = con.execute(
        "SELECT source_id, checked_utc, ok FROM source_status WHERE checked_utc < ?", (cutoff,)).fetchall()
    for r in stale_src:
        record("info", "source-stale", {"source": r["source_id"],
                                        "last_checked": r["checked_utc"], "ok": r["ok"]})

    # --- duplicate kalshi markets (same ticker inserted twice with different payloads)
    dup_km = con.execute(
        "SELECT event_ticker, COUNT(DISTINCT ticker) c FROM kalshi_markets GROUP BY 1 HAVING c>1"
    ).fetchall()
    for r in dup_km:
        record("info", "kalshi-multi-tickers-per-event", dict(r))

    # --- outstanding pending bets whose tipoff was 24h ago
    overdue = con.execute(
        "SELECT bet_id, strategy_id, game_id, tipoff_utc FROM bets WHERE result='pending' "
        "AND kind='forward' AND tipoff_utc < ?",
        ((datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),)
    ).fetchall()
    for r in overdue:
        record("warn", "bet-still-pending-after-24h", dict(r))

    # --- injury listing appearing AFTER game result (impossible)
    futuristic_inj = con.execute(
        "SELECT i.player, i.published_utc, g.game_date_et FROM injuries i "
        "JOIN games g ON g.home_team = i.team OR g.away_team = i.team "
        "WHERE i.published_utc IS NOT NULL AND date(i.published_utc) > "
        "datetime(g.game_date_et, '+7 days') LIMIT 25").fetchall()
    for r in futuristic_inj:
        record("info", "injury-listing-late", dict(r))

    return summary
