#!/usr/bin/env python
"""Post-collection database report for CI.

Prints row counts for every table plus the collection-log tail, and exits
non-zero when a collector CRASHED during this run. Silent zero-row collection
was the failure mode that kept this project's database empty for a day while
every job still reported success (2026-09-21), so the counts are printed on
every run whether or not anything is wrong.

Exit codes:
  0  no collector crashes recorded in the current run window
  1  at least one task recorded status='crash' (a code defect, not a 404)

Usage: db_report.py [--since ISO8601]
  --since restricts the crash/failure verdict to log rows written at or after
  that timestamp, so a crash fixed in a later commit does not keep failing the
  job forever (the log is append-only and never rewritten).
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
from nbacomp import db  # noqa: E402

#: Report order for the tables the project cares about most. This is a display
#: preference only -- it is NOT the set of tables reported. A hardcoded list was
#: the bug: `game_aliases`, `meta`, `player_season_stats`, `quarter_scores` and
#: `signal_backtest_summary` all existed in the schema and were silently absent
#: from this report, so two genuinely empty tables (`player_season_stats`,
#: `quarter_scores`) never appeared in the "empty tables" line that exists to
#: catch exactly that. Every table in sqlite_master is now reported.
TABLE_ORDER = ["games", "odds_snapshots", "injuries", "team_gamelogs", "player_gamelogs",
               "player_season_stats", "team_season_stats", "quarter_scores",
               "kalshi_markets", "kalshi_candles", "kalshi_orderbooks",
               "hist_odds", "hist_backtests", "line_backtests", "signal_backtests",
               "signal_backtest_summary", "strategies", "bets", "bet_flags",
               "bankroll_events", "verifications", "anomalies", "audit_log",
               "research_log", "source_status", "collection_log", "game_aliases",
               "meta"]


def all_tables(con) -> list[str]:
    """Every real table, preferred ones first, alphabetically afterwards.

    sqlite_sequence is excluded (SQLite's own AUTOINCREMENT bookkeeping, not
    project data). Anything added to the schema later is picked up here
    automatically, which is the whole point: the report cannot drift from the
    schema again.
    """
    present = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    ordered = [t for t in TABLE_ORDER if t in present]
    ordered += sorted(present - set(ordered))
    return ordered


def main(argv: list[str]) -> int:
    since = None
    if "--since" in argv:
        since = argv[argv.index("--since") + 1]
    con = db.connect()
    tables = all_tables(con)
    print(f"=== table row counts ({len(tables)} tables) ===")
    empty = []
    for t in tables:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception as e:  # schema drift should be loud, not silent
            print(f"{t:22s} ERROR {e}")
            continue
        print(f"{t:22s} {n}")
        if n == 0:
            empty.append(t)
    if empty:
        print("empty tables: " + ", ".join(empty))

    span = con.execute("SELECT MIN(game_date_et) lo, MAX(game_date_et) hi, "
                       "SUM(status='final') finals, SUM(verified=1) verified FROM games").fetchone()
    print(f"games span: {span[0]} .. {span[1]} finals={span[2]} verified={span[3]}")

    print("=== collection log (last 25) ===")
    for r in con.execute("SELECT ts_utc, task, source, status, rows, substr(detail,1,120) "
                         "FROM collection_log ORDER BY id DESC LIMIT 25"):
        print(f"{r[0]} {r[1]:22s} {r[2]:24s} {r[3]:8s} rows={r[4]:<6} {r[5]}")

    where = "WHERE status='crash'"
    params: list = []
    if since:
        where += " AND ts_utc >= ?"
        params.append(since)
    crashes = con.execute(
        f"SELECT task, source, ts_utc, substr(detail,1,400) FROM collection_log "
        f"{where} ORDER BY id DESC LIMIT 5", params).fetchall()
    print(f"=== collector crashes recorded (since {since or 'all time'}): {len(crashes)} ===")
    for c in crashes:
        print(f"{c[2]} {c[0]} ({c[1]})\n{c[3]}")

    fwhere = "WHERE status='fail'"
    fparams: list = []
    if since:
        fwhere += " AND ts_utc >= ?"
        fparams.append(since)
    fails = con.execute(
        f"SELECT task, source, COUNT(*) c FROM collection_log {fwhere} "
        f"GROUP BY 1,2 ORDER BY c DESC LIMIT 10", fparams).fetchall()
    if fails:
        print(f"=== repeated failures (since {since or 'all time'}) ===")
        for f in fails:
            print(f"{f[0]:22s} {f[1]:24s} {f[2]}")
    con.close()
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
