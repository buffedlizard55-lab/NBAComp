"""Collection orchestration — the only code that touches the network.

Verified transport reality (GitHub Actions runners, 2026-09-20 — see
data/diagnostics.txt and the research log):
- ESPN: both API hosts 403 urllib fingerprints from runner IPs; requests
  fall back to Node-fetch transport automatically (same public data; the
  transport used is recorded in source_status.detail).
- stats.nba.com / data.nba.net / cdn.nba.com: blocked or tarpitted → replaced
  by ESPN box scores + Basketball-Reference verification.
- Kalshi public market data: fully accessible (keyless).
- Basketball-Reference: reachable; monthly pages backfill schedule + finals.

Commands:
  backfill-days        ESPN scoreboard per day (schedule/results/odds)
  backfill-bref-months BRef monthly pages -> games rows (finals + scheduled)
  verify-bref-months   Basketball-Reference monthly score verification
  boxscores            ESPN box scores for a date range (player + team logs)
  daily                schedule/odds/injuries/Kalshi/boxscores catch-up
  kalshi-discovery     probe candidate NBA series; record what exists
  kalshi-backfill      settled events -> markets (recent history walk)
  kalshi-snapshot      open markets + orderbooks (forward prices)
  kalshi-candles       candlesticks for winner markets in a window
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import db, engine, util  # noqa: E402
from nbacomp.sources import bref, espn, kalshi  # noqa: E402

CAP = util.utcnow_iso()


def save_source_status(con, source_id: str, r, detail: str = ""):
    db.insert(con, "source_status", {
        "source_id": source_id, "checked_utc": util.utcnow_iso(),
        "ok": 1 if r.ok else 0, "http_status": r.status,
        "detail": (detail or r.error or "")[:500],
        "sample_hash": util.stable_hash((r.json or {})) if r.ok else None,
    }, replace=True)


# ------------------------------------------------------------------ ESPN

def collect_espn_day(con, day: str, verify: bool = False) -> int:
    r = espn.scoreboard(day)
    save_source_status(con, "espn:scoreboard", r,
                       detail=f"date={day} via={getattr(r, 'transport', '?')}")
    if not r.ok:
        db.log_collection(con, "espn-day", "espn", "fail", f"{day}: {r.error}")
        return 0
    games = espn.parse_scoreboard(r.json)
    for g in games:
        odds = g.pop("_odds", None)
        row = {
            "game_id": g["game_id"], "source": g["source"], "season": g["season"],
            "game_date_et": g["game_date_et"] or "", "tipoff_utc": g["tipoff_utc"],
            "home_team": g["home_team"], "away_team": g["away_team"],
            "home_score": g["home_score"], "away_score": g["away_score"],
            "status": g["status"], "neutral_site": g["neutral_site"],
            "source_updated_utc": None, "captured_utc": CAP, "verified": 0,
        }
        db.insert(con, "games", row, replace=True)
        if odds:
            _store_espn_odds(con, odds)
    db.log_collection(con, "espn-day", "espn", "ok", f"date={day}", rows=len(games))
    return len(games)


def _store_espn_odds(con, odds: dict):
    game_id = odds["game_id"]
    captured = util.utcnow_iso()
    items = []
    if odds.get("total") is not None:
        items.append(("total", f"line {odds['total']:g}", odds["total"], None))
    if odds.get("spread_home") is not None:
        items.append(("spread", f"home {odds['spread_home']:+g}", odds["spread_home"], None))
    if odds.get("ml_home") is not None:
        items.append(("ml", "home", None, odds["ml_home"]))
    if odds.get("ml_away") is not None:
        items.append(("ml", "away", None, odds["ml_away"]))
    for market, selection, line, price in items:
        db.insert(con, "odds_snapshots", {
            "game_id": game_id, "market": market, "selection": selection,
            "line": line, "price": price if price is not None else 0,
            "price_format": "american" if price is not None else "line",
            "source": f"espn:{odds.get('provider', 'unknown')}",
            "source_url": espn.BASE + "/scoreboard",
            "source_updated_utc": odds.get("source_updated_utc"),
            "captured_utc": captured,
        })
    for key, market, sel in (("open_total", "total", "opening total"),
                             ("open_spread_home", "spread", "opening home spread")):
        if odds.get(key) is not None:
            db.insert(con, "odds_snapshots", {
                "game_id": game_id, "market": market, "selection": sel,
                "line": odds[key], "price": 0, "price_format": "line",
                "source": f"espn:{odds.get('provider', 'unknown')} (opening)",
                "source_url": espn.BASE + "/scoreboard",
                "source_updated_utc": None, "captured_utc": captured,
            })


def backfill_days(con, start: str, end: str) -> int:
    d0 = datetime.strptime(start, "%Y%m%d")
    d1 = datetime.strptime(end, "%Y%m%d")
    d = d0
    total = 0
    while d <= d1:
        total += collect_espn_day(con, d.strftime("%Y%m%d"))
        d += timedelta(days=1)
        time.sleep(0.35)
    return total


def verify_bref_months(con, seasons: list[tuple[int, list[str]]]) -> int:
    n = 0
    for year, months in seasons:
        for m in months:
            n += bref.verify_month(con, year, m)
            time.sleep(1.0)
    return n


# ------------------------------------------------------------------ boxscores

def collect_boxscores(con, date_yyyymmdd: str) -> int:
    """Fetch summaries for all FINAL games of an ET date; store player+team logs."""
    day = f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
    games = con.execute(
        "SELECT * FROM games WHERE game_date_et=? AND status='final'", (day,)).fetchall()
    n = 0
    for g in games:
        gid = g["game_id"].split(":", 1)[1]
        r = espn.summary(gid)
        save_source_status(con, "espn:summary", r, detail=gid)
        if not r.ok:
            db.log_collection(con, "boxscore", "espn", "fail", f"{gid}: {r.error}")
            continue
        players = espn.parse_boxscore(r.json)
        teams = espn.parse_team_boxscore(r.json)
        tmeta = {t["team"]: t for t in teams}
        for p in players:
            db.insert(con, "player_gamelogs", {
                "season": p["season"] if p["season"] != "unknown" else g["season"],
                "game_id": g["game_id"],
                "game_date_et": day,
                "player_id": p["player_id"] or f"name:{p['player']}",
                "player": p["player"],
                "team": p["team"],
                "status": p["status"],
                "minutes": p.get("minutes"), "pts": p.get("pts"), "reb": p.get("reb"),
                "ast": p.get("ast"), "stl": p.get("stl"), "blk": p.get("blk"),
                "tov": p.get("tov"), "fg3m": p.get("fg3m"), "fgm": p.get("fgm"),
                "fga": p.get("fga"), "ftm": p.get("ftm"), "fta": p.get("fta"),
                "plus_minus": p.get("plus_minus"),
                "source": "espn:summary", "captured_utc": util.utcnow_iso(),
            }, replace=True)
            n += 1
        for t in teams:
            meta = tmeta.get(t["team"], {})
            opp = [x for x in tmeta if x != t["team"]]
            opp_team = opp[0] if opp else None
            opp_pts = tmeta.get(opp_team, {}).get("score") if opp_team else None
            db.insert(con, "team_gamelogs", {
                "season": t["season"] if t["season"] != "unknown" else g["season"],
                "game_id": g["game_id"],
                "game_date_et": day,
                "team": t["team"], "opp": opp_team,
                "is_home": t.get("is_home") if t.get("is_home") is not None
                else (1 if g["home_team"] == t["team"] else 0),
                "pts": t.get("pts"), "opp_pts": opp_pts, "wl": None,
                "minutes": None,
                "fgm": t.get("fgm"), "fga": t.get("fga"),
                "fg3m": t.get("fg3m"), "fg3a": t.get("fg3a"),
                "ftm": t.get("ftm"), "fta": t.get("fta"),
                "oreb": t.get("oreb"), "dreb": None,
                "reb": t.get("reb"), "ast": t.get("ast"),
                "stl": None, "blk": None, "tov": t.get("tov"), "pf": None,
                "plus_minus": None,
                "source": "espn:summary", "captured_utc": util.utcnow_iso(),
            }, replace=True)
            n += 1
        time.sleep(0.4)
    db.log_collection(con, "boxscores", "espn", "ok", f"{day}", rows=n)
    return n


def collect_injuries(con) -> int:
    r = espn.injuries()
    save_source_status(con, "espn:injuries", r)
    if not r.ok:
        db.log_collection(con, "injuries", "espn", "fail", r.error or "unavailable")
        return 0
    items = espn.parse_injuries(r.json)
    for it in items:
        db.insert(con, "injuries", it)
    db.log_collection(con, "injuries", "espn", "ok", "", rows=len(items))
    return len(items)


# ------------------------------------------------------------------ Kalshi

def kalshi_discovery(con) -> dict:
    """Probe candidate NBA series; record which actually exist."""
    found = {}
    for s in kalshi.CANDIDATE_SERIES:
        r = kalshi.get_series(s)
        found[s] = {"exists": r.ok, "http": r.status}
        save_source_status(con, f"kalshi:series:{s}", r)
        time.sleep(0.15)
    db.insert(con, "meta", {"key": "kalshi_series_discovery",
                            "value": json.dumps(found, default=str),
                            "updated_utc": util.utcnow_iso()}, replace=True)
    db.log_collection(con, "kalshi-discovery", "kalshi", "ok",
                      json.dumps({k: v for k, v in found.items() if v["exists"]}), rows=len(found))
    return found


def _live_series(con) -> list[str]:
    meta = con.execute("SELECT value FROM meta WHERE key='kalshi_series_discovery'").fetchone()
    if not meta:
        kalshi_discovery(con)
        meta = con.execute("SELECT value FROM meta WHERE key='kalshi_series_discovery'").fetchone()
    found = json.loads(meta["value"])
    return [s for s, v in found.items() if v.get("exists")]


def kalshi_snapshot(con) -> int:
    """Refresh OPEN markets (forward prices) + orderbooks for the main series."""
    n = 0
    CAP2 = util.utcnow_iso()
    book_tickers: list[str] = []
    for s in _live_series(con):
        try:
            markets = kalshi.get_markets(s, status="open", max_pages=10)
        except Exception as e:
            db.log_collection(con, "kalshi-snapshot", f"kalshi:{s}", "fail", str(e)[:200])
            continue
        for m in markets:
            try:
                row = kalshi.parse_market(m, CAP2)
            except Exception as e:
                db.log_anomaly(con, "warn", "kalshi-market-parse-error",
                               {"ticker": m.get("ticker"), "err": str(e)[:200]})
                continue
            db.insert(con, "kalshi_markets", row, replace=True)
            n += 1
            if s in ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBA1H", "KXNBAQ1"):
                book_tickers.append(row["ticker"])
        time.sleep(0.3)
    # per-ticker orderbooks (batch endpoint shape unreliable in probes)
    books = 0
    CAP3 = util.utcnow_iso()
    for t in book_tickers[:100]:
        r = kalshi.get_orderbook(t)
        if not r.ok:
            continue
        book = r.json or {}
        ob = book.get("orderbook") or book.get("orderbook_fp") or {}
        # Live shape 2026-09-20: yes_dollars/no_dollars ascending [[price, size]]
        # as DOLLAR STRINGS ("0.5400"). Best bid is the LAST level, not the
        # first — the old code stored level[0] (1c garbage) as yes_bid.
        yes_bids = sorted(_norm_side(ob.get("yes") or ob.get("yes_dollars")))
        no_bids = sorted(_norm_side(ob.get("no") or ob.get("no_dollars")))
        yes_asks = sorted([[100 - p, sz] for p, sz in no_bids]) if no_bids else []
        db.insert(con, "kalshi_orderbooks", {
            "ticker": t, "captured_utc": CAP3,
            "yes_bid": yes_bids[-1][0] if yes_bids else None,
            "yes_ask": yes_asks[0][0] if yes_asks else None,
            "bids": json.dumps(yes_bids), "asks": json.dumps(yes_asks),
        }, replace=True)
        books += 1
        time.sleep(0.15)
    db.log_collection(con, "kalshi-snapshot", "kalshi", "ok",
                      f"open_markets={n} books={books}", rows=n)
    return n


def _norm_side(side) -> list:
    """Normalize one orderbook side to [[cents, size], ...].

    Live shape 2026-09-20: dollar strings (["0.5400", "12.50"]) — int()
    raised ValueError on every level, so books silently stored ZERO levels.
    Strings containing '.' scale x100; plain ints/floats pass through.
    """
    out = []
    for item in side or []:
        try:
            if isinstance(item, dict):
                p, s = item["price"], item.get("size") or 0
            else:
                p, s = item[0], item[1]
            if isinstance(p, str) and "." in p:
                p = int(round(float(p) * 100))
            else:
                p = int(float(p))
            out.append([p, int(float(s))])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def kalshi_backfill(con, max_pages: int = 5, recent_days: int | None = None) -> int:
    """Walk SETTLED events per series (cursor pagination), store their markets.

    Default is deliberately shallow (5 pages x 200 events per series): each
    event costs one markets fetch, so the old 40-page default meant 8000+
    requests per series and never finished inside a workflow run. Pass an
    explicit max_pages (CLI --pages) for deliberate deep history walks.

    This is how historical Kalshi NBA data is obtained: the /markets endpoint
    with a series+status filter returns nothing for settled history; the
    events endpoint does return settled events, and each event's markets carry
    the recorded result. Verified in runner probes (data/diagnostics.txt).

    recent_days: when set, only events dated within the last N days get
    their markets fetched (used by the daily catch-up; the full backfill
    walks everything up to max_pages).

    Runner evidence (probe5, data/diagnostics.txt): settled event rows carry
    title/subtitle but NO "ticker" or "close_time" keys — the event identity
    is "event_ticker" (Kalshi schema), and its date comes from the ticker
    encoding (KXNBAGAME-26JUN13NYKSAS), not close_time.
    """
    total = 0
    CAP2 = util.utcnow_iso()
    cutoff = None
    if recent_days:
        cutoff = util.to_iso(util.parse_iso(util.utcnow_iso()) - timedelta(days=recent_days))[:10]
    for s in _live_series(con):
        cursor = None
        events: list[dict] = []
        for _ in range(max_pages):
            params: dict = {"series_ticker": s, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = kalshi._get("/events", params)
            if not r.ok:
                db.log_collection(con, "kalshi-backfill", f"kalshi:{s}", "fail",
                                  f"events http={r.status}", rows=len(events))
                break
            js = r.json or {}
            batch = js.get("events") or []
            events.extend(batch)
            cursor = js.get("cursor")
            if not cursor:
                break
            if recent_days and batch:
                dates = [engine.parse_event_ticker(e.get("event_ticker") or e.get("ticker") or "")[0]
                         for e in batch]
                dates = [d for d in dates if d]
                if dates and min(dates) < cutoff:
                    break
            time.sleep(0.25)
        n_events = skipped = 0
        for e in events:
            ev_ticker = e.get("event_ticker") or e.get("ticker")
            if not ev_ticker:
                skipped += 1
                continue
            ev_date = engine.parse_event_ticker(ev_ticker)[0]
            if cutoff and ev_date and ev_date < cutoff:
                continue
            ms = kalshi.get_markets_by_event(ev_ticker)
            for m in ms:
                try:
                    row = kalshi.parse_market(m, CAP2)
                except Exception:
                    continue
                row["title"] = row["title"] or e.get("title")
                row["event_ticker"] = row["event_ticker"] or ev_ticker
                db.insert(con, "kalshi_markets", row, replace=True)
            n_events += 1
            total += len(ms)
            time.sleep(0.2)
        db.log_collection(con, "kalshi-backfill", f"kalshi:{s}", "ok",
                          f"events={n_events} skipped_no_ticker={skipped} markets_total={total}",
                          rows=n_events)
    return total


def kalshi_candles_window(con, series_filter: str | None, start_iso: str, end_iso: str,
                          interval: int = 60) -> int:
    """Fetch candlesticks for winner markets closing inside a window."""
    rows = con.execute(
        "SELECT ticker FROM kalshi_markets WHERE (? IS NULL OR series_ticker=?) "
        "AND close_time IS NOT NULL AND close_time >= ? AND close_time <= ?",
        (series_filter, series_filter, start_iso, end_iso)).fetchall()
    tickers = [r["ticker"] for r in rows]
    start = int(util.parse_iso(start_iso).timestamp())
    end = int(util.parse_iso(end_iso).timestamp())
    got = kalshi.get_candlesticks(tickers, start * 1000, end * 1000, interval)
    n = 0
    C = util.utcnow_iso()
    for entry in got:
        t = entry.get("market_ticker")
        for c in entry.get("candlesticks") or []:
            # Live shape 2026-09-20: {"end_period_ts": 1788..., "price":
            # {"open_dollars": "0.38", ...}, "volume_fp": "707.69"}. The old
            # keys (ts/open/high/...) never existed -> every row was NULLs.
            ts = c.get("end_period_ts") or c.get("ts") or c.get("timestamp_ms")
            price = c.get("price") or {}
            db.insert(con, "kalshi_candles", {
                "ticker": t, "interval": interval,
                "ts_utc": _ms_iso(ts),
                "open": _dollars_to_cents(price.get("open_dollars"), price.get("open")),
                "high": _dollars_to_cents(price.get("high_dollars"), price.get("high")),
                "low": _dollars_to_cents(price.get("low_dollars"), price.get("low")),
                "close": _dollars_to_cents(price.get("close_dollars"), price.get("close")),
                "volume": _contracts(c.get("volume_fp"), c.get("volume")),
                "captured_utc": C,
            })
            n += 1
    db.log_collection(con, "kalshi-candles", "kalshi", "ok" if n else "empty",
                      f"tickers={len(tickers)} candles={n} window={start_iso}..{end_iso}", rows=n)
    return n


def _ms_iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        v = float(ts)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _dollars_to_cents(*vals) -> int | None:
    """First present value as integer cents (dollar strings x100)."""
    for v in vals:
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if isinstance(v, str) and "." in v:
            return int(round(f * 100))
        return int(f)
    return None


def _contracts(*vals) -> int | None:
    """First present value as integer contracts (never rescaled)."""
    for v in vals:
        if v is None or v == "":
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return None


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(description="NBAComp data collection")
    ap.add_argument("command", choices=["backfill-days", "backfill-bref-months", "verify-bref-months",
                                        "boxscores", "daily", "kalshi-discovery", "kalshi-backfill",
                                        "kalshi-snapshot", "kalshi-candles"])
    ap.add_argument("--start", help="YYYYMMDD or ISO")
    ap.add_argument("--end", help="inclusive")
    ap.add_argument("--months", help="e.g. 2026:october,2026:november (season-end-year:month)")
    ap.add_argument("--series", default=None)
    ap.add_argument("--pages", type=int, default=None,
                    help="kalshi-backfill event pages per series (default 5)")
    args = ap.parse_args()

    with db.get_db() as con:
        if args.command == "backfill-days" and args.start and args.end:
            print(f"games collected: {backfill_days(con, args.start, args.end)}")
        elif args.command == "verify-bref-months" and args.months:
            # tokens are season-end-year:monthname, e.g. 2026:january
            # (Oct-Dec of calendar year Y belong to season ending Y+1)
            print(f"verified: {verify_bref_months(con, _parse_month_tokens(args.months))}")
        elif args.command == "backfill-bref-months" and args.months:
            tot = {"parsed": 0, "inserted": 0, "merged": 0, "skipped": 0}
            for y, m in _parse_month_tokens(args.months):
                st = bref.backfill_month(con, y, m)
                for k in tot:
                    tot[k] += st.get(k, 0)
                time.sleep(1.0)
            print(f"bref backfill: {tot}")
        elif args.command == "boxscores" and args.start:
            d0 = datetime.strptime(args.start, "%Y%m%d")
            d1 = datetime.strptime(args.end, "%Y%m%d") if args.end else d0
            d = d0
            n = 0
            while d <= d1:
                n += collect_boxscores(con, d.strftime("%Y%m%d"))
                d += timedelta(days=1)
            print(f"boxscore rows: {n}")
        elif args.command == "daily":
            now = datetime.now(timezone.utc)
            d = now - timedelta(days=2)
            for _ in range(11):
                collect_espn_day(con, d.strftime("%Y%m%d"))
                d += timedelta(days=1)
            collect_injuries(con)
            kalshi_snapshot(con)
            # recent settled events (settlement ground truth for forward bets)
            kalshi_backfill(con, max_pages=2)
            # yesterday's boxscores (player logs + rolling features)
            y = now - timedelta(days=1)
            collect_boxscores(con, y.strftime("%Y%m%d"))
            # BRef: backfill + verify the current month. Oct-Dec belong to the
            # season ending NEXT year (2026-10 -> season-end 2027).
            seas_end = now.year + (1 if now.month >= 10 else 0)
            bref.backfill_month(con, seas_end, _month_name(now.month))
            bref.verify_month(con, seas_end, _month_name(now.month))
        elif args.command == "kalshi-discovery":
            found = kalshi_discovery(con)
            print(json.dumps({k: v for k, v in found.items() if v["exists"]}, indent=2))
        elif args.command == "kalshi-backfill":
            kw = {"max_pages": args.pages} if args.pages else {}
            print(f"markets stored: {kalshi_backfill(con, **kw)}")
        elif args.command == "kalshi-snapshot":
            print(f"open markets: {kalshi_snapshot(con)}")
        elif args.command == "kalshi-candles":
            end = args.end or util.utcnow_iso()
            start = util.parse_iso(args.start) if args.start else util.parse_iso(end) - timedelta(days=30)
            n = kalshi_candles_window(con, args.series, util.to_iso(start), end)
            print(f"candles: {n}")


def _parse_month_tokens(months: str) -> list[tuple[int, str]]:
    items = []
    for token in months.split(","):
        y, m = token.strip().split(":")
        items.append((int(y), m.strip().lower()))
    return items


def _month_name(m: int) -> str:
    return ["january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december"][m - 1]


if __name__ == "__main__":
    main()
