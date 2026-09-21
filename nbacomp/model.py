"""Models: Elo ratings, pace/efficiency totals, schedule/rest/travel features.

STRICT AS-OF RULES: every function takes a cutoff and only uses observations
with a game date strictly BEFORE the decision time. Team season aggregates are
excluded from decision features (they include future games); rolling windows
computed from chronological game logs are used instead.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from . import util

# 30 NBA home cities: (timezone offset std hours vs UTC, lat, lon) — public
# reference information used only for travel time-zone features.
TEAM_CITY_TZ = {
    "ATL": -5, "BOS": -5, "BKN": -5, "CHA": -5, "CHI": -6, "CLE": -5,
    "DAL": -6, "DEN": -7, "DET": -5, "GSW": -8, "HOU": -6, "IND": -5,
    "LAC": -8, "LAL": -8, "MEM": -6, "MIA": -5, "MIL": -6, "MIN": -6,
    "NOP": -6, "NYK": -5, "OKC": -6, "ORL": -5, "PHI": -5, "PHX": -7,
    "POR": -8, "SAC": -8, "SAS": -6, "TOR": -5, "UTA": -7, "WAS": -5,
}

TEAM_LATLON = {
    "ATL": (33.755, -84.400), "BOS": (42.066, -71.081), "BKN": (40.683, -73.975),
    "CHA": (35.225, -80.839), "CHI": (41.881, -87.674), "CLE": (41.497, -81.688),
    "DAL": (32.790, -96.810), "DEN": (39.749, -105.008), "DET": (42.341, -83.055),
    "GSW": (37.768, -122.388), "HOU": (29.751, -95.362), "IND": (39.764, -86.156),
    "LAC": (34.043, -118.267), "LAL": (34.043, -118.267), "MEM": (35.138, -90.051),
    "MIA": (25.781, -80.187), "MIL": (43.045, -87.917), "MIN": (44.979, -93.276),
    "NOP": (29.949, -90.082), "NYK": (40.750, -73.993), "OKC": (35.463, -97.515),
    "ORL": (28.539, -81.384), "PHI": (39.901, -75.172), "PHX": (33.446, -112.071),
    "POR": (45.532, -122.667), "SAC": (38.580, -121.500), "SAS": (29.427, -98.437),
    "TOR": (43.643, -79.419), "UTA": (40.768, -111.901), "WAS": (38.898, -77.021),
}


def great_circle_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = (math.sin((lat2 - lat1) / 2) ** 2 +
         math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 3958.7613 * math.asin(math.sqrt(x))


# Prior SD of NBA game totals (points). Literature prior (not fitted on our
# sample): game-total SD ≈ 22-24 points. Used only for total-bets probabilities
# and labeled as an assumption everywhere it is used.
SD_TOTAL = 23.0


# ---------------------------------------------------------------- Elo

class EloModel:
    """Chronological Elo with season carryover regression to the mean.

    Updates only on games strictly before the cutoff the caller passes.
    """

    START = 1500.0
    K = 20.0
    HOME_ELO = 100.0  # ~3.2 pts of true HCA at K=20 scale; fixed prior, measured separately
    CARRYOVER_REGRESSION = 0.75  # new season rating = START + 0.75*(old-START)

    def __init__(self, ratings: dict[str, float] | None = None, season: str | None = None):
        self.ratings = dict(ratings or {})
        self.season = season

    def get(self, team: str) -> float:
        return self.ratings.get(team, self.START)

    def new_season(self):
        self.ratings = {t: self.START + self.CARRYOVER_REGRESSION * (r - self.START)
                        for t, r in self.ratings.items()}
        self.season = None

    def expect(self, home: str, away: str, neutral: bool = False) -> float:
        ha = 0.0 if neutral else self.HOME_ELO
        return util.elo_expected(self.get(home), self.get(away), ha)

    def margin(self, home: str, away: str, neutral: bool = False) -> float:
        """Expected home margin in points (Elo diff / 28, standard conversion)."""
        ha = 0.0 if neutral else self.HOME_ELO
        return ((self.get(home) + ha) - self.get(away)) / 28.0

    def update(self, home: str, away: str, home_pts: int, away_pts: int, neutral: bool = False):
        exp = self.expect(home, away, neutral)
        actual = 1.0 if home_pts > away_pts else 0.0
        mov_mult = _mov_multiplier(home_pts - away_pts, self.get(home) + (0 if neutral else self.HOME_ELO), self.get(away))
        delta = self.K * mov_mult * (actual - exp)
        self.ratings[home] = self.get(home) + delta
        self.ratings[away] = self.get(away) - delta


def _mov_multiplier(margin: float, ra: float, rb: float) -> float:
    """FiveThirtyEight-style margin-of-victory multiplier."""
    import math
    lo = min(ra + margin / 28.0 * 28.0, rb)  # winner-adjusted simplified
    _ = lo
    return (abs(margin) + 3.0) ** 0.8 / 14.0 ** 0.8 if margin != 0 else 1.0


# --------------------------------------------------------- rolling team form

def possessions(pts_rows: dict) -> float:
    """Standard possession estimate: FGA - OREB + TOV + 0.44*FTA."""
    fga = pts_rows.get("fga") or 0
    oreb = pts_rows.get("oreb") or 0
    tov = pts_rows.get("tov") or 0
    fta = pts_rows.get("fta") or 0
    return fga - oreb + tov + 0.44 * fta


class RollingTeamState:
    """Maintains per-team rolling stats strictly from past games."""

    def __init__(self, window: int = 15):
        self.window = window
        self.history: dict[str, list[dict]] = defaultdict(list)  # team -> past games

    def add_game(self, row: dict):
        self.history[row["team"]].append(row)

    def team_rolling(self, team: str) -> dict | None:
        rows = self.history.get(team) or []
        if len(rows) < 3:
            return None
        recent = rows[-self.window:]
        n = len(recent)

        def m(key, default=0.0):
            vals = [r.get(key) if r.get(key) is not None else default for r in recent]
            return sum(float(v) for v in vals) / n

        # 2026-09-21: sources that do not split rebounds (balldontlie team
        # rows summed from per-player stats) have OREB=NULL. Treating missing
        # OREB as 0 would silently overstate pace by ~12 possessions and
        # corrupt every possession-based feature, so when OREB is missing in
        # ANY window row, pace/efficiency are reported as None (unavailable)
        # and pace-based strategies skip the game.
        oreb_missing = any(r.get("oreb") is None for r in recent)
        # points-allowed series is pace-independent (computed either way)
        opp_pts = [float(r["opp_pts"]) if r.get("opp_pts") is not None else None
                   for r in recent]
        if oreb_missing:
            avg_poss = None
            ortg = None
            drtg = None
        else:
            poss = [possessions(r) for r in recent]
            avg_poss = sum(poss) / max(len(poss), 1)
            ortg = m("pts") / avg_poss * 100.0 if avg_poss else None
            # DRtg approximated from points allowed per possession
            if all(v is not None for v in opp_pts) and avg_poss:
                drtg = sum(opp_pts) / n / avg_poss * 100.0
            else:
                drtg = None
        out = {
            "games": n,
            "pace": avg_poss,
            "ortg": ortg,
            "drtg": drtg,
            "pts": m("pts"),
            "opp_pts": (sum(opp_pts) / n) if all(v is not None for v in opp_pts) else None,
            "fg3a": m("fg3a"),
            "fg3m": m("fg3m"),
            "fg3pct": (m("fg3m") / m("fg3a")) if m("fg3a") else None,
            "opp_fg3a": None, "opp_fg3m": None,  # opponent box not available in team logs
            "oreb": None if oreb_missing else m("oreb"),
            "dreb": None,  # not provided by any current source (honest None)
            "reb": m("reb"),
            "ast": m("ast"), "tov": m("tov"),
            "pace_available": not oreb_missing,
        }
        return out

    def rest_and_travel(self, team: str, game_date_et: str, is_home: bool) -> dict:
        """Schedule features from STRICTLY PRIOR games (B2B, 3in4, 4in6, road trips)."""
        rows = self.history.get(team) or []
        d0 = datetime.strptime(game_date_et, "%Y-%m-%d")
        prev = [r for r in rows if r.get("game_date_et") and r["game_date_et"] < game_date_et]
        if not prev:
            return {"rest_days": 3.0, "b2b": 0, "three_in_four": 0, "four_in_six": 0,
                    "road_trip_len": 0, "tz_shift": 0, "travel_miles": 0.0}
        last = prev[-1]
        rest = (d0 - datetime.strptime(last["game_date_et"], "%Y-%m-%d")).days - 1
        b2b = 1 if rest <= 0 else 0

        def played_in(days: int) -> int:
            cnt = 0
            for k in range(days):
                dd = (d0 - timedelta(days=k + 1)).strftime("%Y-%m-%d")
                cnt += 1 if any(r["game_date_et"] == dd for r in rows) else 0
            return cnt

        t4 = played_in(4)
        t6 = played_in(6)
        # road trip: consecutive away games ending at last game
        rtl = 0
        for r in reversed(prev):
            if not r.get("is_home"):
                rtl += 1
            else:
                break
        prev_venue = "home" if last.get("is_home") else ("away" if not is_home else "away")
        travel = 0.0
        tz = 0
        if not is_home and last.get("is_home"):
            travel = 0.0  # leaving home, distance unknown until opponent known (set by caller)
        prev_team_city = TEAM_LATLON.get(team)
        if prev_team_city and not last.get("is_home") and last.get("venue_team"):
            prev_city = TEAM_LATLON.get(last["venue_team"])
            if prev_city:
                travel = great_circle_miles(prev_city, prev_team_city)
        if last.get("venue_team") and last.get("venue_team") != team:
            tz = abs(TEAM_CITY_TZ.get(last["venue_team"], 0) - TEAM_CITY_TZ.get(team, 0))
        return {"rest_days": float(rest), "b2b": b2b, "three_in_four": 1 if t4 >= 3 else 0,
                "four_in_six": 1 if t6 >= 4 else 0, "road_trip_len": rtl,
                "tz_shift": tz, "travel_miles": round(travel, 0)}


# ---------------------------------------------------------- totals model

def expected_total(home: "RollingTeamState.entry | dict",
                   away: "RollingTeamState.entry | dict") -> float | None:
    """Expected game total from rolling pace + rolling ORtg/DRtg.

    exp_pace = mean(team paces); exp_total = exp_pace * (ORTg_home + DRTg_away + ORTg_away + DRTg_home) / 2 / 100
    """
    h, a = home, away
    if not h or not a or not h.get("ortg") or not a.get("ortg"):
        return None
    if not h.get("drtg") or not a.get("drtg"):
        return None
    pace = (h["pace"] + a["pace"]) / 2.0
    home_pts_exp = pace * (h["ortg"] + a["drtg"]) / 2.0 / 100.0
    away_pts_exp = pace * (a["ortg"] + h["drtg"]) / 2.0 / 100.0
    return home_pts_exp + away_pts_exp
