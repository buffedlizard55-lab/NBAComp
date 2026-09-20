#!/usr/bin/env python
"""Probe #5: Kalshi settled-event enumeration -> per-event markets ->
candlesticks on a real settled market. Also event title shapes for mapping."""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import http  # noqa: E402

PATH = "data/diagnostics.txt"
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def log(s):
    print(s, flush=True)
    with open(PATH, "a") as f:
        f.write(s + "\n")


def get(path, params=None):
    r = http.get(f"{BASE}{path}", params, min_interval=0.35)
    return r


def main():
    with open(PATH, "w") as f:
        f.write("probe5 " + time.strftime("%FT%TZ", time.gmtime()) + "\n")

    # page settled events
    r = get("/events", {"series_ticker": "KXNBAGAME", "status": "settled", "limit": 200})
    js = r.json or {}
    evs = js.get("events") or []
    log(f"events settled page1: http={r.status} rows={len(evs)} cursor={'yes' if js.get('cursor') else 'no'}")
    if evs:
        for e in evs[:3]:
            log(f"  event: ticker={e.get('ticker')} title={e.get('title')!r} "
                f"sub={e.get('sub_title')!r} strike={e.get('strike_date')} "
                f"close={e.get('close_time')}")
        # deepest + cursor walk
        cursor = js.get("cursor")
        seen = len(evs)
        oldest = evs[-1]
        pages = 1
        while cursor and pages < 8:
            r2 = get("/events", {"series_ticker": "KXNBAGAME", "status": "settled",
                                 "limit": 200, "cursor": cursor})
            js2 = r2.json or {}
            evs2 = js2.get("events") or []
            if not evs2:
                break
            oldest = evs2[-1]
            seen += len(evs2)
            cursor = js2.get("cursor")
            pages += 1
            time.sleep(0.3)
        log(f"  walked {pages} pages, seen={seen} oldest={oldest.get('ticker')} "
            f"close={oldest.get('close_time')}")
        # markets of one settled event
        ev0 = evs[0]
        rm = get("/markets", {"event_ticker": ev0["ticker"], "limit": 100})
        ms = (rm.json or {}).get("markets") or []
        log(f"markets of {ev0['ticker']}: rows={len(ms)}")
        for m in ms[:4]:
            log(f"  {m.get('ticker')} title={m.get('title')!r} sub={m.get('market_subtitle')!r} "
                f"result={m.get('result')} yes_bid={m.get('yes_bid')} cap={m.get('cap')}")
        if ms:
            tk = ms[0]["ticker"]
            close_iso = ms[0].get("close_time")
            # candles for the full life of this market
            import datetime as dt
            t_end = int(time.time())
            t_start = t_end - 400 * 86400
            rc = get("/markets/candlesticks", {"tickers": tk, "start_ts": t_start,
                                               "end_ts": t_end, "interval": 1440})
            cs = (rc.json or {}).get("candlesticks") or []
            n = sum(len(e.get("candlesticks") or []) for e in cs)
            log(f"candles(1d) for {tk}: http={rc.status} entries={len(cs)} candles={n}")
            if cs and cs[0].get("candlesticks"):
                log(f"  first={cs[0]['candlesticks'][0]}")
                log(f"  last={cs[0]['candlesticks'][-1]}")
    log("probe5 done")


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
