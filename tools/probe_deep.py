#!/usr/bin/env python
"""Probe #3: pin down the working endpoint matrix.

Focus: site.web.api.espn.com (scoreboard/summary/injuries/teams/standings,
historical dates), basketball-reference reachability, stats.nba.com via Node
fetch, and Kalshi historical market access (windowed queries, events status,
candlesticks/trades on real markets).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import http  # noqa: E402
from nbacomp.sources import kalshi  # noqa: E402

OUT = []


def log(s):
    OUT.append(s)
    print(s, flush=True)


def curl(url: str, timeout: int = 25) -> tuple[int, bytes]:
    p = subprocess.run(["curl", "-s", "-o", "-", "-w", "%{http_code}", "-m", str(timeout),
                        "-H", f"User-Agent: {http.USER_AGENT}", url], capture_output=True)
    body = p.stdout
    code = body[-3:].decode("ascii", "replace")
    return int(code) if code.isdigit() else 0, body[:-3]


def jlen(body: bytes, key: str) -> str:
    try:
        js = json.loads(body)
        v = js
        for k in key.split("."):
            v = v.get(k, []) if isinstance(v, dict) else []
        return str(len(v)) if isinstance(v, list) else "dict"
    except Exception as e:
        return f"parse-err {e}"


def main():
    WEB = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba"

    # ESPN matrix on the working host
    for label, url, key in [
        ("site.web scoreboard today", f"{WEB}/scoreboard", "events"),
        ("site.web scoreboard 20260115", f"{WEB}/scoreboard?dates=20260115", "events"),
        ("site.web scoreboard 20250605 (finals)", f"{WEB}/scoreboard?dates=20250605", "events"),
        ("site.web injuries", f"{WEB}/injuries", "items"),
        ("site.web teams", f"{WEB}/teams", "sports.0.leagues.0.teams"),
    ]:
        code, body = curl(url)
        log(f"{label}: {code} bytes={len(body)} {key}={jlen(body, key) if code == 200 else '-'}")

    # summary for a specific event (from 20260115 board)
    code, body = curl(f"{WEB}/scoreboard?dates=20260115")
    event_id = None
    if code == 200:
        try:
            js = json.loads(body)
            evs = js.get("events") or []
            if evs:
                event_id = evs[0].get("id")
                log(f"  sample event id={event_id} date={evs[0].get('date')}")
        except json.JSONDecodeError:
            pass
    if event_id:
        code, body = curl(f"{WEB}/summary?event={event_id}")
        ok = code == 200
        has_box = "boxscore" in body.decode("utf-8", "replace") if ok else False
        log(f"site.web summary?event={event_id}: {code} bytes={len(body)} has_boxscore={has_box}")

    # standings via core API
    code, body = curl("https://sports.core.api.espn.com/v2/sports/basketball/league/nba/standings?season=2025")
    log(f"core standings 2025: {code} bytes={len(body)}")
    code, body = curl("https://sports.core.api.espn.com/v2/sports/basketball/league/nba/injuries")
    log(f"core injuries (no params): {code} bytes={len(body)}")

    # basketball-reference
    code, body = curl("https://www.basketball-reference.com/leagues/NBA_2026_games.html")
    log(f"basketball-reference 2026 games: {code} bytes={len(body)}")

    # stats.nba.com via node fetch (different TLS stack)
    node_script = ("fetch('https://stats.nba.com/stats/scoreboardv2?GameDate=01%2F15%2F2026&LeagueID=00&DayOffset=0',"
                   "{headers:{'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36','Referer':'https://www.nba.com/',"
                   "'Accept':'application/json'}}).then(r=>r.text().then(t=>"
                   "console.log('status',r.status,'len',t.length))).catch(e=>console.log('ERR',String(e).slice(0,120)))")
    p = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, timeout=60)
    log(f"node stats.nba.com scoreboardv2: {p.stdout.strip()} {p.stderr.strip()[:120]}")

    node_script2 = ("fetch('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',"
                    "{headers:{'User-Agent':'Mozilla/5.0'}}).then(r=>r.text().then(t=>"
                    "console.log('status',r.status,'len',t.length))).catch(e=>console.log('ERR',String(e).slice(0,120)))")
    p = subprocess.run(["node", "-e", node_script2], capture_output=True, text=True, timeout=60)
    log(f"node site.api.espn.com scoreboard: {p.stdout.strip()} {p.stderr.strip()[:120]}")

    # Kalshi historical access patterns
    start = int(time.mktime(time.strptime("2026-01-10", "%Y-%m-%d")))
    end = int(time.mktime(time.strptime("2026-01-20", "%Y-%m-%d")))
    r = http.get(f"{kalshi.BASE}/markets", {"series_ticker": "KXNBAGAME",
                                            "min_close_ts": start, "max_close_ts": end, "limit": 1000})
    log(f"kalshi markets window 2026-01-10..20 (no status): http={r.status} "
        f"rows={len((r.json or {}).get('markets') or [])}")
    if r.ok and (r.json or {}).get("markets"):
        m0 = r.json["markets"][0]
        log(f"  sample: {m0.get('ticker')} result={m0.get('result')} close={m0.get('close_time')} "
            f"yes_open={m0.get('open_interest')}")
        tk = m0["ticker"]
        r2 = http.get(f"{kalshi.BASE}/markets/candlesticks",
                      {"tickers": tk, "start_ts": start, "end_ts": end, "interval": 60})
        cs = (r2.json or {}).get("candlesticks") or []
        log(f"kalshi candles real settled ticker: http={r2.status} entries={len(cs)} "
            f"candles={sum(len(e.get('candlesticks') or []) for e in cs)}")
        if cs:
            log(f"  candle sample: {cs[0].get('candlesticks', [{}])[0]}")
    r = http.get(f"{kalshi.BASE}/events", {"series_ticker": "KXNBAGAME", "status": "settled", "limit": 5})
    log(f"kalshi events settled: http={r.status} rows={len((r.json or {}).get('events') or [])}")
    # more prop series candidates
    for s in ["KXNBAPTS", "KXNBATHREES", "KXNBATO", "KXNBAPRA", "KXNBADD", "KXNBASTL", "KXNBABLK",
              "KXNBAREBS", "KXNBAASTS"]:
        rr = kalshi.get_series(s)
        log(f"kalshi.series {s}: exists={rr.ok} http={rr.status}")

    with open("data/diagnostics.txt", "w") as f:
        f.write("\n".join(OUT) + "\n")


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
