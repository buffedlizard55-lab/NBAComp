"""ESPN public JSON endpoints (site.api.espn.com) — schedule, results, odds, injuries.

Access notes (verified 2026-09-20 against sibling project NBAInjuryReport audits):
- Keyless, no registration. Rate limits unknown -> min_interval enforced.
- Some endpoints have 403'd specific runner fingerprints before; failures are
  logged, never fabricated, and data.nba.net / stats.nba.com act as fallbacks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import http

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/league/nba"

LEAGUE_ID = "00"


def scoreboard(date: str | None = None) -> http.HttpResult:
    """date = YYYYMMDD (ET game date). None = today."""
    params = {"dates": date} if date else None
    return http.get(f"{BASE}/scoreboard", params, min_interval=1.0)


def summary(event_id: str) -> http.HttpResult:
    return http.get(f"{BASE}/summary", {"event": event_id}, min_interval=1.0)


def injuries() -> http.HttpResult:
    return http.get(f"{BASE}/injuries", min_interval=1.0)


def teams() -> http.HttpResult:
    return http.get(f"{BASE}/teams", min_interval=1.0)


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
                    "winner": c.get("winner"),
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
                "game_date_et": (ev.get("date") or "")[:10] if False else _et_date(ev.get("date")),
                "tipoff_utc": ev.get("date"),
                "home_team": home["abbrev"],
                "away_team": away["abbrev"],
                "home_espn_id": home["espn_id"],
                "away_espn_id": away["espn_id"],
                "status": status,
                "neutral_site": 1 if comp.get("neutral") else 0,
                "home_score": _int_or_none(home["score"]) if status == "final" else None,
                "away_score": _int_or_none(away["score"]) if status == "final" else None,
                "source_updated_utc": None,
            }
            odds = comp.get("odds") or ev.get("odds")
            if odds:
                g["_odds"] = _parse_odds(odds[0] if isinstance(odds, list) else odds,
                                         f"espn:{ev.get('id')}")
            out.append(g)
        except Exception:
            continue
    return out


def _season_label(season: dict | None) -> str:
    try:
        y = (season or {}).get("year")
        if not y:
            return "unknown"
        # NBA season label: year is the year the season *ends* (e.g. 2026 -> 2025-26)
        return f"{y - 1}-{str(y)[2:]}"
    except Exception:
        return "unknown"


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
        out = {
            "game_id": game_id,
            "provider": prov,
            "captured_note": "snapshot at fetch time; source timestamps not provided by endpoint",
            "source_updated_utc": None,
        }
        details = o.get("details") or ""
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
        out["details"] = details
        h = o.get("homeTeamOdds") or {}
        a = o.get("awayTeamOdds") or {}
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
        return out if (out.get("total") or out.get("ml_home") or out.get("ml_away")
                       or out.get("spread_home") is not None) else None
    except Exception:
        return None


def parse_injuries(js: dict) -> list[dict]:
    out: list[dict] = []
    for team_block in (js or {}).get("items", []) or []:
        t = team_block.get("team") or {}
        abbrev = t.get("abbreviation") or t.get("shortDisplayName")
        for inj in team_block.get("injuries", []) or []:
            ath = inj.get("athlete") or {}
            item = {
                "player": ath.get("displayName") or "",
                "player_id": f"espn:{ath.get('id')}" if ath.get("id") else None,
                "team": abbrev,
                "status": ((inj.get("status") or {}).get("name") or ""),
                "reason": None,
                "source": "espn",
                "source_url": f"{BASE}/injuries",
                "published_utc": inj.get("date") or ((inj.get("status") or {}).get("date")),
                "note": inj.get("longComment") or inj.get("shortComment") or None,
            }
            if item["player"] and item["status"]:
                out.append(item)
    return out
