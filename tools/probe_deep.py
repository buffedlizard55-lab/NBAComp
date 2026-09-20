#!/usr/bin/env python
"""Probe #4 (final matrix): writes every result line incrementally to
data/diagnostics.txt so partial results survive crashes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import http  # noqa: E402
from nbacomp.sources import kalshi  # noqa: E402

PATH = "data/diagnostics.txt"


def log(s):
    print(s, flush=True)
    os.makedirs("data", exist_ok=True)
    with open(PATH, "a") as f:
        f.write(s + "\n")


def curl(url: str, timeout: int = 25) -> tuple[int, bytes]:
    p = subprocess.run(["curl", "-s", "-o", "-", "-w", "%{http_code}", "-m", str(timeout),
                        "-H", f"User-Agent: {http.USER_AGENT}", url], capture_output=True)
    body = p.stdout
    code = body[-3:].decode("ascii", "replace")
    return int(code) if code.isdigit() else 0, body[:-3]


def node_fetch(url: str, hdrs: str = "{}", timeout: int = 45) -> str:
    script = ("fetch(" + json.dumps(url) + ",{headers:" + hdrs +
              "}).then(r=>r.text().then(t=>console.log('status',r.status,'len',t.length,"
              "'head',t.slice(0,80)))).catch(e=>console.log('ERR',String(e).slice(0,120)))")
    try:
        p = subprocess.run(["node", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout.strip() or p.stderr.strip()[:150]
    except subprocess.TimeoutExpired:
        return f"TIMEOUT >{timeout}s"
    except FileNotFoundError:
        return "node not available"


def main():
    os.makedirs("data", exist_ok=True)
    with open(PATH, "w") as f:
        f.write("probe4 " + time.strftime("%FT%TZ", time.gmtime()) + "\n")

    WEB = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba"

    # --- ESPN matrix on site.web (the host that answered 200)
    for label, url in [
        ("site.web scoreboard today", f"{WEB}/scoreboard"),
        ("site.web scoreboard 20260115", f"{WEB}/scoreboard?dates=20260115"),
        ("site.web scoreboard 20250605", f"{WEB}/scoreboard?dates=20250605"),
        ("site.web injuries", f"{WEB}/injuries"),
        ("site.web teams", f"{WEB}/teams"),
    ]:
        code, body = curl(url)
        ev = ""
        if code == 200:
            try:
                js = json.loads(body)
                n = len(js.get("events") or js.get("items") or [])
                ev = f"toplist={n}"
                if url.endswith("20260115") and js.get("events"):
                    e0 = js["events"][0]
                    ev += f" first={e0.get('shortName')} status={(e0.get('status') or {}).get('type', {}).get('state')}"
                    comp = (e0.get("competitions") or [{}])[0]
                    odds = comp.get("odds")
                    if odds:
                        o = odds[0]
                        ev += f" odds: provider={((o.get('provider') or {}).get('name'))} details={o.get('details')} ou={o.get('overUnder')} ml_h={(o.get('homeTeamOdds') or {}).get('moneyLine')}"
                    else:
                        ev += " odds=None"
            except Exception as e:
                ev = f"parse-err {e}"
        log(f"{label}: {code} bytes={len(body)} {ev}")

    # summary for one historical event -> boxscore presence
    code, body = curl(f"{WEB}/scoreboard?dates=20260115")
    if code == 200:
        try:
            e0 = (json.loads(body).get("events") or [{}])[0]
            eid = e0.get("id")
            code2, body2 = curl(f"{WEB}/summary?event={eid}")
            has_box = b"boxscore" in body2
            has_players = b"athletes" in body2
            has_inj = b"injuries" in body2
            log(f"site.web summary?event={eid}: {code2} bytes={len(body2)} "
                f"boxscore={has_box} athletes={has_players} injuries={has_inj}")
        except Exception as e:
            log(f"summary probe failed: {e}")

    # --- basketball-reference
    code, body = curl("https://www.basketball-reference.com/leagues/NBA_2026_games.html")
    log(f"basketball-reference 2026 games: {code} bytes={len(body)}")

    # --- stats.nba.com via node (last check) + site.api via node
    log("node stats.nba.com scoreboardv2: " + node_fetch(
        "https://stats.nba.com/stats/scoreboardv2?GameDate=01%2F15%2F2026&LeagueID=00&DayOffset=0",
        "{'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36','Referer':'https://www.nba.com/','Accept':'application/json'}"))
    log("node site.api.espn.com scoreboard: " + node_fetch(
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        "{'User-Agent':'Mozilla/5.0'}"))

    # --- Kalshi historical window access
    start = int(time.mktime(time.strptime("2026-01-10", "%Y-%m-%d")))
    end = int(time.mktime(time.strptime("2026-01-20", "%Y-%m-%d")))
    r = http.get(f"{kalshi.BASE}/markets", {"series_ticker": "KXNBAGAME",
                                            "min_close_ts": start, "max_close_ts": end, "limit": 1000})
    markets = (r.json or {}).get("markets") or []
    log(f"kalshi markets window 2026-01-10..20 no-status: http={r.status} rows={len(markets)}")
    if markets:
        m0 = markets[0]
        log(f"  sample: {m0.get('ticker')} result={m0.get('result')} close={m0.get('close_time')}")
        r2 = http.get(f"{kalshi.BASE}/markets/candlesticks",
                      {"tickers": m0["ticker"], "start_ts": start, "end_ts": end, "interval": 60})
        cs = (r2.json or {}).get("candlesticks") or []
        ncand = sum(len(e.get("candlesticks") or []) for e in cs)
        log(f"kalshi candles settled ticker: http={r2.status} entries={len(cs)} candles={ncand}")
        if cs and cs[0].get("candlesticks"):
            log(f"  candle sample: {cs[0]['candlesticks'][0]}")
    r = http.get(f"{kalshi.BASE}/events", {"series_ticker": "KXNBAGAME", "status": "settled", "limit": 5})
    log(f"kalshi events settled: http={r.status} rows={len((r.json or {}).get('events') or [])}")

    # prop series candidates
    for s in ["KXNBAPTS", "KXNBATHREES", "KXNBATO", "KXNBAPRA", "KXNBADD", "KXNBASTL",
              "KXNBABLK", "KXNBAREBS", "KXNBAASTS", "KXNBAPTSALT"]:
        rr = kalshi.get_series(s)
        log(f"kalshi.series {s}: exists={rr.ok}")

    # season coverage check: how far back do KXNBAGAME markets go?
    for y, m in [(2026, 1), (2025, 10), (2025, 4), (2024, 12)]:
        t0 = time.mktime(time.strptime(f"{y}-{m:02d}-10", "%Y-%m-%d"))
        t1 = t0 + 5 * 86400
        r = http.get(f"{kalshi.BASE}/markets", {"series_ticker": "KXNBAGAME",
                                                "min_close_ts": int(t0), "max_close_ts": int(t1),
                                                "limit": 5})
        n = len((r.json or {}).get("markets") or [])
        log(f"kalshi KXNBAGAME coverage {y}-{m:02d}: rows(5-cap)={n}")

    log("probe4 done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        with open(PATH, "a") as f:
            f.write("\nPROBE_CRASH:\n" + tb + "\n")
        raise
