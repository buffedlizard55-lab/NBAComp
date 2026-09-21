"""ESPN public JSON endpoints — schedule, results, odds, injuries, box scores.

TRANSPORT NOTES (verified from GitHub Actions runners 2026-09-20, recorded in
data/diagnostics.txt and the research log):
- site.api.espn.com returns Akamai 403 to runner TLS fingerprints (python and
  curl) but answers 200 to Node fetch; site.web.api.espn.com answers 200 to
  plain curl/urllib. We therefore use site.web as PRIMARY and keep site.api as
  fallback for other environments.
- Historical dates work on /scoreboard?dates=YYYYMMDD (final scores persist;
  odds only persist for upcoming/near-term games).
- /summary?event=ID returns full box scores (athletes + statistics) and the
  game's injury listings.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .. import http

BASE_WEB = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba"
BASE = BASE_WEB  # primary host (runner-verified)
BASE_ALT = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"

LEAGUE_ID = "00"


def _get(path: str, params: dict | None = None):
    # Primary: plain urllib against both mirror hosts. As of 2026-09-20 both
    # 403 from GitHub runner IPs (629 consecutive collection_log fails), so a
    # Node-fetch fallback follows (runner-verified 200 for the same URLs).
    # r.transport records which path actually delivered the bytes.
    r = http.get(f"{BASE_WEB}{path}", params, min_interval=0.8)
    if r.ok:
        return r
    if r.status in (0, 403, 404):
        r2 = http.get(f"{BASE_ALT}{path}", params, min_interval=0.8)
        if r2.ok:
            return r2
    if r.status in (0, 403):
        r3 = http.node_fetch(f"{BASE_ALT}{path}", params)
        if r3.ok:
            return r3
        # last resort: node against the primary host too
        r4 = http.node_fetch(f"{BASE_WEB}{path}", params)
        if r4.ok:
            return r4
    return r


def scoreboard(date: str | None = None):
    """date = YYYYMMDD (ET game date). None = today."""
    return _get("/scoreboard", {"dates": date} if date else None)


def summary(event_id: str):
    return _get("/summary", {"event": event_id})


def injuries():
    return _get("/injuries")


def teams():
    return _get("/teams")


# ---------------------------------------------------------------- parsing

def parse_scoreboard(js: dict) -> list[dict]:
    """Normalize scoreboard events into game dicts (no scores invented)."""
    out: list[dict] = []
    for ev in (js or {}).get("events", []) or []:
        try:
            comp = (ev.get("competitions") or [{}])[0]
            home = away = None
            for c in comp.get("competitors", []) or []:
                side = {
                    "espn_id": c.get("id"),
                    "abbrev": ((c.get("team") or {}).get("abbreviation") or
                               (c.get("team") or {}).get("shortDisplayName")),
                    "name": (c.get("team") or {}).get("displayName"),
                    "score": (c.get("score") if isinstance(c.get("score"), str)
                              else ((c.get("score") or {}).get("displayValue"))),
                }
                if c.get("homeAway") == "home":
                    home = side
                else:
                    away = side
            if not home or not away:
                continue
            status_type = ((ev.get("status") or {}).get("type") or {})
            state = status_type.get("state")  # pre|in|post
            status = {"pre": "scheduled", "in": "in", "post": "final"}.get(state, state)
            g = {
                "game_id": f"espn:{ev.get('id')}",
                "source": "espn",
                "season": _season_label(ev.get("season")),
                "season_type": _season_type_label(ev.get("season")),
                "game_date_et": _et_date(ev.get("date")),
                "tipoff_utc": ev.get("date"),
                "home_team": home["abbrev"],
                "away_team": away["abbrev"],
                "status": status,
                "neutral_site": 1 if comp.get("neutral") else 0,
                "home_score": _int_or_none(home["score"]) if status == "final" else None,
                "away_score": _int_or_none(away["score"]) if status == "final" else None,
            }
            odds = comp.get("odds") or ev.get("odds")
            if odds:
                g["_odds"] = _parse_odds(odds[0] if isinstance(odds, list) else odds,
                                         f"espn:{ev.get('id')}")
            qs = parse_quarters(ev)
            if qs:
                g["_quarters"] = qs
            out.append(g)
        except Exception:
            continue
    return out


def parse_quarters(ev: dict) -> list[tuple[int, int, int]]:
    """Per-quarter cumulative team scores from an in-game scoreboard event.

    Returns [(quarter, home_cum, away_cum), ...] ONLY from an observed
    `scores` list on the competitors (e.g. {"value":"21","detail":"Q1"}).
    When the shape is not recognized the function returns [] — quarter data
    is never reconstructed from anything else (1H/quarter settlement
    depends on this being real).
    """
    try:
        comp = (ev.get("competitions") or [{}])[0]
        qh: dict[int, int] = {}
        qa: dict[int, int] = {}
        for c in comp.get("competitors", []) or []:
            scores = c.get("scores")
            if not isinstance(scores, list):
                return []
            target = qh if c.get("homeAway") == "home" else qa
            for s in scores:
                detail = str(s.get("detail") or "")
                m = re.match(r"([1-9])", detail)
                if not m:
                    return []
                try:
                    target[int(m.group(1))] = int(str(s.get("value")))
                except (TypeError, ValueError):
                    return []
        if not qh or not qa:
            return []
        out = []
        for q in sorted(set(qh) & set(qa)):
            out.append((q, qh[q], qa[q]))
        return out
    except Exception:
        return []


def _season_label(season: dict | None) -> str:
    try:
        y = (season or {}).get("year")
        if not y:
            return "unknown"
        return f"{y - 1}-{str(y)[2:]}"
    except Exception:
        return "unknown"


def _season_type_label(season: dict | None) -> str | None:
    """ESPN season.type -> preseason|regular|postseason (None when absent).

    ESPN encodes season type as 1=preseason, 2=regular season, 3=postseason
    and also exposes a slug. Nothing is inferred when the field is missing:
    the value stays None ("unrecorded") rather than being guessed from dates,
    because a wrong preseason/regular label would silently corrupt every
    rolling feature built from it.
    """
    try:
        st = (season or {}).get("type")
        slug = str((season or {}).get("slug") or "")
        if isinstance(st, str) and st.isdigit():
            st = int(st)
        if st == 1 or slug in ("preseason", "pre-season"):
            return "preseason"
        if st == 2 or slug in ("regular-season", "regular"):
            return "regular"
        if st in (3, 4) or slug in ("postseason", "post-season", "playoffs"):
            return "postseason"
        return None
    except Exception:
        return None


def _et_date(iso_date: str | None) -> str | None:
    dt = _parse(iso_date)
    if not dt:
        return None
    return (dt - timedelta(hours=5)).strftime("%Y-%m-%d")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _int_or_none(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_odds(o: dict, game_id: str) -> dict | None:
    try:
        prov = ((o.get("provider") or {}).get("name") or "unknown-provider")
        out = {"game_id": game_id, "provider": prov, "source_updated_utc": None}
        ou = o.get("overUnder")
        spread = o.get("spread")
        if ou is not None:
            try:
                out["total"] = float(ou)
            except (TypeError, ValueError):
                pass
        if spread is not None:
            try:
                out["spread_home"] = float(spread)
            except (TypeError, ValueError):
                pass
        # Total price (both sides). ESPN exposes `overOdds`/`underOdds` for
        # many events; when present these are REAL observed prices, which
        # replaces the blind -110 assumption for totals on those games.
        for key, src_key in (("over_odds", "overOdds"), ("under_odds", "underOdds")):
            v = o.get(src_key)
            if v is None:
                v = ((o.get("current") or {}).get(src_key)
                     if isinstance(o.get("current"), dict) else None)
            if isinstance(v, (int, float, str)):
                try:
                    out[key] = int(float(v))
                except (TypeError, ValueError):
                    pass
        h = o.get("homeTeamOdds") or {}
        a = o.get("awayTeamOdds") or {}
        if not isinstance(h, dict):
            h = {}
        if not isinstance(a, dict):
            a = {}
        if isinstance(h.get("moneyLine"), (int, float)):
            out["ml_home"] = int(h["moneyLine"])
        if isinstance(a.get("moneyLine"), (int, float)):
            out["ml_away"] = int(a["moneyLine"])
        opening = o.get("open") or {}
        if isinstance(opening, dict):
            if opening.get("overUnder") is not None:
                try:
                    out["open_total"] = float(opening["overUnder"])
                except (TypeError, ValueError):
                    pass
            if opening.get("spread") is not None:
                try:
                    out["open_spread_home"] = float(opening["spread"])
                except (TypeError, ValueError):
                    pass
        return out if (out.get("total") or out.get("ml_home") is not None
                       or out.get("ml_away") is not None
                       or out.get("spread_home") is not None) else None
    except Exception:
        return None


def parse_injuries(js: dict) -> list[dict]:
    """Shape-agnostic: find every {team, injuries[]} block in the payload."""
    out: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            inj = node.get("injuries")
            if isinstance(inj, list) and node.get("team"):
                t = node["team"] or {}
                abbrev = t.get("abbreviation") or t.get("shortDisplayName")
                for i in inj:
                    ath = i.get("athlete") or {}
                    item = {
                        "player": ath.get("displayName") or "",
                        "player_id": f"espn:{ath.get('id')}" if ath.get("id") else None,
                        "team": abbrev,
                        "status": ((i.get("status") or {}).get("name") or ""),
                        "source": "espn",
                        "source_url": f"{BASE}/injuries",
                        "published_utc": i.get("date") or ((i.get("status") or {}).get("date")),
                        "note": i.get("longComment") or i.get("shortComment") or None,
                        "game_date": None,
                    }
                    if item["player"] and item["status"]:
                        out.append(item)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(js or {})
    return out


# ------------------------------------------------------------- box scores

def _game_meta(js: dict) -> tuple[str | None, str, dict[str, dict]]:
    """(game_id, season, {abbrev: {home, score}}) from a summary payload."""
    game_info = js.get("header") or {}
    game_id = js.get("gameId") or game_info.get("id")
    season_year = None
    try:
        season_year = ((game_info.get("season") or {}).get("year"))
    except Exception:
        pass
    season = f"{season_year - 1}-{str(season_year)[2:]}" if season_year else "unknown"
    meta: dict[str, dict] = {}
    try:
        for c in ((game_info.get("competitions") or [{}])[0].get("competitors") or []):
            ab = (c.get("team") or {}).get("abbreviation")
            if ab:
                meta[ab] = {"home": c.get("homeAway") == "home", "score": c.get("score")}
    except Exception:
        pass
    return (str(game_id) if game_id else None), season, meta


def _split_made_att(v: str) -> tuple[int | None, int | None]:
    try:
        a, b = str(v).split("-")
        return int(float(a)), int(float(b))
    except (TypeError, ValueError):
        return None, None


def parse_boxscore(js: dict) -> list[dict]:
    """Per-player game logs from a summary payload (verified real shape):

    boxscore.players[] = {team:{abbreviation}, statistics[]} where each
    statistics entry has names[] like MIN,PTS,FG,3PT,FT,REB,AST,TO,STL,BLK,
    OREB,DREB,PF,+/- and athletes[] = {athlete:{id,displayName}, starter,
    didNotPlay, reason, stats[]}. FG/3PT/FT values are "made-att" strings.
    """
    out: list[dict] = []
    box = (js or {}).get("boxscore") or {}
    game_id, season, _meta = _game_meta(js)
    for team_block in box.get("players", []) or []:
        team = ((team_block.get("team") or {}).get("abbreviation")) or ""
        for stat_group in team_block.get("statistics", []) or []:
            names = [str(x).upper() for x in (stat_group.get("names") or [])]
            if not names:
                continue
            for entry in stat_group.get("athletes", []) or []:
                ath = entry.get("athlete") or {}
                name = ath.get("displayName") or ""
                if not name:
                    continue
                stats_raw = entry.get("stats") or []
                dnp = bool(entry.get("didNotPlay"))
                row = {
                    "season": season,
                    "game_id": f"espn:{game_id}" if game_id else None,
                    "player": name,
                    "player_id": f"espn:{ath.get('id')}" if ath.get("id") else None,
                    "team": team,
                    "status": "dnp" if dnp else ("started" if entry.get("starter") else "bench"),
                    "minutes": None, "pts": None, "reb": None, "ast": None,
                    "stl": None, "blk": None, "tov": None, "fg3m": None,
                    "fgm": None, "fga": None, "ftm": None, "fta": None,
                    "plus_minus": None,
                }
                if stats_raw and not dnp:
                    for i, val in enumerate(stats_raw):
                        key = names[i] if i < len(names) else ""
                        try:
                            if key == "MIN":
                                row["minutes"] = _minutes(val)
                            elif key == "PTS":
                                row["pts"] = int(float(val))
                            elif key == "REB":
                                row["reb"] = int(float(val))
                            elif key == "AST":
                                row["ast"] = int(float(val))
                            elif key == "STL":
                                row["stl"] = int(float(val))
                            elif key == "BLK":
                                row["blk"] = int(float(val))
                            elif key == "TO":
                                row["tov"] = int(float(val))
                            elif key == "FG":
                                row["fgm"], row["fga"] = _split_made_att(val)
                            elif key == "3PT":
                                row["fg3m"], row["fg3a"] = _split_made_att(val)
                            elif key == "FT":
                                row["ftm"], row["fta"] = _split_made_att(val)
                            elif key == "+/-":
                                row["plus_minus"] = float(val)
                        except (TypeError, ValueError):
                            continue
                if row["game_id"]:
                    out.append(row)
    return out


def _minutes(v) -> float | None:
    try:
        if isinstance(v, str) and ":" in v:
            mm, ss = v.split(":")
            return round(int(mm) + int(ss) / 60.0, 1)
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_team_boxscore(js: dict) -> list[dict]:
    """Per-team box lines from the summary payload (verified real shape):

    boxscore.teams[] = {team:{abbreviation}, statistics:[{name, label,
    displayValue}]} with FG/3PT/FT as "made-att" and OR/TO as ints. Used for
    possession-based pace/efficiency features.
    """
    out: list[dict] = []
    box = (js or {}).get("boxscore") or {}
    game_id, season, meta = _game_meta(js)
    for team_block in box.get("teams", []) or []:
        team = ((team_block.get("team") or {}).get("abbreviation")) or ""
        row = {"season": season, "game_id": f"espn:{game_id}" if game_id else None,
               "team": team, "fga": None, "fg3a": None, "oreb": None, "tov": None,
               "fta": None, "pts": None, "fgm": None, "fg3m": None, "ftm": None,
               "reb": None, "ast": None, "is_home": None, "score": None}
        for st in team_block.get("statistics", []) or []:
            name = str(st.get("name") or "")
            val = st.get("displayValue")
            try:
                if name == "fieldGoalsMade-fieldGoalsAttempted":
                    row["fgm"], row["fga"] = _split_made_att(val)
                elif name == "threePointFieldGoalsMade-threePointFieldGoalsAttempted":
                    row["fg3m"], row["fg3a"] = _split_made_att(val)
                elif name == "freeThrowsMade-freeThrowsAttempted":
                    row["ftm"], row["fta"] = _split_made_att(val)
                elif name == "totalRebounds":
                    row["reb"] = int(float(val))
                elif name == "offensiveRebounds":
                    row["oreb"] = int(float(val))
                elif name == "assists":
                    row["ast"] = int(float(val))
                elif name == "turnovers":
                    row["tov"] = int(float(val))
            except (TypeError, ValueError):
                continue
        m = meta.get(team) or {}
        row["is_home"] = 1 if m.get("home") else 0
        try:
            row["score"] = int(float(m.get("score"))) if m.get("score") is not None else None
        except (TypeError, ValueError):
            pass
        # `pts` is what every consumer reads (collect_boxscores writes it into
        # team_gamelogs, RollingTeamState computes pace/efficiency from it).
        # It was left None while the identical value sat in `score`, so run
        # 35553997534 stored 46 team_gamelogs rows with pts=NULL.
        if row["pts"] is None:
            row["pts"] = row["score"]
        if row["game_id"]:
            out.append(row)
    return out
