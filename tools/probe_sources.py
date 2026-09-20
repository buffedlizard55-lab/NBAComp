#!/usr/bin/env python
"""Probe every data source from the CURRENT machine and print a diagnosis.

Used by CI (committed as data/diagnostics.txt on failure) and locally.
Never fabricates: unreachable is unreachable.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp.sources import espn, kalshi, nba  # noqa: E402


def main():
    out = []

    def probe(name, fn):
        t0 = time.time()
        try:
            r = fn()
            if hasattr(r, "ok"):
                body = b"" if r.body is None else r.body[:200]
                out.append(f"{name}: http={r.status} ok={r.ok} bytes={len(r.body or b'')} "
                           f"err={r.error} t={time.time()-t0:.1f}s body[:160]={body!r}")
            else:
                out.append(f"{name}: rows={len(r)} t={time.time()-t0:.1f}s")
        except Exception as e:
            out.append(f"{name}: EXCEPTION {type(e).__name__}: {e}")

    probe("espn.scoreboard", lambda: espn.scoreboard())
    probe("espn.scoreboard(20260115)", lambda: espn.scoreboard("20260115"))
    probe("espn.injuries", espn.injuries)
    probe("nba.teamstats", lambda: nba.leaguedashteamstats("2025-26", "Advanced"))
    probe("nba.teamgamelogs", lambda: nba.teamgamelogs("2025-26"))
    probe("nba.scoreboardv2", lambda: nba.scoreboard_v2("01/15/2026"))
    probe("kalshi.series KXNBAGAME", lambda: kalshi.get_series("KXNBAGAME"))
    probe("kalshi.markets KXNBAGAME open", lambda: kalshi.get_markets("KXNBAGAME", "open", max_pages=1))
    probe("kalshi.markets KXNBAGAME settled", lambda: kalshi.get_markets("KXNBAGAME", "settled", max_pages=1))
    probe("kalshi.candlesticks", lambda: kalshi.get_candlesticks(
        ["X"], start_ts_ms=0, end_ts_ms=1, interval=60))

    # DB collection log tail
    try:
        from nbacomp import db
        con = db.connect()
        rows = con.execute("SELECT ts_utc, task, source, status, detail FROM collection_log "
                           "ORDER BY id DESC LIMIT 25").fetchall()
        out.append("\ncollection_log tail:")
        out.extend(f"  {r['ts_utc']} {r['task']} {r['source']} {r['status']} {r['detail'][:120]}"
                   for r in rows)
        an = con.execute("SELECT detected_utc, severity, check_name, detail_json FROM anomalies "
                         "ORDER BY id DESC LIMIT 15").fetchall()
        if an:
            out.append("anomaly tail:")
            out.extend(f"  {r['detected_utc']} {r['severity']} {r['check_name']} {r['detail_json'][:140]}"
                       for r in an)
    except Exception as e:
        out.append(f"db read failed: {e}")
    text = "\n".join(out)
    print(text)
    os.makedirs("data", exist_ok=True)
    with open("data/diagnostics.txt", "w") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
