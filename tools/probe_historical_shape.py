#!/usr/bin/env python
"""Probe #7: what does historical data actually contain?

Three questions that decide whether honest backtests are possible at all, each
answered with a real HTTP status and a real row count appended to
data/diagnostics.txt (never assumed):

  A. Does ESPN's scoreboard for a PAST date still carry bookmaker odds
     (provider / spread / over-under / moneylines)? If yes, the resumable ESPN
     backfill is collecting real historical prices and totals/spread/ML
     backtests become possible. If no, only forward prices will ever exist.

  B. Does Basketball-Reference's monthly page expose each game's start time,
     and under which `data-stat` name? (The parser already reads it for the
     2024-26 bootstrap — 2643 games landed with tipoff_utc — this pins the
     field name so a BRef markup change is detected rather than silently
     dropping every tipoff.)

  C. Is a BRef boxscore ID the same ID ESPN's summary endpoint accepts? BRef
     links /boxscores/202410220BOS.html; if `summary?event=202410220BOS`
     returns that game's box score, then box scores for the two seasons the
     BRef bootstrap loaded can be fetched without waiting for the ESPN
     schedule walk to reach them.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import http  # noqa: E402
from nbacomp.sources import espn  # noqa: E402

PATH = "data/diagnostics.txt"
BREF = "https://www.basketball-reference.com"


def log(s):
    print(s, flush=True)
    os.makedirs("data", exist_ok=True)
    with open(PATH, "a") as f:
        f.write(s + "\n")


def main():
    log("")
    log("probe7 " + time.strftime("%FT%TZ", time.gmtime()))

    # --- A. historical ESPN scoreboard odds
    for day in ("20260115", "20260613", "20250115", "20241022"):
        r = espn.scoreboard(day)
        if not r.ok:
            log(f"  espn {day}: http={r.status} err={r.error} transport={r.transport}")
            continue
        events = (r.json or {}).get("events") or []
        with_odds = 0
        sample = ""
        for e in events:
            comp = (e.get("competitions") or [{}])[0]
            odds = comp.get("odds") or []
            if odds:
                with_odds += 1
                if not sample:
                    o = odds[0]
                    sample = (f" provider={(o.get('provider') or {}).get('name')} "
                              f"details={o.get('details')} ou={o.get('overUnder')} "
                              f"spread={o.get('spread')} "
                              f"ml_h={(o.get('homeTeamOdds') or {}).get('moneyLine')} "
                              f"ml_a={(o.get('awayTeamOdds') or {}).get('moneyLine')} "
                              f"open_ou={(o.get('open') or {}).get('overUnder')}")
        state = ((events[0].get("status") or {}).get("type", {}).get("state")
                 if events else None)
        log(f"  espn {day}: http={r.status} events={len(events)} state={state} "
            f"events_with_odds={with_odds}{sample}")
        time.sleep(0.4)

    # --- B + C. BRef monthly page shape and boxscore ID
    url = f"{BREF}/leagues/NBA_2025_games-october.html"
    r = http.get(url, min_interval=1.2)
    if not r.ok_body:
        log(f"  bref {url}: http={r.status} err={r.error}")
        return
    html = r.body.decode("utf-8", "replace")
    log(f"  bref october-2024 page: http={r.status} bytes={len(html)}")
    stats = sorted(set(re.findall(r'data-stat="([a-z_]+)"', html)))
    log(f"  bref data-stat names: {stats}")
    m = re.search(r"<tr[^>]*>.*?</tr>", html[html.find('data-stat="visitor_team_name"') - 2000:], re.S)
    box = re.findall(r'/boxscores/(\d{8})0([A-Z]{3})\.html', html)
    log(f"  bref boxscore links: {len(box)} first={box[:3]}")
    starts = re.findall(r'data-stat="(game_start_time|start_time|time)"[^>]*>([^<]{0,20})<', html)
    log(f"  bref start-time cells: {starts[:5]}")

    if box:
        gid = f"{box[0][0]}0{box[0][1]}"
        r2 = espn.summary(gid)
        ok = r2.ok
        teams = ""
        if ok:
            parsed = espn.parse_team_boxscore(r2.json)
            teams = f" teams={[(t.get('team'), t.get('pts')) for t in parsed]}"
            players = espn.parse_boxscore(r2.json)
            teams += f" player_rows={len(players)}"
        log(f"  espn summary({gid}) [from bref link]: http={r2.status} ok={ok}{teams} "
            f"err={r2.error}")
    log("probe7 done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        with open(PATH, "a") as f:
            f.write("\nPROBE7_CRASH:\n" + tb + "\n")
        raise
