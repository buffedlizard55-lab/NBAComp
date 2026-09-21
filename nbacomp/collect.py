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


def espn_forward_window(con, days_ahead: int = 42) -> dict:
    """Collect UPCOMING scheduled games (tipoffs) from ESPN's scoreboard.

    probe7 (2026-09-21T02:29:20Z, data/diagnostics.txt) established that a past
    ESPN scoreboard returns games with `state=post` and **no odds at all**
    (20260115: 9 events, 0 with odds; 20260613: 1 event, 0 with odds; 20250115:
    11/0; 20241022: 2/0). Historical prices therefore cannot come from ESPN —
    but *future* dates do carry real tipoffs and, closer to tip, real lines.

    The daily job used to look only 8 days ahead, so the 2026-10-20 openers —
    the only games with live Kalshi markets — had no game row at all and the
    forward engine had nothing to join them to. This walks 42 days ahead.

    Existing rows are never overwritten with an empty future row: Basketball-
    Reference rows carry final scores and verified tipoffs, and a scheduled
    ESPN row for the same matchup must not blank them.
    """
    stats = {"days": 0, "inserted": 0, "skipped": 0, "odds": 0}
    today = datetime.now(timezone.utc)
    for i in range(1, days_ahead + 1):
        day = (today + timedelta(days=i)).strftime("%Y%m%d")
        r = espn.scoreboard(day)
        save_source_status(con, "espn:scoreboard", r, detail=f"date={day} forward")
        if not r.ok:
            db.log_collection(con, "espn-forward", "espn", "fail", f"{day}: {r.error}")
            continue
        for g in espn.parse_scoreboard(r.json):
            odds = g.pop("_odds", None)
            have = con.execute("SELECT game_id FROM games WHERE game_date_et=? "
                               "AND away_team=? AND home_team=?",
                               (g["game_date_et"], g["away_team"], g["home_team"])).fetchone()
            if have:
                stats["skipped"] += 1
            else:
                db.insert(con, "games", {
                    "game_id": g["game_id"], "source": g["source"], "season": g["season"],
                    "game_date_et": g["game_date_et"] or "", "tipoff_utc": g["tipoff_utc"],
                    "home_team": g["home_team"], "away_team": g["away_team"],
                    "home_score": g["home_score"], "away_score": g["away_score"],
                    "status": g["status"], "neutral_site": g["neutral_site"],
                    "source_updated_utc": None, "captured_utc": util.utcnow_iso(),
                    "verified": 0,
                }, replace=True)
                stats["inserted"] += 1
            if odds:
                _store_espn_odds(con, odds)
                stats["odds"] += 1
        stats["days"] += 1
        time.sleep(0.35)
    db.log_collection(con, "espn-forward", "espn", "ok",
                      f"days={stats['days']} inserted={stats['inserted']} "
                      f"skipped_existing={stats['skipped']} odds_rows={stats['odds']}",
                      rows=stats["inserted"])
    return stats


# Oldest ESPN scoreboard date this project will walk back to. 2023-10 covers
# the 2023-24 and 2024-25 seasons plus the 2024 offseason; going deeper buys
# nothing for the current backtest windows and costs a request per day.
ESPN_BACKFILL_FLOOR = "20231001"
BACKFILL_CURSOR_KEY = "espn_backfill_next_day"


def _merge_espn_game_row(con, row: dict) -> str:
    """Insert one ESPN game row, adopting an existing row for the same game.

    The BRef bootstrap loads a whole season as `bref:{date}-{away}-{home}`;
    the ESPN walk later reaches the same games as `espn:{id}`. Inserting both
    created duplicate rows for one real game (run 35553997534: 5
    `duplicate-game` warnings), and the BRef-only rows could never be joined
    to box scores or odds because their id is not an ESPN event id.

    When a row already exists for the same (date, away, home), the ESPN
    identity wins: the row is re-keyed in place, `bets` are re-pointed, and
    the earlier verification flag is preserved. Returns the action taken.
    """
    existing = con.execute(
        "SELECT game_id, source, verified FROM games WHERE game_date_et=? "
        "AND away_team=? AND home_team=?",
        (row["game_date_et"], row["away_team"], row["home_team"])).fetchall()
    same = [e for e in existing if e["game_id"] == row["game_id"]]
    if same:
        db.insert(con, "games", row, replace=True)
        return "updated"
    others = [e for e in existing if not (e["source"] or "").startswith("espn")]
    if not others:
        db.insert(con, "games", row, replace=True)
        return "inserted"
    old = others[0]["game_id"]
    if con.execute("SELECT 1 FROM games WHERE game_id=?", (row["game_id"],)).fetchone():
        # an ESPN row already exists separately: drop the stale BRef row
        con.execute("DELETE FROM games WHERE game_id=?", (old,))
        db.insert(con, "games", row, replace=True)
        db.log_audit(con, "collect", "game-dedup", row["game_id"],
                     {"removed": old, "reason": "espn identity supersedes bref row"})
        return "deduped"
    row["verified"] = max([e["verified"] or 0 for e in others] + [0])
    con.execute("UPDATE games SET game_id=?, source=?, season=?, tipoff_utc=?, "
                "home_score=?, away_score=?, status=?, neutral_site=?, "
                "source_updated_utc=?, captured_utc=?, verified=? WHERE game_id=?",
                (row["game_id"], row["source"], row["season"], row["tipoff_utc"],
                 row["home_score"], row["away_score"], row["status"], row["neutral_site"],
                 row["source_updated_utc"], row["captured_utc"], row["verified"], old))
    con.execute("UPDATE bets SET game_id=? WHERE game_id=?", (row["game_id"], old))
    con.execute("UPDATE team_gamelogs SET game_id=? WHERE game_id=?", (row["game_id"], old))
    con.execute("UPDATE player_gamelogs SET game_id=? WHERE game_id=?", (row["game_id"], old))
    db.log_audit(con, "collect", "game-merge", row["game_id"],
                 {"previous_id": old, "reason": "espn event id replaces bref identity"})
    return "merged"


def espn_backfill_resumable(con, days_per_run: int = 113,
                            floor: str = ESPN_BACKFILL_FLOOR) -> dict:
    """Walk ESPN's scoreboard BACKWARDS one day at a time, resuming across runs.

    Why this exists (2026-09-21 defect): the daily job only fetched
    `today-2 .. today+8`, and the two-season backfill lived behind a manual
    `workflow_dispatch` input that the cron never sets — so `games` stayed at
    0 rows forever and every backtest logged "no games in window". This makes
    the history catch-up automatic and incremental: each scheduled run spends
    a bounded request budget and records its cursor in `meta`, so a fresh
    database self-heals to a full two-season history without human action.

    Dates are fetched oldest-first from the cursor. A day that fails (network
    error, not "zero games") stops the walk WITHOUT advancing the cursor, so
    the day is retried on the next run rather than silently skipped.
    """
    row = con.execute("SELECT value FROM meta WHERE key=?", (BACKFILL_CURSOR_KEY,)).fetchone()
    today = datetime.now(timezone.utc)
    cursor_day = row["value"] if row else today.strftime("%Y%m%d")
    stats = {"days_fetched": 0, "games": 0, "cursor": cursor_day, "done": False,
             "stopped": None}
    d = datetime.strptime(cursor_day, "%Y%m%d") - timedelta(days=1)
    for _ in range(max(0, days_per_run)):
        day = d.strftime("%Y%m%d")
        if day < floor:
            stats["done"] = True
            stats["cursor"] = floor
            break
        r = espn.scoreboard(day)
        save_source_status(con, "espn:scoreboard", r, detail=f"date={day} backfill")
        if not r.ok:
            stats["stopped"] = f"{day}: {r.error}"
            break  # cursor NOT advanced -> retried next run
        games = espn.parse_scoreboard(r.json)
        for g in games:
            odds = g.pop("_odds", None)
            _merge_espn_game_row(con, {
                "game_id": g["game_id"], "source": g["source"], "season": g["season"],
                "game_date_et": g["game_date_et"] or "", "tipoff_utc": g["tipoff_utc"],
                "home_team": g["home_team"], "away_team": g["away_team"],
                "home_score": g["home_score"], "away_score": g["away_score"],
                "status": g["status"], "neutral_site": g["neutral_site"],
                "source_updated_utc": None, "captured_utc": util.utcnow_iso(),
                "verified": 0,
            })
            if odds:
                _store_espn_odds(con, odds)
        stats["days_fetched"] += 1
        stats["games"] += len(games)
        db.insert(con, "meta", {"key": BACKFILL_CURSOR_KEY, "value": day,
                                "updated_utc": util.utcnow_iso()}, replace=True)
        d -= timedelta(days=1)
        time.sleep(0.35)
    db.log_collection(con, "espn-backfill", "espn",
                      "done" if stats["done"] else ("fail" if stats["stopped"] else "ok"),
                      f"days={stats['days_fetched']} games={stats['games']} "
                      f"cursor={stats['cursor']} floor={floor}"
                      + (f" stopped={stats['stopped']}" if stats["stopped"] else ""),
                      rows=stats["games"])
    return stats


# Basketball-Reference monthly pages only exist for months that had games.
# July/August/September are offseason -> the page 404s, and logging that as a
# source "fail" every 6 hours was noise that hid real failures
# (observed 2026-09-21: bref-backfill/bref-verify "2026-september: HTTP 404").
BREF_GAME_MONTHS = {"october", "november", "december", "january", "february",
                    "march", "april", "may", "june"}


def bref_current_month(con, now: datetime | None = None) -> dict:
    """Backfill + verify the CURRENT month on Basketball-Reference.

    Season-end-year mapping matches bref.calendar_year_for(): Oct-Dec of
    calendar year Y belong to the season ENDING in Y+1. Skipped (logged as
    `skipped`, not `fail`) during the offseason.
    """
    now = now or datetime.now(timezone.utc)
    month = _month_name(now.month)
    if month not in BREF_GAME_MONTHS:
        db.log_collection(con, "bref-backfill", "basketball-reference", "skipped",
                          f"{now.year}-{month}: offseason, no monthly page exists")
        return {"skipped": f"{now.year}-{month}"}
    seas_end = now.year + (1 if now.month >= 10 else 0)
    st = bref.backfill_month(con, seas_end, month)
    verified = bref.verify_month(con, seas_end, month)
    st["verified"] = verified
    return st


def collect_boxscores(con, date_yyyymmdd: str) -> int:
    """Fetch summaries for all FINAL games of an ET date; store player+team logs.

    Only rows whose game_id is namespaced `espn:{id}` are fetched. Run
    35553630400 (2026-09-21) logged 41 `boxscore espn fail` rows because this
    used to take `game_id.split(':', 1)[1]` from Basketball-Reference rows
    (`bref:2024-10-28-MIA-DET`), which yields a date fragment, not an ESPN
    event id — one doomed request per game, and the request budget spent on
    games that could never resolve.
    """
    day = f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
    games = con.execute(
        "SELECT * FROM games WHERE game_date_et=? AND status='final' "
        "AND game_id LIKE 'espn:%'", (day,)).fetchall()
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


def boxscores_backfill_resumable(con, start_day: str, end_day: str,
                                 max_games: int = 40) -> dict:
    """Collect box scores for FINAL games, resuming across runs.

    Box scores are one request per game, so a full two-season sweep cannot fit
    in a single scheduled run. The cursor (`boxscore_backfill_next_day`) makes
    it incremental: each run spends at most `max_games` requests and continues
    where the previous run stopped. Dates with no FINAL games in `games` are
    walked through for free (no network).
    """
    row = con.execute("SELECT value FROM meta WHERE key='boxscore_backfill_next_day'").fetchone()
    cursor = row["value"] if row else start_day
    if cursor > end_day:
        db.log_collection(con, "boxscores-backfill", "espn", "done",
                          f"cursor={cursor} end={end_day}", rows=0)
        return {"games": 0, "rows": 0, "cursor": cursor, "done": True}
    d = datetime.strptime(cursor, "%Y%m%d")
    end = datetime.strptime(end_day, "%Y%m%d")
    spent = rows = 0
    last_done = None
    # Days already covered, computed once: the naive per-day
    # `game_id NOT IN (SELECT game_id FROM team_gamelogs)` form rescans the
    # whole log for every date of a 700-day walk.
    done_days = {r[0] for r in con.execute(
        "SELECT DISTINCT game_date_et FROM team_gamelogs")}
    while d <= end:
        day = d.strftime("%Y%m%d")
        iso_day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
        if iso_day in done_days:
            last_done = day
            d += timedelta(days=1)
            continue
        pending = con.execute(
            "SELECT count(*) AS c FROM games WHERE game_date_et=? AND status='final' "
            "AND game_id LIKE 'espn:%'", (iso_day,)).fetchone()
        if pending and pending["c"]:
            if spent >= max_games:
                break
            rows += collect_boxscores(con, day)
            spent += pending["c"]
            done_days.add(iso_day)
        last_done = day
        d += timedelta(days=1)
    done = d > end
    next_day = ((datetime.strptime(last_done, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
                if last_done else cursor)
    db.insert(con, "meta", {"key": "boxscore_backfill_next_day", "value": next_day,
                            "updated_utc": util.utcnow_iso()}, replace=True)
    db.log_collection(con, "boxscores-backfill", "espn", "done" if done else "ok",
                      f"cursor={cursor} next={next_day} games_touched={spent} "
                      f"rows={rows} end={end_day}", rows=rows)
    return {"games": spent, "rows": rows, "cursor": cursor, "next": next_day, "done": done}


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


def normalize_market_row(row: dict, queried_series: str) -> dict | None:
    """Make one parsed Kalshi market row insertable, or return None.

    kalshi_markets.ticker / series_ticker / event_ticker are NOT NULL. The
    live /markets payload does NOT always carry `series_ticker` (runner
    evidence 2026-09-21T00:36:56Z, data/nbacomp.db collection_log id=680:
    `IntegrityError: NOT NULL constraint failed: kalshi_markets.series_ticker`
    — the whole snapshot task crashed on the first such row, so ZERO Kalshi
    prices were ever stored and the paper engine had nothing to price).

    Recovery rules (no invention):
      * series_ticker: fall back to the series we actually queried — that is
        the request parameter, i.e. observed, not guessed.
      * event_ticker: fall back to the market ticker's own prefix
        (`KXNBAGAME-26OCT20LALBOS-YES` -> `KXNBAGAME-26OCT20LALBOS`).
      * still missing identity -> return None so the caller can log an
        anomaly instead of writing a partial row.
    """
    if not row.get("ticker"):
        return None
    if not row.get("series_ticker"):
        row["series_ticker"] = queried_series
    if not row.get("event_ticker"):
        tk = row["ticker"]
        if "-" in tk:
            head = tk.rsplit("-", 1)[0]
            if head and head != tk:
                row["event_ticker"] = head
    if not row.get("series_ticker") or not row.get("event_ticker"):
        return None
    return row


def kalshi_snapshot(con) -> int:
    """Refresh OPEN markets (forward prices) + orderbooks for the main series."""
    n = 0
    rejected = 0
    CAP2 = util.utcnow_iso()
    book_tickers: list[str] = []
    for s in _live_series(con):
        try:
            markets = kalshi.get_markets(s, status="open", max_pages=10)
        except Exception as e:
            db.log_collection(con, "kalshi-snapshot", f"kalshi:{s}", "fail", str(e)[:200])
            continue
        stored = 0
        for m in markets:
            try:
                row = kalshi.parse_market(m, CAP2)
            except Exception as e:
                db.log_anomaly(con, "warn", "kalshi-market-parse-error",
                               {"ticker": m.get("ticker"), "err": str(e)[:200]})
                rejected += 1
                continue
            row = normalize_market_row(row, s)
            if row is None:
                db.log_anomaly(con, "warn", "kalshi-market-missing-identity",
                               {"ticker": m.get("ticker"), "queried_series": s,
                                "keys": sorted(m.keys())[:25]})
                rejected += 1
                continue
            db.insert(con, "kalshi_markets", row, replace=True)
            n += 1
            stored += 1
            if s in ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBA1H", "KXNBAQ1"):
                book_tickers.append(row["ticker"])
        db.log_collection(con, "kalshi-snapshot", f"kalshi:{s}", "ok",
                          f"open_markets={stored}", rows=stored)
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
    status = "ok" if (n or not book_tickers) else "partial"
    db.log_collection(con, "kalshi-snapshot", "kalshi", status,
                      f"open_markets={n} books={books} rejected={rejected}", rows=n)
    if n == 0 and rejected:
        # Never let a total-loss run look like a quiet success: the site,
        # the paper engine and every downstream number depend on these rows.
        db.log_anomaly(con, "critical", "kalshi-snapshot-stored-nothing",
                       {"rejected": rejected,
                        "detail": "live /markets returned rows but none were storable"})
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

    HONEST LIMIT (verified 2026-09-21): settled Kalshi markets are NOT
    retrievable via any public endpoint (/markets?event_ticker,
    /events/{ticker}, series+status filters, and direct ticker GETs all
    return empty/404 for settled rows), so this walk currently yields zero
    markets. It is kept (bounded) because Kalshi could re-expose settled
    rows at any time; forward candle accumulation is the real history
    source. A zero yield is logged as such, never hidden.

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
        n_events = skipped = empty_streak = 0
        for e in events:
            ev_ticker = e.get("event_ticker") or e.get("ticker")
            if not ev_ticker:
                skipped += 1
                continue
            ev_date = engine.parse_event_ticker(ev_ticker)[0]
            if cutoff and ev_date and ev_date < cutoff:
                continue
            if empty_streak >= 3:
                # Settled markets are API-unexposed (verified 2026-09-21):
                # after 3 consecutive empty events, stop burning a request
                # per event (400+/series) and just finish the walk.
                skipped += 1
                continue
            ms = kalshi.get_markets_by_event(ev_ticker)
            if not ms:
                empty_streak += 1
            else:
                empty_streak = 0
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
                          f"events={n_events} skipped={skipped} markets_total={total}",
                          rows=n_events)
    if total == 0:
        db.log_collection(con, "kalshi-backfill", "kalshi", "empty",
                          "settled markets not exposed by public API (verified 2026-09-21); "
                          "forward candle accumulation is the history source", rows=0)
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
    n = _store_candles(con, got, interval)
    db.log_collection(con, "kalshi-candles", "kalshi", "ok" if n else "empty",
                      f"tickers={len(tickers)} candles={n} window={start_iso}..{end_iso}", rows=n)
    return n


def kalshi_candles_forward(con, days: int = 3, interval: int = 60) -> int:
    """Candles for currently OPEN game markets (forward history accumulation).

    Settled markets are NOT exposed by the public API (verified 2026-09-21:
    /markets?event_ticker, /events/{t}, series+status, and direct ticker GETs
    all return empty/404 for settled rows), so this forward accumulation is
    the ONLY Kalshi price-history source. Run every collection; idempotent
    (PK ticker/interval/ts_utc, INSERT OR IGNORE).
    """
    now = util.utcnow_iso()
    start_iso = util.to_iso(util.parse_iso(now) - timedelta(days=days))
    rows = con.execute(
        "SELECT ticker FROM kalshi_markets WHERE status IN ('active', 'open') "
        "AND series_ticker IN ('KXNBAGAME','KXNBASPREAD','KXNBATOTAL','KXNBA1H','KXNBAQ1') "
        "AND close_time IS NOT NULL AND close_time >= ?", (now,)).fetchall()
    tickers = [r["ticker"] for r in rows]
    if not tickers:
        db.log_collection(con, "kalshi-candles-forward", "kalshi", "empty",
                          "no open game markets stored yet", rows=0)
        return 0
    start = int(util.parse_iso(start_iso).timestamp())
    end = int(util.parse_iso(now).timestamp())
    got = kalshi.get_candlesticks(tickers, start * 1000, end * 1000, interval)
    n = _store_candles(con, got, interval)
    db.log_collection(con, "kalshi-candles-forward", "kalshi", "ok" if n else "empty",
                      f"tickers={len(tickers)} candles={n} window={start_iso}..{now}", rows=n)
    return n


MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def event_ticker_for(date_et: str, team_a: str, team_b: str,
                     series: str = "KXNBAGAME") -> str | None:
    """Build a Kalshi NBA event ticker from a verified game row.

    Format runner-verified 2026-09-20/21 (data/diagnostics.txt probe5 and the
    real open-market fixtures in tests/test_live_shapes.py):
    `{SERIES}-{YY}{MON}{DD}{TEAM1}{TEAM2}`, e.g. KXNBAGAME-26OCT20OKCSAS.
    Both team orders are returned as candidates by `event_ticker_candidates`
    because the API's ordering rule was never observed directly — we probe
    both instead of guessing one.
    """
    try:
        y, m, d = date_et.split("-")
    except (ValueError, AttributeError):
        return None
    if len(y) != 4:
        return None
    return f"{series}-{y[2:]}{MONTH_ABBR[int(m) - 1]}{int(d):02d}{team_a}{team_b}"


def event_ticker_candidates(date_et: str, home: str, away: str,
                            series: str = "KXNBAGAME") -> list[str]:
    out = []
    for a, b in ((away, home), (home, away)):
        t = event_ticker_for(date_et, a, b, series)
        if t and t not in out:
            out.append(t)
    return out


def market_ticker_candidates(event_ticker: str, home: str, away: str) -> list[str]:
    """Candidate market tickers for one event.

    Live markets in KXNBAGAME are per-team (fixture-verified:
    `KXNBAGAME-26OCT20OKCSAS-SAS` / `-OKC`), while generic binary Kalshi
    markets are `-YES`/`-NO`. We probe all four rather than assume which
    convention a settled series used; only tickers that actually return data
    are ever stored.
    """
    return [f"{event_ticker}-{s}" for s in (away, home, "YES", "NO")]


def kalshi_settled_history(con, series: str = "KXNBAGAME", max_games: int = 60,
                           interval: int = 60) -> dict:
    """Attempt to recover price history for SETTLED Kalshi markets.

    Why this exists: settled market ROWS are not exposed by the public API
    (runner-verified 2026-09-21, collection_log: "settled markets not exposed
    by public API"), so `kalshi_backfill` can never yield history and the
    backtester had nothing to price against. Whether settled markets still
    expose CANDLESTICKS or the TRADE tape is a separate question, and this
    function is the experiment.

    Tickers are CONSTRUCTED from verified game rows already in `games`
    (date + both team abbreviations -> event ticker, then the four market
    suffixes). Nothing is invented: if a constructed ticker does not exist
    the endpoint returns no rows for it and nothing is stored. A zero yield
    is logged as `empty` and recorded in `meta` so the site can state
    honestly that settled price history is unavailable.
    """
    CAP2 = util.utcnow_iso()
    games = con.execute(
        "SELECT game_id, game_date_et, home_team, away_team FROM games "
        "WHERE status='final' ORDER BY game_date_et DESC LIMIT ?", (max_games,)).fetchall()
    if not games:
        db.log_collection(con, "kalshi-settled-history", f"kalshi:{series}", "empty",
                          "no final games in database to derive tickers from", rows=0)
        return {"games_probed": 0, "tickers_probed": 0, "candles": 0, "trades": 0,
                "availability": "no-games"}

    ev_by_ticker: dict[str, str] = {}
    game_by_ev: dict[str, dict] = {}
    for g in games:
        for ev in event_ticker_candidates(g["game_date_et"], g["home_team"], g["away_team"], series):
            for tk in market_ticker_candidates(ev, g["home_team"], g["away_team"]):
                ev_by_ticker[tk] = ev
                game_by_ev[ev] = dict(g)
    tickers = sorted(ev_by_ticker)

    now_s = int(datetime.now(timezone.utc).timestamp())
    start_s = now_s - 500 * 86400
    got = kalshi.get_candlesticks(tickers, start_s * 1000, now_s * 1000, interval)
    candles = _store_candles(con, got, interval)

    hit_tickers = [e.get("market_ticker") for e in got if e.get("candlesticks")]
    for tk in hit_tickers:
        ev = ev_by_ticker.get(tk) or (tk.rsplit("-", 1)[0] if "-" in tk else tk)
        # A ticker that returned candles demonstrably exists. Record its
        # identity so the backtester can join it to a game; every price field
        # stays NULL because no quote was observed on this channel.
        db.insert(con, "kalshi_markets", {
            "ticker": tk, "series_ticker": series, "event_ticker": ev,
            "title": None, "subtitle": None,
            "market_type": "winner" if series == "KXNBAGAME" else "unknown",
            "strike_values": None, "status": "settled", "close_time": None,
            "expected_expiration_time": None, "yes_bid": None, "yes_ask": None,
            "last_price": None, "volume": None, "open_interest": None,
            "result": None, "settled_time": None, "captured_utc": CAP2,
        }, replace=False)

    # trade tape: an independent historical price channel, sampled
    trades = 0
    for tk in (hit_tickers or tickers)[:4]:
        tr = kalshi._get("/markets/trades", {"ticker": tk, "limit": 1000})
        rows = ((tr.json or {}).get("trades") or []) if tr.ok else []
        trades += len(rows)
        db.log_collection(con, "kalshi-settled-history", f"kalshi:tape:{tk}",
                          "ok" if tr.ok else "fail",
                          f"http={tr.status} trades={len(rows)}"
                          + ("" if tr.ok else f" err={tr.error}"), rows=len(rows))
        time.sleep(0.25)

    availability = ("candles-available" if candles else
                    "tape-available" if trades else "unavailable")
    db.log_collection(con, "kalshi-settled-history", f"kalshi:{series}",
                      "ok" if (candles or trades) else "empty",
                      f"games_probed={len(games)} tickers_probed={len(tickers)} "
                      f"tickers_with_candles={len(hit_tickers)} candles={candles} "
                      f"trades={trades} availability={availability}",
                      rows=candles + trades)
    db.insert(con, "meta", {"key": f"kalshi_settled_availability:{series}",
                            "value": json.dumps({"availability": availability,
                                                 "candles": candles, "trades": trades,
                                                 "games_probed": len(games),
                                                 "tickers_probed": len(tickers),
                                                 "checked_utc": CAP2}),
               "updated_utc": CAP2}, replace=True)
    return {"games_probed": len(games), "tickers_probed": len(tickers),
            "candles": candles, "trades": trades, "availability": availability}


def _store_candles(con, got: list[dict], interval: int) -> int:
    n = 0
    C = util.utcnow_iso()
    for entry in got:
        t = entry.get("market_ticker")
        for c in entry.get("candlesticks") or []:
            # Live shape 2026-09-20: {"end_period_ts": 1788..., "price":
            # {"open_dollars": "0.38", ...}, "volume_fp": "707.69"}. The old
            # keys (ts/open/high/...) never existed -> every row was NULLs.
            raw_ts = c.get("end_period_ts")
            ts = _ms_iso(raw_ts if raw_ts is not None
                         else (c.get("ts") or c.get("timestamp_ms")))
            if ts and raw_ts is not None and interval:
                # end_period_ts is the candle CLOSE; the DB invariant (and
                # PriceBook's look-ahead guard) is ts = candle OPEN.
                ts = util.to_iso(util.parse_iso(ts) - timedelta(seconds=interval * 60))
            price = c.get("price") or {}
            db.insert(con, "kalshi_candles", {
                "ticker": t, "interval": interval,
                "ts_utc": ts,
                "open": _dollars_to_cents(price.get("open_dollars"), price.get("open")),
                "high": _dollars_to_cents(price.get("high_dollars"), price.get("high")),
                "low": _dollars_to_cents(price.get("low_dollars"), price.get("low")),
                "close": _dollars_to_cents(price.get("close_dollars"), price.get("close")),
                "volume": _contracts(c.get("volume_fp"), c.get("volume")),
                "captured_utc": C,
            })
            n += 1
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
                                        "kalshi-snapshot", "kalshi-candles", "espn-backfill",
                                        "espn-forward",
                                        "boxscores-backfill", "kalshi-settled-history",
                                        "bref-month"])
    ap.add_argument("--days", type=int, default=113,
                    help="espn-backfill: day budget per run (default 113 = ~1 season)")
    ap.add_argument("--max-games", type=int, default=40,
                    help="boxscores-backfill: request budget per run (default 40)")
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
            # Per-task isolation: one crashing source must not discard the
            # other tasks' work (get_db commits only at clean exit, so each
            # task commits separately; tracebacks go to stdout AND the
            # collection_log, never swallowed).
            def run_task(name, fn, *a, **k):
                try:
                    out = fn(*a, **k)
                    con.commit()
                    return out
                except Exception:
                    import traceback
                    tb = traceback.format_exc()
                    print(f"TASK-FAIL {name}:\n{tb}", flush=True)
                    try:
                        db.log_collection(con, f"daily-{name}", "nbacomp", "crash",
                                          tb[-1500:])
                        con.commit()
                    except Exception:
                        pass
                    return None

            now = datetime.now(timezone.utc)
            d = now - timedelta(days=2)
            for _ in range(11):
                run_task("espn-day", collect_espn_day, con, d.strftime("%Y%m%d"))
                d += timedelta(days=1)
            # Automatic, resumable two-season history catch-up. Without this
            # the DB never left 0 games (the full backfill was gated behind a
            # manual workflow_dispatch input the cron never sets), so every
            # backtest logged "no games in window". 2026-09-21.
            run_task("espn-backfill", espn_backfill_resumable, con, 113)
            # Upcoming schedule + tipoffs (the openers the live Kalshi markets
            # refer to). ESPN carries no odds for PAST dates (probe7), so this
            # is also the only channel that will ever hold pre-game lines.
            run_task("espn-forward", espn_forward_window, con, 42)
            run_task("boxscores-backfill", boxscores_backfill_resumable, con,
                     "20241001", now.strftime("%Y%m%d"), 40)
            run_task("injuries", collect_injuries, con)
            run_task("kalshi-snapshot", kalshi_snapshot, con)
            # candles for open markets: the ONLY Kalshi history source
            # (settled markets are not API-exposed). Idempotent.
            run_task("kalshi-candles-forward", kalshi_candles_forward, con)
            # recent settled events (settlement ground truth for forward bets)
            run_task("kalshi-backfill", kalshi_backfill, con, max_pages=2)
            # does Kalshi expose candles/tape for settled NBA markets? probed
            # and recorded every run; a zero yield is logged as empty.
            run_task("kalshi-settled-history", kalshi_settled_history, con)
            # yesterday's boxscores (player logs + rolling features)
            y = now - timedelta(days=1)
            run_task("boxscores", collect_boxscores, con, y.strftime("%Y%m%d"))
            # BRef: backfill + verify the current month (skipped, not failed,
            # in the offseason — July/August/September have no monthly page).
            run_task("bref-month", bref_current_month, con, now)
        elif args.command == "espn-forward":
            print(json.dumps(espn_forward_window(con, args.days), indent=2))
        elif args.command == "espn-backfill":
            print(json.dumps(espn_backfill_resumable(con, days_per_run=args.days), indent=2))
        elif args.command == "boxscores-backfill":
            print(json.dumps(boxscores_backfill_resumable(
                con, args.start or "20241001", args.end or datetime.now(timezone.utc).strftime("%Y%m%d"),
                args.max_games), indent=2))
        elif args.command == "kalshi-settled-history":
            print(json.dumps(kalshi_settled_history(con, args.series or "KXNBAGAME"), indent=2))
        elif args.command == "bref-month":
            print(json.dumps(bref_current_month(con), indent=2))
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
