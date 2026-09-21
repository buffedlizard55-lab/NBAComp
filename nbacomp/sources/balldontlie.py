"""BallDon'tLie API — DISABLED (keyless access retired; excluded by policy).

STATUS 2026-09-21: retired as a source for this project. CI run
35561731651 (collect-and-build) hit HTTP 404 on every endpoint of the
legacy keyless base (https://www.balldontlie.io/api/v1). The service now
serves NBA data only behind a registered API key
(https://api.balldontlie.io, key in the Authorization header; a "free
tier" exists but requires account registration). A signup key is not
keyless, so the project's free-keyless-only data policy excludes it.
No row from this source ever entered the database (verified 2026-09-21:
0 rows with source LIKE '%balldontlie%'). The client code below is kept
for the record; all collectors in nbacomp.collect are gated on the
BDLT_DISABLED_REASON constant and never send requests.

Original design notes (keyless era):
Endpoints used:
  GET /games?seasons[]=YYYY[&per_page=250&offset=N]
      season game lists (ids, teams+abbrevs, dates, final scores)
  GET /games/{id}
      per-game box score (players_home / players_away with per-game stats)
  GET /teams?seasons[]=YYYY&per_page=100
      per-team per-season aggregates
  GET /players?seasons[]=YYYY&per_page=250&include[]=games
      per-player per-season per-game aggregates

SEASON PARAMETER SEMANTICS ARE NOT TRUSTED ON FAITH: the collector reads the
`season` label and the actual game dates out of every response and records
them; if a requested season returns games outside the expected Oct Y .. Jun
Y+1 window the discrepancy is logged and the data is still stored under the
label the source itself reports (never re-labelled by guesswork).

Role in this project: deep-history box scores (seasons older than the ESPN
walk has reached), team/player season aggregates, and an INDEPENDENT
final-score cross-check against ESPN/Basketball-Reference rows (semi-
independent: a different service aggregating public league data).
"""
from __future__ import annotations

from .. import http

BASE = "https://www.balldontlie.io/api/v1"


def get(url_path: str, params: dict | None = None) -> http.HttpResult:
    return http.get(f"{BASE}{url_path}", params, min_interval=0.55, timeout=25)


def season_games(season_start_year: int, page: int = 1, per_page: int = 250) -> http.HttpResult:
    params = {"seasons[]": str(season_start_year), "per_page": str(per_page),
              "offset": str((page - 1) * per_page), "order_by": "date"}
    return get("/games", params)


def game_boxscore(game_id: int) -> http.HttpResult:
    return get(f"/games/{game_id}")


def season_teams(season_start_year: int, page: int = 1, per_page: int = 100) -> http.HttpResult:
    params = {"seasons[]": str(season_start_year), "per_page": str(per_page),
              "offset": str((page - 1) * per_page)}
    return get("/teams", params)


def season_players(season_start_year: int, page: int = 1, per_page: int = 250) -> http.HttpResult:
    params = {"seasons[]": str(season_start_year), "per_page": str(per_page),
              "offset": str((page - 1) * per_page), "include[]": "games"}
    return get("/players", params)


# ---------------------------------------------------------------- parsing

def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_game_list(js: dict) -> tuple[list[dict], dict]:
    """Normalize a /games page. Returns (games, meta). Never invents values."""
    out = []
    for g in (js or {}).get("data") or []:
        try:
            home = (g.get("home_team") or {}).get("abbreviation")
            away = (g.get("away_team") or {}).get("abbreviation")
            date = (g.get("date") or "")[:10]
            if not (g.get("id") and home and away and date):
                continue
            out.append({
                "bdlt_id": g["id"],
                "season": g.get("season") or "unknown",
                "game_date_et": date,
                "home_team": home, "away_team": away,
                "home_score": _int(g.get("home_score")),
                "away_score": _int(g.get("away_score")),
                "raw_season": (g.get("season") or ""),
            })
        except Exception:
            continue
    meta = (js or {}).get("meta") or {}
    return out, {"total_count": _int(meta.get("total_count")) or 0,
                 "current_page": _int(meta.get("current_page")) or 1}


def parse_boxscore(js: dict) -> dict | None:
    """Normalize a /games/{id} box score.

    Returns {home_team, away_team, game_date_et, season,
             players: [{name, team, minutes, pts, reb, ast, stl, blk, tov,
                        fg3m, fgm, fga, ftm, fta, plus_minus}]} or None when
    the payload shape is not recognized (never partial guesses).
    """
    d = (js or {}).get("data") or {}
    home = (d.get("home_team") or {}).get("abbreviation")
    away = (d.get("away_team") or {}).get("abbreviation")
    date = (d.get("date") or "")[:10]
    if not (home and away and date):
        return None

    def players(side_key: str, team: str) -> list[dict]:
        out = []
        for p in d.get(side_key) or []:
            try:
                name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).strip()
                if not name:
                    name = p.get("name") or ""
            except Exception:
                name = ""
            if not name:
                continue
            out.append({
                "player_id": f"bdlt:{p.get('id')}" if p.get("id") else None,
                "name": name, "team": team,
                "minutes": _float(p.get("minutes")),
                "pts": _int(p.get("points")),
                "reb": _int(p.get("rebounds")),
                "ast": _int(p.get("assists")),
                "stl": _int(p.get("steals")),
                "blk": _int(p.get("blocks")),
                "tov": _int(p.get("turnovers")),
                "fgm": _int(p.get("field_goals_made")),
                "fga": _int(p.get("field_goals_attempted")),
                "fg3m": _int(p.get("three_points_made")),
                "fg3a": _int(p.get("three_points_attempted")),
                "ftm": _int(p.get("free_throws_made")),
                "fta": _int(p.get("free_throws_attempted")),
                "plus_minus": _float(p.get("plus_minus")),
            })
        return out

    return {"home_team": home, "away_team": away, "game_date_et": date,
            "season": d.get("season") or "unknown",
            "players": players("players_home", home) + players("players_away", away)}


def parse_team_season(js: dict) -> list[dict]:
    """Per-team per-season aggregates (totals for the season as reported)."""
    out = []
    for t in (js or {}).get("data") or []:
        ab = (t.get("team") or {}).get("abbreviation") if isinstance(t.get("team"), dict) else None
        ab = ab or t.get("team_abbreviation")
        if not ab or not t.get("season"):
            continue
        out.append({
            "season": t.get("season"), "team": ab,
            "stats_json": __import__("json").dumps({k: v for k, v in t.items()
                                                    if not isinstance(v, (dict, list))},
                                                   default=str),
        })
    return out


def parse_player_season(js: dict) -> list[dict]:
    """Per-player per-season PER-GAME aggregates (include[]=games).

    BallDon'tLie's /players endpoint reports season per-game averages for
    `minutes/points/rebounds/...` — recorded with per_game_basis='per_game'
    so consumers never mistake them for season totals.
    """
    out = []
    for p in (js or {}).get("data") or []:
        try:
            name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).strip()
        except Exception:
            name = ""
        ab = p.get("team_abbreviation") or (p.get("team") or {}).get("abbreviation") if isinstance(p.get("team"), dict) else p.get("team_abbreviation")
        if not (name and ab and p.get("season")):
            continue
        out.append({
            "season": p.get("season"),
            "player_id": f"bdlt:{p.get('id')}" if p.get("id") else f"name:{name}",
            "name": name, "team": ab,
            "games": _int(p.get("games")),
            "minutes": _float(p.get("minutes")),
            "pts": _float(p.get("points")),
            "reb": _float(p.get("rebounds")),
            "ast": _float(p.get("assists")),
            "stl": _float(p.get("steals")),
            "blk": _float(p.get("blocks")),
            "tov": _float(p.get("turnovers")),
            "fg_pct": _float(p.get("field_goals_percent")),
            "fg3_pct": _float(p.get("three_point_field_goals_percent")),
            "ft_pct": _float(p.get("free_throws_percent")),
            "per_game_basis": "per_game",
            "raw": p,
        })
    return out
