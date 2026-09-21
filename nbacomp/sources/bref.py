"""Basketball-Reference — independent score verification source.

Reachability from GitHub Actions runners verified 2026-09-20 (probe4:
leagues/NBA_2026_games.html → HTTP 200). Monthly results pages carry every
game's final score; we parse the "schedule" table rows and cross-check ESPN
finals. BBR is HTML (no API); we self-limit to ~1 request/second.
"""
from __future__ import annotations

import re

from .. import http

BASE = "https://www.basketball-reference.com"


def monthly_games_url(season_end_year: int, month: str) -> str:
    return f"{BASE}/leagues/NBA_{season_end_year}_games-{month}.html"


def parse_monthly_games(html: str) -> list[dict]:
    """Parse the monthly schedule table into normalized game rows.

    Returns date (YYYY-MM-DD), visitor/home abbreviations and scores.
    Only rows with final scores are returned (live/pregame rows skipped).
    """
    out: list[dict] = []
    # each game row: <tr ...><th scope="row" class="left " data-stat="game_start_time">...
    # <td data-stat="visitor_team_name" ...><a ...>Team Name</a></td>
    # <td data-stat="visitor_pts">…</td> ... <td data-stat="home_team_name">...
    # <td data-stat="home_pts">
    for m in re.finditer(
            r'<td[^>]*data-stat="visitor_team_name"[^>]*>.*?<a href="[^"]*">([^<]+)</a>.*?'
            r'<td[^>]*data-stat="visitor_pts"[^>]*>(\d+)</td>.*?'
            r'<td[^>]*data-stat="home_team_name"[^>]*>.*?<a href="[^"]*">([^<]+)</a>.*?'
            r'<td[^>]*data-stat="home_pts"[^>]*>(\d+)</td>', html, re.S):
        v_name, v_pts, h_name, h_pts = m.groups()
        out.append({"visitor_name": v_name.strip(), "visitor_pts": int(v_pts),
                    "home_name": h_name.strip(), "home_pts": int(h_pts)})
    # game dates from row headers: <th data-stat="game_start_time"...> on <tr>
    # simpler: capture date text rows separately and zip in order of appearance
    dates = re.findall(r'<tr[^>]*>\s*<th(?:\s[^>]*)?data-stat="game_start_time"[^>]*>([^<]+)</th>', html)
    _ = dates  # per-row date mapping is fragile; verification joins on (names, scores, month)
    return out


TEAM_NAME_TO_ABBR = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM", "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA", "Washington Wizards": "WAS", "Seattle SuperSonics": "SEA",
    "New Jersey Nets": "BKN", "Vancouver Grizzlies": "MEM", "New Orleans Hornets": "NOP",
    "Charlotte Bobcats": "CHA", "New Orleans/OKC Hornets": "NOP",
}


def abbr_for(name: str) -> str | None:
    return TEAM_NAME_TO_ABBR.get(name.strip())


_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
               "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
_CELL_RE = re.compile(r'<td[^>]*data-stat="([a-z_]+)"[^>]*>(.*?)</td>', re.S)
_LINK_RE = re.compile(r'<a\s+href="([^"]*)">([^<]*)</a>')
_BOX_RE = re.compile(r"/boxscores/(\d{4})(\d{2})(\d{2})0([A-Z]{3})\.html")
_DATE_RE = re.compile(r"([A-Z][a-z]{2,9})\s+(\d{1,2}),\s+(\d{4})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])", re.I)
# BRef boxscore-URL codes that differ from our ESPN-style abbreviations.
_BREF_URL_ABBR = {"CHO": "CHA", "BRK": "BKN", "PHO": "PHX"}


def _text(html_frag: str) -> str:
    return re.sub(r"<[^>]+>", "", html_frag or "").strip()


def parse_schedule_page(html: str) -> list[dict]:
    """Parse a BRef monthly schedule page into game rows (final AND upcoming).

    Returns dicts with game_date_et, visitor/home abbrs, scores or None,
    start_et ("7:30p") or None, boxscore_href or None, status. Tolerant by
    design: rows that don't look like games are skipped, never guessed.
    Date priority: boxscore URL (exact) > row-header date text.
    """
    out: list[dict] = []
    for tr in _TR_RE.findall(html or ""):
        cells = dict(_CELL_RE.findall(tr))
        if "visitor_team_name" not in cells and "home_team_name" not in cells:
            continue
        v_raw, h_raw = cells.get("visitor_team_name", ""), cells.get("home_team_name", "")
        v_link = _LINK_RE.search(v_raw)
        h_link = _LINK_RE.search(h_raw)
        v_name = _text(v_link.group(2) if v_link else v_raw)
        h_name = _text(h_link.group(2) if h_link else h_raw)
        v, h = abbr_for(v_name) if v_name else None, abbr_for(h_name) if h_name else None
        if not v or not h:
            continue

        def _pts(key: str) -> int | None:
            t = _text(cells.get(key, "")).strip()
            return int(t) if t.isdigit() else None

        v_pts, h_pts = _pts("visitor_pts"), _pts("home_pts")
        box = _BOX_RE.search(tr)
        if box:
            yy, mm, dd, home3 = box.groups()
            date_et = f"{yy}-{mm}-{dd}"
            # BRef's URL codes predate ours for three teams (CHO/BRK/PHO vs
            # our ESPN-style CHA/BKN/PHX) — normalize before comparing.
            home3 = _BREF_URL_ABBR.get(home3, home3)
            if home3 in TEAM_NAME_TO_ABBR.values() and home3 != h:
                continue  # boxscore URL disagrees with row -> skip, don't guess
        else:
            date_et = None
            for th in _TH_RE.findall(tr):
                dm = _DATE_RE.search(_text(th))
                if dm:
                    mon = _MONTH_ABBR.get(dm.group(1)[:3].lower())
                    if mon:
                        date_et = f"{dm.group(3)}-{mon:02d}-{int(dm.group(2)):02d}"
                    break
        if not date_et:
            continue
        start = None
        for _key, val in cells.items():
            tm = _TIME_RE.search(_text(val))
            if tm and _key in ("game_start_time", "start_time", "time"):
                start = f"{int(tm.group(1))}:{tm.group(2)}{tm.group(3).lower()}"
                break
        else:
            for _key, val in cells.items():
                tm = _TIME_RE.search(_text(val))
                if tm:
                    start = f"{int(tm.group(1))}:{tm.group(2)}{tm.group(3).lower()}"
                    break
        out.append({"game_date_et": date_et, "away_team": v, "home_team": h,
                    "away_score": v_pts, "home_score": h_pts,
                    "start_et": start,
                    "boxscore_href": box.group(0) if box else None,
                    "status": "final" if v_pts is not None and h_pts is not None else "scheduled"})
    return out


def season_for(calendar_year: int, month: str) -> str:
    """'2025-26' style label for a calendar (year, month)."""
    m = month.strip().lower()
    if m in ("october", "november", "december"):
        return f"{calendar_year}-{str(calendar_year + 1)[2:]}"
    return f"{calendar_year - 1}-{str(calendar_year)[2:]}"


def calendar_year_for(season_end_year: int, month: str) -> int:
    """Oct-Dec belong to the calendar year BEFORE the season's end year."""
    if month.strip().lower() in ("october", "november", "december"):
        return season_end_year - 1
    return season_end_year


def et_tipoff_to_utc(date_et: str, start_et: str | None) -> str | None:
    """'2026-10-20' + '7:30p' -> '2026-10-20T23:30:00Z' (DST-aware ET).

    Returns None when the start time is missing/unparseable — callers must
    treat that as unknown, never invent a tipoff.
    """
    if not start_et:
        return None
    m = _TIME_RE.match(start_et.strip())
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == "p" and hh != 12:
        hh += 12
    if ap == "a" and hh == 12:
        hh = 0
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        y, mo, d = map(int, date_et.split("-"))
        local = datetime(y, mo, d, hh, mm, tzinfo=ZoneInfo("America/New_York"))
        return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def verify_month(con, season_end_year: int, month: str) -> int:
    """Fetch a monthly page and cross-verify every ESPN final of that month."""
    import calendar

    from .. import db, util
    r = http.get(monthly_games_url(season_end_year, month), min_interval=1.2)
    db.insert(con, "source_status", {
        "source_id": "bref:monthly-games", "checked_utc": util.utcnow_iso(),
        "ok": 1 if r.ok_body else 0, "http_status": r.status,
        "detail": f"{season_end_year}-{month}", "sample_hash": None}, replace=True)
    if not r.ok_body:
        db.log_collection(con, "bref-verify", "basketball-reference", "fail",
                          f"{season_end_year}-{month}: {r.error}")
        return 0
    rows = parse_monthly_games(r.body.decode("utf-8", "replace"))
    # Season-end year != calendar year for Oct-Dec (e.g. 2026 season's
    # October games are 2025-10). Old code queried YYYY=season_end for all
    # months, so fall verification windows matched nothing.
    cal_year = calendar_year_for(season_end_year, month)
    last_day = calendar.monthrange(cal_year, _month_num(month))[1]
    d0 = f"{cal_year}-{_month_num(month):02d}-01"
    d1 = f"{cal_year}-{_month_num(month):02d}-{last_day:02d}"
    n = 0
    for row in rows:
        v = abbr_for(row["visitor_name"])
        h = abbr_for(row["home_name"])
        if not v or not h:
            continue
        games = con.execute(
            "SELECT * FROM games WHERE home_team=? AND away_team=? AND status='final' "
            "AND home_score=? AND away_score=? AND verified=0 AND game_date_et BETWEEN ? AND ?",
            (h, v, row["home_pts"], row["visitor_pts"], d0, d1)).fetchall()
        for g in games:
            con.execute("UPDATE games SET verified=1, verified_against='basketball-reference' "
                        "WHERE game_id=?", (g["game_id"],))
            db.insert(con, "verifications", {
                "checked_utc": util.utcnow_iso(),
                "claim": f"final score {g['game_id']} {v}@{h} on {g['game_date_et']}",
                "primary_source": "espn",
                "primary_value": f"{row['visitor_pts']}-{row['home_pts']}",
                "secondary_source": "basketball-reference",
                "secondary_value": f"{row['visitor_pts']}-{row['home_pts']}",
                "status": "match", "discrepancy": None})
            n += 1
    db.log_collection(con, "bref-verify", "basketball-reference", "ok",
                      f"{season_end_year}-{month}: {len(rows)} rows, {n} games verified", rows=n)
    return n


def _month_num(month: str) -> int:
    return ["january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december"].index(month) + 1


def backfill_verification(con, seasons: list[tuple[int, list[str]]]) -> int:
    """Verify whole seasons: [(season_end_year, [months...])]."""
    n = 0
    for year, months in seasons:
        for m in months:
            n += verify_month(con, year, m)
    return n


def backfill_month(con, season_end_year: int, month: str) -> dict:
    """Write one BRef monthly page into games (finals AND scheduled).

    BRef is the schedule-of-record fallback while ESPN is unreachable: it
    supplies dates, matchups, tipoffs (ET->UTC), and final scores. Identity
    is by (date, away, home) because ESPN's opaque `espn:{id}` game_ids
    cannot be reproduced — an ESPN row for the same matchup always wins
    (only missing scores/status are merged in, never overwritten blindly).
    New rows use game_id `bref:{date}-{away}-{home}`. Returns stats.
    """
    from .. import db, util
    stats = {"parsed": 0, "inserted": 0, "merged": 0, "skipped": 0}
    r = http.get(monthly_games_url(season_end_year, month), min_interval=1.2)
    db.insert(con, "source_status", {
        "source_id": "bref:monthly-backfill", "checked_utc": util.utcnow_iso(),
        "ok": 1 if r.ok_body else 0, "http_status": r.status,
        "detail": f"{season_end_year}-{month}", "sample_hash": None}, replace=True)
    if not r.ok_body:
        db.log_collection(con, "bref-backfill", "basketball-reference", "fail",
                          f"{season_end_year}-{month}: {r.error}")
        return stats
    rows = parse_schedule_page(r.body.decode("utf-8", "replace"))
    stats["parsed"] = len(rows)
    cal_year = calendar_year_for(season_end_year, month)
    season = season_for(cal_year, month)
    now = util.utcnow_iso()
    for row in rows:
        d, a, h = row["game_date_et"], row["away_team"], row["home_team"]
        have = con.execute(
            "SELECT game_id, source, status, home_score, away_score, tipoff_utc, verified "
            "FROM games WHERE game_date_et=? AND away_team=? AND home_team=?",
            (d, a, h)).fetchall()
        espn_rows = [x for x in have if (x["source"] or "").startswith("espn")]
        if espn_rows:
            g = espn_rows[0]
            # Merge BRef final scores into a stale ESPN row (e.g. collected
            # as scheduled during an outage); never touch a complete final.
            if (row["status"] == "final" and g["status"] != "final"):
                con.execute(
                    "UPDATE games SET home_score=?, away_score=?, status='final', "
                    "source_updated_utc=? WHERE game_id=?",
                    (row["home_score"], row["away_score"], now, g["game_id"]))
                db.log_audit(con, "collect", "bref-merge", g["game_id"],
                             {"scores": [row["away_score"], row["home_score"]]})
                stats["merged"] += 1
            else:
                stats["skipped"] += 1
            continue
        tip = et_tipoff_to_utc(d, row["start_et"])
        gid = f"bref:{d}-{a}-{h}"
        # never downgrade a verification earned by an earlier pass
        ver = max([x["verified"] or 0 for x in have] + [0])
        db.insert(con, "games", {
            "game_id": gid, "source": "basketball-reference", "season": season,
            "game_date_et": d, "tipoff_utc": tip,
            "home_team": h, "away_team": a,
            "home_score": row["home_score"], "away_score": row["away_score"],
            "status": row["status"], "neutral_site": 0,
            "source_updated_utc": None, "captured_utc": now, "verified": ver,
        }, replace=True)
        stats["inserted"] += 1
    db.log_collection(con, "bref-backfill", "basketball-reference", "ok",
                      f"{season_end_year}-{month}: parsed={stats['parsed']} "
                      f"inserted={stats['inserted']} merged={stats['merged']} "
                      f"skipped={stats['skipped']}", rows=stats["inserted"])
    return stats
