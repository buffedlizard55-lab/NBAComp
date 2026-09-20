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


def verify_month(con, season_end_year: int, month: str) -> int:
    """Fetch a monthly page and cross-verify every ESPN final of that month."""
    import calendar

    from .. import db, util
    r = http.get(monthly_games_url(season_end_year, month), min_interval=1.2)
    db.insert(con, "source_status", {
        "source_id": "bref:monthly-games", "checked_utc": util.utcnow_iso(),
        "ok": 1 if r.ok else 0, "http_status": r.status,
        "detail": f"{season_end_year}-{month}", "sample_hash": None}, replace=True)
    if not r.ok:
        db.log_collection(con, "bref-verify", "basketball-reference", "fail",
                          f"{season_end_year}-{month}: {r.error}")
        return 0
    rows = parse_monthly_games(r.body.decode("utf-8", "replace"))
    last_day = calendar.monthrange(season_end_year, _month_num(month))[1]
    d0 = f"{season_end_year}-{_month_num(month):02d}-01"
    d1 = f"{season_end_year}-{_month_num(month):02d}-{last_day:02d}"
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
