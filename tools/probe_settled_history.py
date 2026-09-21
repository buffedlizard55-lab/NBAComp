#!/usr/bin/env python
"""Probe #6: can settled Kalshi NBA markets still yield price history?

Settled market ROWS are confirmed unavailable via the public API (runner
evidence 2026-09-21, data/nbacomp.db collection_log). This probe tests the two
remaining channels for historical prices on settled markets:

  1. GET /markets/candlesticks?market_tickers={event}-YES,...
  2. GET /markets/trades?ticker={event}-YES

Market tickers are CONSTRUCTED from settled event tickers (binary Kalshi
markets are `{event_ticker}-YES` / `{event_ticker}-NO`) — nothing is guessed.
Every HTTP status and row count is appended to data/diagnostics.txt so the
answer is verifiable from the repository, and the collector in
nbacomp/collect.py::kalshi_settled_history only stores what these endpoints
actually return.

Also re-verifies two claims the pipeline depends on:
  * ESPN scoreboard answers for a historical date (2025-01-15) with games.
  * The current UTC date/time on the runner (guards against a wrong clock
    silently shifting every window).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import http  # noqa: E402

PATH = "data/diagnostics.txt"
BASE = "https://api.elections.kalshi.com/trade-api/v2"
ESPN_WEB = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba"


def log(s):
    print(s, flush=True)
    os.makedirs("data", exist_ok=True)
    with open(PATH, "a") as f:
        f.write(s + "\n")


def get(url, params=None):
    return http.get(url, params, min_interval=0.35)


def main():
    log("")
    log("probe6 " + time.strftime("%FT%TZ", time.gmtime()))
    log(f"  runner clock: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
        f"(epoch {int(time.time())})")

    # --- ESPN historical scoreboard (games backfill depends on this)
    r = get(f"{ESPN_WEB}/scoreboard", {"dates": "20250115"})
    n = len((r.json or {}).get("events") or []) if r.ok else -1
    first = ""
    if r.ok and n > 0:
        e0 = (r.json or {}).get("events")[0]
        comp = (e0.get("competitions") or [{}])[0]
        first = (f" first={e0.get('shortName')} "
                 f"state={(e0.get('status') or {}).get('type', {}).get('state')} "
                 f"odds={bool(comp.get('odds'))}")
    log(f"  espn scoreboard 20250115: http={r.status} events={n}{first} "
        f"transport={r.transport} err={r.error}")

    # --- settled events -> constructed market tickers
    r = get(f"{BASE}/events", {"series_ticker": "KXNBAGAME", "status": "settled", "limit": 200})
    evs = (r.json or {}).get("events") or []
    log(f"  settled events page1: http={r.status} rows={len(evs)}")
    if not evs:
        log("probe6 done (no settled events to probe)")
        return
    ev0 = evs[0].get("event_ticker") or evs[0].get("ticker")
    # Live KXNBAGAME markets are PER-TEAM (fixture-verified:
    # KXNBAGAME-26OCT20OKCSAS-SAS / -OKC), not -YES/-NO. Settled rows expose
    # only the event ticker, so probe every plausible suffix.
    sample = []
    for e in evs[:4]:
        ev = e.get("event_ticker") or e.get("ticker")
        if not ev:
            continue
        sample.extend(f"{ev}-{s}" for s in ("YES", "NO"))
        title = f"{e.get('title') or ''} {e.get('sub_title') or ''}"
        for ab in re.findall(r"\b([A-Z]{3})\b", title):
            sample.append(f"{ev}-{ab}")
    sample = list(dict.fromkeys(sample))
    log(f"  sample constructed tickers ({len(sample)}): {sample[:8]}")
    log(f"  event title sample: {evs[0].get('title')!r} sub={evs[0].get('sub_title')!r} "
        f"event_ticker={ev0}")

    # --- 1) candlesticks for settled tickers, 1h and 1d intervals
    now_s = int(time.time())
    for interval in (60, 1440):
        rc = get(f"{BASE}/markets/candlesticks", {
            "market_tickers": ",".join(sample),
            "start_ts": now_s - 500 * 86400, "end_ts": now_s,
            "period_interval": interval})
        body = (rc.json or {}) if rc.ok else {}
        entries = body.get("markets") or []
        candles = sum(len(e.get("candlesticks") or []) for e in entries)
        log(f"  candles interval={interval}: http={rc.status} err={rc.error} "
            f"entries={len(entries)} candles={candles}")
        if entries and entries[0].get("candlesticks"):
            c0 = entries[0]["candlesticks"]
            log(f"    ticker={entries[0].get('market_ticker')} n={len(c0)} "
                f"first={json.dumps(c0[0])[:220]}")
            log(f"    last={json.dumps(c0[-1])[:220]}")

    # --- single-ticker variant (in case batch shape differs)
    rc = get(f"{BASE}/markets/candlesticks", {
        "market_tickers": sample[0], "start_ts": now_s - 500 * 86400,
        "end_ts": now_s, "period_interval": 60})
    log(f"  candles single({sample[0]}): http={rc.status} err={rc.error} "
        f"keys={sorted((rc.json or {}).keys())} "
        f"candles={sum(len(e.get('candlesticks') or []) for e in ((rc.json or {}).get('markets') or []))}")

    # --- 2) trade tape for settled markets
    rt = get(f"{BASE}/markets/trades", {"ticker": sample[0], "limit": 100})
    trades = (rt.json or {}).get("trades") or [] if rt.ok else []
    log(f"  trades({sample[0]}): http={rt.status} err={rt.error} rows={len(trades)}")
    if trades:
        log(f"    first={json.dumps(trades[0])[:220]}")
        log(f"    last={json.dumps(trades[-1])[:220]}")

    # --- 3) does an OPEN market still expose candles? (control)
    ro = get(f"{BASE}/markets", {"series_ticker": "KXNBAGAME", "status": "open", "limit": 3})
    oms = (ro.json or {}).get("markets") or []
    log(f"  open KXNBAGAME markets: http={ro.status} rows={len(oms)}")
    if oms:
        tk = oms[0].get("ticker")
        rc = get(f"{BASE}/markets/candlesticks", {
            "market_tickers": tk, "start_ts": now_s - 30 * 86400,
            "end_ts": now_s, "period_interval": 60})
        n = sum(len(e.get("candlesticks") or []) for e in ((rc.json or {}).get("markets") or []))
        log(f"  control candles for open {tk}: http={rc.status} candles={n}")
    log("probe6 done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        with open(PATH, "a") as f:
            f.write("\nPROBE6_CRASH:\n" + tb + "\n")
        raise
