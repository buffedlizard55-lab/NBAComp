"""NBA.com official statistics endpoints (stats.nba.com) + fallback scoreboard.

stats.nba.com is the league's own public JSON API (keyless; browser-like
headers required; undocumented rate limits — we keep ~1 rps). Historical depth
verified per endpoint at collection time and recorded in the registry.

Verification role: official game results cross-check ESPN results (#5).
"""
from __future__ import annotations

from .. import http

BASE = "https://stats.nba.com/stats"
DATA_NET = "https://data.nba.net/prod/v1"

HEADERS = {
    "User-Agent": http.USER_AGENT,
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
}


def _get(path: str, params: dict) -> http.HttpResult:
    return http.get(f"{BASE}{path}", params, headers=HEADERS, min_interval=1.1)


def leaguedashteamstats(season: str, measure: str = "Base", season_type: str = "Regular Season") -> http.HttpResult:
    params = {
        "Conference": "", "DateFrom": "", "DateTo": "", "Division": "",
        "GameScope": "", "GameSegment": "", "LastNGames": "0", "LeagueID": "00",
        "Location": "", "MeasureType": measure, "Month": "0", "OpponentTeamID": "0",
        "Outcome": "", "PORound": "0", "PaceAdjust": "N", "PerMode": "PerGame",
        "Period": "0", "PlayerExperience": "", "PlayerPosition": "", "PlusMinus": "N",
        "Rank": "N", "Season": season, "SeasonSegment": "", "SeasonType": season_type,
        "ShotClockRange": "", "StarterBench": "", "TeamID": "0", "TwoWay": "",
        "VsConference": "", "VsDivision": "",
    }
    return _get("/leaguedashteamstats", params)


def teamgamelogs(season: str, season_type: str = "Regular Season") -> http.HttpResult:
    params = {
        "DateFrom": "", "DateTo": "", "GameSegment": "", "LastNGames": "0",
        "LeagueID": "00", "Location": "", "MeasureType": "Base", "Month": "0",
        "OppTeamID": "0", "Outcome": "", "PORound": "0", "PerMode": "T",
        "Period": "0", "PlayerID": "0", "Season": season, "SeasonSegment": "",
        "SeasonType": season_type, "ShotClockRange": "", "TeamID": "0",
        "VsConference": "", "VsDivision": "",
    }
    return _get("/teamgamelogs", params)


def playergamelogs(season: str, season_type: str = "Regular Season") -> http.HttpResult:
    params = {
        "DateFrom": "", "DateTo": "", "GameSegment": "", "LastNGames": "0",
        "LeagueID": "00", "Location": "", "MeasureType": "Base", "Month": "0",
        "OppTeamID": "0", "Outcome": "", "PORound": "0", "PerMode": "T",
        "Period": "0", "PlayerID": "0", "Season": season, "SeasonSegment": "",
        "SeasonType": season_type, "ShotClockRange": "", "TeamID": "0",
        "VsConference": "", "VsDivision": "",
    }
    return _get("/playergamelogs", params)


def leaguehustlestatsteam(season: str) -> http.HttpResult:
    params = {
        "College": "", "Conference": "", "Country": "", "DateFrom": "", "DateTo": "",
        "Division": "", "DraftPick": "", "DraftYear": "", "GameScope": "",
        "Height": "", "LastNGames": "0", "LeagueID": "00", "Location": "",
        "Month": "0", "OppTeamID": "0", "Outcome": "", "PORound": "0",
        "PerMode": "PerGame", "Period": "0", "PlayerExperience": "",
        "PlayerPosition": "", "Season": season, "SeasonSegment": "",
        "SeasonType": "Regular Season", "StarterBench": "", "TeamID": "0",
        "TwoWay": "", "VsConference": "", "VsDivision": "", "Weight": "",
    }
    return _get("/leaguehustlestatsteam", params)


def scoreboard_v2(game_date_mmddyyyy: str) -> http.HttpResult:
    """Official results for cross-verification (game_date ET, MM/DD/YYYY)."""
    params = {
        "GameDate": game_date_mmddyyyy, "LeagueID": "00", "DayOffset": "0",
    }
    return _get("/scoreboardv2", params)


def parse_rows(js: dict | None) -> list[dict]:
    """stats.nba.com responses: resultSets[0] {headers, rowSet}."""
    if not js:
        return []
    out: list[dict] = []
    for rs in js.get("resultSets", []) or []:
        headers = [h.lower() for h in rs.get("headers", [])]
        for row in rs.get("rowSet", []) or []:
            out.append(dict(zip(headers, row)))
    return out
