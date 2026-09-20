#!/usr/bin/env python
"""Deep probe: find a WORKING path to ESPN/NBA data from this runner.

Tests multiple transport fingerprints (urllib vs curl) and endpoints.
Writes data/diagnostics.txt. Nothing is fabricated: failures are failures.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import db, util  # noqa: E402
from nbacomp import http  # noqa: E402
from nbacomp.sources import espn, kalshi  # noqa: E402

ESPN_UA = http.USER_AGENT


def curl(url: str, headers: list[str] | None = None, timeout: int = 25) -> tuple[int, bytes]:
    cmd = ["curl", "-s", "-o", "-", "-w", "%{http_code}", "-m", str(timeout),
           "-H", f"User-Agent: {ESPN_UA}"]
    for h in headers or []:
        cmd += ["-H", h]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True)
    body = p.stdout
    code = body[-3:].decode("ascii", "replace")
    return int(code) if code.isdigit() else 0, body[:-3]


def main():
    out = []

    def log(s):
        out.append(s)
        print(s, flush=True)

    # ---- ESPN via curl (different TLS fingerprint than urllib)
    code, body = curl("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard")
    log(f"curl espn.scoreboard: {code} bytes={len(body)} head={body[:120]!r}")
    if code == 200:
        try:
            js = json.loads(body)
            log(f"  events={len(js.get('events') or [])}")
        except json.JSONDecodeError:
            log("  json parse failed")
    code, body = curl("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20260115")
    log(f"curl espn.scoreboard(20260115): {code} bytes={len(body)}")
    code, body = curl("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries")
    log(f"curl espn.injuries: {code} bytes={len(body)}")
    code, body = curl("https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard")
    log(f"curl espn.site-web scoreboard: {code} bytes={len(body)}")
    code, body = curl("https://sports.core.api.espn.com/v2/sports/basketball/league/nba/injuries?limit=50")
    log(f"curl espn.core injuries: {code} bytes={len(body)}")
    # with fuller browser header set
    hdrs = ["Accept: application/json, text/plain, */*", "Accept-Language: en-US,en;q=0.9",
            "Origin: https://www.espn.com", "Referer: https://www.espn.com/",
            "Sec-Fetch-Dest: empty", "Sec-Fetch-Mode: cors", "Sec-Fetch-Site: same-site"]
    code, body = curl("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", hdrs)
    log(f"curl espn.scoreboard +browser-hdrs: {code} bytes={len(body)}")

    # ---- NBA.com via curl
    nbah = ["Referer: https://www.nba.com/", "Origin: https://www.nba.com"]
    code, body = curl("https://stats.nba.com/stats/scoreboardv2?GameDate=01%2F15%2F2026&LeagueID=00&DayOffset=0",
                      nbah + ["Accept-Encoding: gzip, deflate, br"], timeout=30)
    log(f"curl stats.nba.com scoreboardv2: {code} bytes={len(body)} head={body[:100]!r}")
    code, body = curl("https://data.nba.net/prod/v1/20260115/scoreboard.json", timeout=30)
    log(f"curl data.nba.net scoreboard: {code} bytes={len(body)} head={body[:100]!r}")
    code, body = curl("https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json", timeout=30)
    log(f"curl cdn.nba.net liveData scoreboard: {code} bytes={len(body)} head={body[:100]!r}")

    # ---- Kalshi deeper probes
    probe_series = ["KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBA1H", "KXNBAPOINT",
                    "KXNBAREB", "KXNBAAST", "KXNBAMVP"]
    found = {}
    for s in probe_series:
        r = kalshi.get_series(s)
        found[s] = bool(r.ok)
        log(f"kalshi.series {s}: http={r.status} exists={r.ok}")
    open_markets = kalshi.get_markets("KXNBAGAME", "open", max_pages=2)
    log(f"kalshi KXNBAGAME open: rows={len(open_markets)}")
    tickers = [m["ticker"] for m in open_markets[:3]]
    if tickers:
        log(f"  sample tickers: {tickers}")
        m0 = open_markets[0]
        log(f"  sample market: title={m0.get('title')!r} subtitle={m0.get('market_subtitle')!r} "
            f"strike={m0.get('strike')} open_interest={m0.get('open_interest')} "
            f"close_time={m0.get('close_time')} yes_bid={m0.get('yes_bid')} yes_ask={m0.get('yes_ask')}")
        from nbacomp.sources import kalshi as K
        now = int(time.time() * 1000)
        cs = K.get_candlesticks(tickers, now - 48 * 3600 * 1000, now, 60)
        log(f"kalshi.candlesticks(48h, {len(tickers)} real tickers): entries={len(cs)} "
            f"candles={sum(len(e.get('candlesticks') or []) for e in cs)}")
        ob = kalshi.get_orderbooks(tickers)
        log(f"kalshi.orderbooks: entries={len(ob)} sample={(ob[0] if ob else None)!r:.200}")
    for status in ("settled", "finalized", "closed"):
        rows = kalshi.get_markets("KXNBAGAME", status, max_pages=1)
        log(f"kalshi KXNBAGAME status={status}: rows={len(rows)}")
        if rows:
            m0 = rows[0]
            log(f"  sample settled: {m0.get('ticker')} result={m0.get('result')} "
                f"close={m0.get('close_time')}")

    # DB log tail
    try:
        con = db.connect()
        rows = con.execute("SELECT ts_utc, task, source, status, detail FROM collection_log "
                           "ORDER BY id DESC LIMIT 12").fetchall()
        out.append("collection_log tail:")
        out.extend(f"  {r['ts_utc']} {r['task']} {r['source']} {r['status']} {r['detail'][:100]}"
                   for r in rows)
    except Exception as e:
        out.append(f"db read failed: {e}")

    os.makedirs("data", exist_ok=True)
    with open("data/diagnostics.txt", "w") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        os.makedirs("data", exist_ok=True)
        with open("data/diagnostics.txt", "a") as f:
            f.write("\nPROBE_CRASH:\n" + tb + "\n")
        raise
