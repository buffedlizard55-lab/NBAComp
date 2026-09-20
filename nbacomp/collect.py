"""Collection orchestration — the only code that touches the network.

Commands:
  backfill-seasons  ESPN scoreboard per day for seasons + verification vs NBA.com
  daily             upcoming schedule+odds, injuries, Kalshi snapshot, NBA stats refresh
  kalshi-discovery  probe candidate NBA series and record which exist
  kalshi-candles    candlesticks for Kalshi NBA markets in a time window

Every fetch logs to collection_log / source_status. Failures are recorded,
never silently swallowed, and never filled with invented data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import db, util  # noqa: E402
from nbacomp.sources import espn, kalshi, nba  # noqa: E402

CAP = util.utcnow_iso()


def save_source_status(con, source_id: str, r: "http.HttpResult", detail: str = ""):
    db.insert(con, "source_status", {
        "source_id": source_id, "checked_utc": util.utcnow_iso(),
        "ok": 1 if r.ok else 0, "http_status": r.status,
        "detail": (detail or r.error or "")[:500],
        "sample_hash": util.stable_hash((r.json or {})) if r.ok else None,
    }, replace=True)


# ------------------------------------------------------------------ ESPN

def collect_espn_day(con, day: str, verify: bool = True) -> int:
    """day = YYYYMMDD (UTC day scanned by ESPN; returns games upserted)."""
    r = espn.scoreboard(day)
    save_source_status(con, "espn:scoreboard", r, detail=f"date={day}")
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

    if verify and games:
        mm, dd, yyyy = day[4:6], day[6:8], day[0:4]
        rv = nba.scoreboard_v2(f"{mm}/{dd}/{yyyy}")
        save_source_status(con, "nba:scoreboardv2", rv, detail=f"date={day}")
        if rv.ok:
            rows = nba.parse_rows(rv.json)
            by_teams = {}
            for row in rows:
                key = (str(row.get("home_team_abbreviation") or row.get("home_team_abbr") or ""),
                       str(row.get("visitor_team_abbreviation") or row.get("visitor_team_abbr") or ""))
                by_teams[key] = row
            for g in games:
                if g["status"] != "final":
                    continue
                row = by_teams.get((g["home_team"], g["away_team"]))
                if row is None:  # try reversed key variants
                    for (h, a), v in by_teams.items():
                        if a == g["home_team"] and h == g["away_team"]:
                            row = v
                            break
                if row is None:
                    continue
                hs, as_ = row.get("home_score"), row.get("visitor_score") or row.get("away_score")
                if hs is None or as_ is None:
                    continue
                match = (int(hs) == g["home_score"] and int(as_) == g["away_score"])
                status = "match" if match else "mismatch"
                db.insert(con, "verifications", {
                    "checked_utc": util.utcnow_iso(),
                    "claim": f"final score {g['game_id']} {g['away_team']}@{g['home_team']}",
                    "primary_source": "espn", "primary_value": f"{g['away_score']}-{g['home_score']}",
                    "secondary_source": "nba.com", "secondary_value": f"{as_}-{hs}",
                    "status": status,
                    "discrepancy": None if match else "sources disagree",
                })
                if match:
                    con.execute("UPDATE games SET verified=1, verified_against='nba.com' WHERE game_id=?",
                                (g["game_id"],))
                else:
                    db.log_anomaly(con, "critical", "score-mismatch", {
                        "game_id": g["game_id"], "espn": [g["away_score"], g["home_score"]],
                        "nba": [as_, hs]})
        else:
            db.log_collection(con, "verify-day", "nba.com", "fail", f"{day}: {rv.error}")
    return len(games)


def _store_espn_odds(con, odds: dict):
    game_id = odds["game_id"]
    captured = util.utcnow_iso()
    items = []
    if odds.get("total") is not None:
        items.append(("total", f"over {odds['total']}", odds["total"], None, None))
    if odds.get("spread_home") is not None:
        items.append(("spread", f"home {odds['spread_home']:+g}", odds["spread_home"], None, None))
    if odds.get("ml_home") is not None:
        items.append(("ml", "home", None, odds["ml_home"], None))
    if odds.get("ml_away") is not None:
        items.append(("ml", "away", None, odds["ml_away"], None))
    for market, selection, line, price, _ in items:
        db.insert(con, "odds_snapshots", {
            "game_id": game_id, "market": market, "selection": selection,
            "line": line, "price": price if price is not None else 0,
            "price_format": "american" if price is not None else "line",
            "source": f"espn:{odds.get('provider', 'unknown')}",
            "source_url": espn.BASE + "/scoreboard",
            "source_updated_utc": odds.get("source_updated_utc"),
            "captured_utc": captured,
        })
    # store open lines as separate snapshot rows when present
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


def backfill_days(con, start: str, end: str):
    d0 = datetime.strptime(start, "%Y%m%d")
    d1 = datetime.strptime(end, "%Y%m%d")
    d = d0
    total = 0
    while d <= d1:
        total += collect_espn_day(con, d.strftime("%Y%m%d"), verify=True)
        d += timedelta(days=1)
        time.sleep(0.4)
    return total


# ------------------------------------------------------------------ Kalshi

def kalshi_discovery(con) -> dict:
    """Probe candidate NBA series; record which actually exist."""
    found = {}
    for s in kalshi.CANDIDATE_SERIES:
        r = kalshi.get_series(s)
        ok = r.ok
        found[s] = {"exists": ok, "http": r.status}
        save_source_status(con, f"kalshi:series:{s}", r)
        time.sleep(0.2)
    # Also scan open events of every found series to count live markets
    for s, info in found.items():
        if not info["exists"]:
            continue
        markets = kalshi.get_markets(s, status="open", max_pages=10)
        events = kalshi.get_events(s, status="open", max_pages=10)
        info["open_markets"] = len(markets)
        info["open_events"] = len(events)
        CAP2 = util.utcnow_iso()
        for m in markets:
            row = kalshi.parse_market(m, CAP2)
            db.insert(con, "kalshi_markets", row, replace=True)
    db.insert(con, "meta", {"key": "kalshi_series_discovery",
                            "value": json.dumps(found, default=str),
                            "updated_utc": util.utcnow_iso()}, replace=True)
    db.log_collection(con, "kalshi-discovery", "kalshi", "ok",
                      json.dumps({k: v for k, v in found.items() if v["exists"]}), rows=len(found))
    return found


def kalshi_snapshot(con) -> int:
    """Refresh open+settled NBA markets we track; capture orderbooks; store quotes."""
    meta = con.execute("SELECT value FROM meta WHERE key='kalshi_series_discovery'").fetchone()
    if not meta:
        kalshi_discovery(con)
        meta = con.execute("SELECT value FROM meta WHERE key='kalshi_series_discovery'").fetchone()
    found = json.loads(meta["value"])
    live_series = [s for s, v in found.items() if v.get("exists")]
    n = 0
    CAP2 = util.utcnow_iso()
    book_tickers: list[str] = []
    for s in live_series:
        for status in ("open", "settled"):
            try:
                markets = kalshi.get_markets(s, status=status, max_pages=30)
            except Exception as e:
                db.log_collection(con, "kalshi-snapshot", f"kalshi:{s}", "fail",
                                  f"status={status}: {e}")
                continue
            if not markets:
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
                if row["status"] == "active":
                    book_tickers.append(row["ticker"])
            time.sleep(0.3)
    # orderbooks for active markets (observability + execution realism)
    got = kalshi.get_orderbooks(book_tickers[:300])
    CAP3 = util.utcnow_iso()
    books_stored = 0
    for ob in got:
        try:
            t = ob.get("market_ticker") or ob.get("ticker")
            if not t:
                continue
            book = ob.get("orderbook") or {}
            yes_bids = _norm_book_side(book.get("yes"))
            no_bids = _norm_book_side(book.get("no"))
            yes_asks = [[100 - p, sz] for p, sz in reversed(no_bids)] if no_bids else \
                _norm_book_side(book.get("yes_ask"))
            db.insert(con, "kalshi_orderbooks", {
                "ticker": t, "captured_utc": CAP3,
                "yes_bid": yes_bids[0][0] if yes_bids else None,
                "yes_ask": yes_asks[0][0] if yes_asks else None,
                "bids": json.dumps(yes_bids), "asks": json.dumps(yes_asks),
            }, replace=True)
            books_stored += 1
        except Exception as e:
            db.log_anomaly(con, "warn", "orderbook-parse-error",
                           {"ticker": ob.get("market_ticker"), "err": str(e)[:200]})
    db.log_collection(con, "kalshi-snapshot", "kalshi", "ok",
                      f"markets={n} books={books_stored}", rows=n)
    return n


def _norm_book_side(side) -> list:
    """Normalize Kalshi order-book side to [[price_cents, size], ...].

    The API has used both list pairs and dict entries ({price, size}); handle
    both defensively and record nothing rather than crash.
    """
    out = []
    for item in side or []:
        try:
            if isinstance(item, dict):
                out.append([int(item["price"]), int(item.get("size") or 0)])
            else:
                out.append([int(item[0]), int(item[1])])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


def kalshi_candles_window(con, series_filter: str | None, start_iso: str, end_iso: str,
                          interval: int = 60) -> int:
    """Fetch candlesticks for NBA markets active/settled inside a window."""
    start = util.parse_iso(start_iso)
    end = util.parse_iso(end_iso)
    rows = con.execute(
        "SELECT ticker FROM kalshi_markets WHERE (? IS NULL OR series_ticker=?) "
        "AND close_time IS NOT NULL", (series_filter, series_filter)).fetchall()
    tickers = [r["ticker"] for r in rows]
    got = kalshi.get_candlesticks(tickers, util.ms(start), util.ms(end), interval)
    n = 0
    C = util.utcnow_iso()
    for entry in got:
        t = entry.get("market_ticker")
        for c in entry.get("candlesticks") or []:
            ts = c.get("ts") or (c.get("timestamp_ms"))
            price = c.get("price") or {}
            db.insert(con, "kalshi_candles", {
                "ticker": t, "interval": interval,
                "ts_utc": _ms_iso(ts),
                "open": price.get("open"), "high": price.get("high"),
                "low": price.get("low"), "close": price.get("close"),
                "volume": c.get("volume"),
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


# ------------------------------------------------------------------ injuries

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


# ------------------------------------------------------------------ NBA stats

def collect_nba_stats(con, seasons: list[str]) -> int:
    n = 0
    for season in seasons:
        for measure in ("Base", "Advanced", "FourFactors"):
            r = nba.leaguedashteamstats(season, measure=measure)
            save_source_status(con, f"nba:teamstats:{measure}", r, detail=season)
            if r.ok:
                for row in nba.parse_rows(r.json):
                    team = str(row.get("team_abbreviation") or row.get("team_abbr") or "")
                    if not team:
                        continue
                    db.insert(con, "team_season_stats", {
                        "season": season, "team": team, "measure": measure,
                        "stats_json": json.dumps(row, default=str),
                        "source": "stats.nba.com", "captured_utc": util.utcnow_iso(),
                    }, replace=True)
                    n += 1
            else:
                db.log_collection(con, "nba-teamstats", f"stats.nba.com:{measure}", "fail",
                                  f"{season}: {r.error}")
        r = nba.teamgamelogs(season)
        save_source_status(con, "nba:teamgamelogs", r, detail=season)
        if r.ok:
            rows = nba.parse_rows(r.json)
            for row in rows:
                gid = row.get("game_id")
                if not gid:
                    continue
                db.insert(con, "team_gamelogs", {
                    "season": season, "game_id": f"nba:{gid}",
                    "game_date_et": str(row.get("game_date") or "")[:10],
                    "team": row.get("team_abbreviation"),
                    "opp": row.get("matchup", "").split(" vs. ")[-1].split(" @ ")[-1]
                    if row.get("matchup") else None,
                    "is_home": 0 if " @ " in (row.get("matchup") or "") else 1,
                    "pts": row.get("pts"), "opp_pts": None, "wl": row.get("wl"),
                    "minutes": row.get("min"),
                    "fgm": row.get("fgm"), "fga": row.get("fga"),
                    "fg3m": row.get("fg3m"), "fg3a": row.get("fg3a"),
                    "ftm": row.get("ftm"), "fta": row.get("fta"),
                    "oreb": row.get("oreb"), "dreb": row.get("dreb"), "reb": row.get("reb"),
                    "ast": row.get("ast"), "stl": row.get("stl"), "blk": row.get("blk"),
                    "tov": row.get("tov"), "pf": row.get("pf"),
                    "plus_minus": row.get("plus_minus"),
                    "source": "stats.nba.com", "captured_utc": util.utcnow_iso(),
                }, replace=True)
                n += 1
        else:
            db.log_collection(con, "nba-gamelogs", "stats.nba.com", "fail",
                              f"{season}: {r.error}")
        r = nba.leaguehustlestatsteam(season)
        save_source_status(con, "nba:hustle", r, detail=season)
        if r.ok:
            for row in nba.parse_rows(r.json):
                team = str(row.get("team_abbreviation") or "")
                if not team:
                    continue
                db.insert(con, "team_season_stats", {
                    "season": season, "team": team, "measure": "Hustle",
                    "stats_json": json.dumps(row, default=str),
                    "source": "stats.nba.com", "captured_utc": util.utcnow_iso(),
                }, replace=True)
                n += 1
    return n


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(description="NBAComp data collection")
    ap.add_argument("command", choices=["backfill-days", "daily", "kalshi-discovery",
                                        "kalshi-candles", "kalshi-settled-window",
                                        "injuries", "nba-stats"])
    ap.add_argument("--start", help="YYYYMMDD")
    ap.add_argument("--end", help="YYYYMMDD inclusive")
    ap.add_argument("--seasons", default="2023-24,2024-25,2025-26,2026-27")
    ap.add_argument("--series", default=None)
    args = ap.parse_args()

    with db.get_db() as con:
        if args.command == "backfill-days" and args.start and args.end:
            n = backfill_days(con, args.start, args.end)
            print(f"games collected: {n}")
        elif args.command == "daily":
            now = datetime.now(timezone.utc)
            # scan a UTC window that covers the next 7 days of ET game dates
            d = now - timedelta(days=1)
            for _ in range(9):
                collect_espn_day(con, d.strftime("%Y%m%d"), verify=True)
                d += timedelta(days=1)
            collect_injuries(con)
            kalshi_snapshot(con)
            seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
            collect_nba_stats(con, seasons)
        elif args.command == "kalshi-discovery":
            found = kalshi_discovery(con)
            print(json.dumps({k: v for k, v in found.items() if v["exists"]}, indent=2))
        elif args.command == "kalshi-candles":
            end = util.utcnow_iso()
            start = args.start and util.parse_iso(args.start) or (util.parse_iso(end) - timedelta(days=30))
            n = kalshi_candles_window(con, args.series, util.to_iso(start), end)
            print(f"candles: {n}")
        elif args.command == "kalshi-settled-window" and args.start:
            end = args.end or util.utcnow_iso()
            n = kalshi_settled_window(con, args.start, end)
            print(f"settled markets loaded: {n}")
        elif args.command == "injuries":
            print(f"injuries: {collect_injuries(con)}")
        elif args.command == "nba-stats":
            seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
            print(f"rows: {collect_nba_stats(con, seasons)}")


if __name__ == "__main__":
    main()
