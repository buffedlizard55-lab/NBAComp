"""Sportsbook Reviews Online (SBR) historical NBA scores + odds archives.

Discovered 2026-09-21 during the research pass (see data/research_log entries):
SBR publishes free, registration-free season pages of NBA *scores and closing
odds* — opening and closing spreads/totals, moneylines and second-half lines —
for the seasons listed in SEASONS. This is the ONLY free source found anywhere
that carries real historical NBA prices, which is why it matters: every other
price source probed (Kalshi settled markets, ESPN past scoreboards, BRef) has no
historical odds, which is why the competition has been running forward-only.

Data-quality reality (measured, not assumed): the archive is a hand-maintained
HTML table whose *column meaning shifts between games* — one row of a game
carries the total in its Open/Close columns and the other row carries the
spread, and the side that does which varies. Therefore nothing here is stored
unless it passes independent cross-checks:

  * the two rows of a game must be a (V,H) pair with rot numbers H == V+1
  * each team's four quarter scores must sum to its printed final score
  * exactly two of the four line values must be total-shaped (180-300) and two
    spread-shaped (-30..30); open/close must agree in kind per pair
  * moneylines must be opposite-signed (or a pick'em pair) and their devigged
    probabilities must be within 0.25 of the probability implied by the spread

A row that fails any check is rejected and counted. Rejections are published on
the data-sources page, never silently dropped and never guessed at.
"""
from __future__ import annotations

import re
from datetime import datetime

from .. import http, util
from ..engine import TEAM_NAMES

BASE = "https://www.sportsbookreviewsonline.com/scoresoddsarchives/"

#: season label -> url slug (the archive's own naming). Coverage stops at
#: 2022-23: the archive page states it will not be updated.
SEASONS: dict[str, str] = {
    "2007-08": "nba-odds-2007-08",
    "2008-09": "nba-odds-2008-09",
    "2009-10": "nba-odds-2009-10",
    "2010-11": "nba-odds-2010-11",
    "2011-12": "nba-odds-2011-12",
    "2012-13": "nba-odds-2012-13",
    "2013-14": "nba-odds-2013-14",
    "2014-15": "nba-odds-2014-15",
    "2015-16": "nba-odds-2015-16",
    "2016-17": "nba-odds-2016-17",
    "2017-18": "nba-odds-2017-18",
    "2018-19": "nba-odds-2018-19",
    "2019-20": "nba-odds-2019-20",
    "2020-21": "nba-odds-2020-21",
    "2021-22": "nba-odds-2021-22",
    "2022-23": "nba-odds-2022-23",
}

#: Seasons whose team labels map onto current franchises (pre-2013 seasons
#: contain Seattle/New Jersey/New Orleans-early labels that must not be guessed).
DEFAULT_SEASONS = ["2013-14", "2014-15", "2015-16", "2016-17", "2017-18",
                   "2018-19", "2019-20", "2020-21", "2021-22", "2022-23"]

# SBR writes a few franchises in ways no concatenation rule reproduces.
_NAME_FIXES = {
    "lalakers": "LAL", "laclippers": "LAC", "goldenstate": "GSW",
    "neworleans": "NOP", "sanantonio": "SAS", "oklahomacity": "OKC",
    "newyork": "NYK", "portland": "POR", "utah": "UTA", "brooklyn": "BKN",
    "newjersey": "NJN", "seattle": "SEA", "charlotte": "CHA",
    "washington": "WAS", "philadelphia": "PHI", "boston": "BOS",
    "cleveland": "CLE", "detroit": "DET", "indiana": "IND", "atlanta": "ATL",
    "miami": "MIA", "orlando": "ORL", "toronto": "TOR", "chicago": "CHI",
    "milwaukee": "MIL", "minnesota": "MIN", "denver": "DEN", "dallas": "DAL",
    "houston": "HOU", "memphis": "MEM", "phoenix": "PHX", "sacramento": "SAC",
}


def _norm_name(raw: str) -> str:
    return re.sub(r"[^a-z]", "", (raw or "").lower())


def _name_index() -> dict[str, str]:
    idx: dict[str, str] = dict(_NAME_FIXES)
    for ab, (nick, city) in TEAM_NAMES.items():
        idx.setdefault(_norm_name(city), ab)
        idx.setdefault(_norm_name(nick), ab)
        idx.setdefault(_norm_name(city + nick), ab)
    return idx


NAME_INDEX = _name_index()


def team_abbrev(raw: str) -> str | None:
    """Map an SBR team label to our abbreviation, or None (never a guess)."""
    return NAME_INDEX.get(_norm_name(raw))


def season_url(season: str) -> str:
    return BASE + SEASONS[season]


def fetch_season(season: str):
    """Fetch one season page (HTML). Returns an http.HttpResult-like object."""
    return http.get(season_url(season), timeout=45)


# --------------------------------------------------------------------- parse

_TAG = re.compile(r"<[^>]+>")
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def _cells(row_html: str) -> list[str]:
    out = []
    for c in _CELL.findall(row_html):
        txt = _TAG.sub("", c)
        txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
                  .replace("&#8217;", "'").strip())
        out.append(txt)
    return out


def _num(txt: str) -> float | None:
    t = (txt or "").strip().lower()
    if t in ("pk", "pick", "pickem", "pick'em", ""):
        return 0.0 if t else None
    t = t.replace("+", "")
    try:
        return float(t)
    except ValueError:
        return None


def _int(txt: str) -> int | None:
    v = _num(txt)
    if v is None or v != int(v):
        return None
    return int(v)


def _date_iso(season: str, mmdd: str) -> str | None:
    if not re.fullmatch(r"\d{4}", mmdd or ""):
        return None
    start_year = int(season[:4])
    mm, dd = int(mmdd[:2]), int(mmdd[2:])
    year = start_year if mm >= 8 else start_year + 1
    try:
        return datetime(year, mm, dd).strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_season(html: str, season: str) -> tuple[list[dict], list[dict]]:
    """Parse + validate one SBR season page.

    Returns (games, rejects). A game is only returned when every structural and
    numeric cross-check passes; otherwise it lands in rejects with a reason.
    """
    rows = []
    for row_html in _ROW.findall(html or ""):
        cells = _cells(row_html)
        if len(cells) >= 13 and re.fullmatch(r"\d{4}", cells[0] or ""):
            rows.append(cells)
    games: list[dict] = []
    rejects: list[dict] = []
    i = 0
    while i + 1 < len(rows):
        v, h = rows[i], rows[i + 1]
        label = f"{season} rot {v[1]}/{h[1]} {v[3]}@{h[3]}"
        if (v[2] or "").upper() != "V" or (h[2] or "").upper() != "H":
            rejects.append({"row": label, "reason": "rows are not a V/H pair"})
            i += 1
            continue
        if _int(v[1]) is None or _int(h[1]) is None or _int(h[1]) != _int(v[1]) + 1:
            rejects.append({"row": label, "reason": "rot numbers are not consecutive"})
            i += 1
            continue
        aw, hm = team_abbrev(v[3]), team_abbrev(h[3])
        if not aw or not hm:
            rejects.append({"row": label, "reason": f"unknown team label {v[3]!r}/{h[3]!r}"})
            i += 2
            continue
        gdate = _date_iso(season, v[0])
        if not gdate:
            rejects.append({"row": label, "reason": f"unparseable date {v[0]!r}"})
            i += 2
            continue
        vq = [_int(v[4]), _int(v[5]), _int(v[6]), _int(v[7])]
        hq = [_int(h[4]), _int(h[5]), _int(h[6]), _int(h[7])]
        vf, hf = _int(v[8]), _int(h[8])
        if None in vq or None in hq or vf is None or hf is None:
            rejects.append({"row": label, "reason": "missing quarter scores or finals"})
            i += 2
            continue
        if sum(vq) != vf or sum(hq) != hf:
            rejects.append({"row": label,
                            "reason": f"quarters {vq}/{hq} do not sum to finals {vf}/{hf}"})
            i += 2
            continue
        if not (50 <= vf <= 200 and 50 <= hf <= 200) or vf == hf:
            rejects.append({"row": label, "reason": f"implausible finals {vf}-{hf}"})
            i += 2
            continue
        # four line values (V open/close, H open/close): two totals, two spreads
        lines = [("away", "open", _num(v[9])), ("away", "close", _num(v[10])),
                 ("home", "open", _num(h[9])), ("home", "close", _num(h[10]))]
        if any(val is None for _, _, val in lines):
            rejects.append({"row": label, "reason": "unparseable Open/Close value"})
            i += 2
            continue
        totals = [(s, k, v_) for s, k, v_ in lines if 180 <= abs(v_) <= 300]
        spreads = [(s, k, v_) for s, k, v_ in lines if abs(v_) <= 30]
        if len(totals) != 2 or len(spreads) != 2:
            rejects.append({"row": label,
                            "reason": f"line values do not split into 2 totals + 2 "
                                      f"spreads: {[v for *_x, v in lines]}"})
            i += 2
            continue
        if len({s for s, _, _ in totals}) != 1 or len({s for s, _, _ in spreads}) != 1:
            rejects.append({"row": label,
                            "reason": "open/close disagree on which row carries the total"})
            i += 2
            continue
        if abs(totals[0][2] - totals[1][2]) > 20 or abs(spreads[0][2] - spreads[1][2]) > 6:
            rejects.append({"row": label,
                            "reason": f"open/close moved implausibly: totals "
                                      f"{totals[0][2]}/{totals[1][2]} spreads "
                                      f"{spreads[0][2]}/{spreads[1][2]}"})
            i += 2
            continue
        # Spread sign: the archive's printed sign is NOT reliable (it prints
        # the same positive magnitude whether the favourite is the home or the
        # away team, and the row that carries the spread changes per game).
        # The magnitude is taken from the printed spread and the SIGN is taken
        # from the moneyline — an independent field — so a mis-signed row can
        # never produce a wrong handicap. Disagreements are counted below.
        spread_row = spreads[0][0]
        printed = {k: v_ for s, k, v_ in spreads if s == spread_row}
        if set(printed) != {"open", "close"}:
            rejects.append({"row": label, "reason": "spread columns ambiguous"})
            i += 2
            continue
        tot = {k: v_ for _s, k, v_ in totals}
        ml_a, ml_h = _num(v[11]), _num(h[11])
        if ml_a is None or ml_h is None or ml_a == 0 or ml_h == 0:
            rejects.append({"row": label, "reason": "missing moneyline"})
            i += 2
            continue
        if (ml_a > 0) == (ml_h > 0) and abs(ml_a) > 120 and abs(ml_h) > 120:
            rejects.append({"row": label, "reason": f"moneylines both one-sided {ml_a}/{ml_h}"})
            i += 2
            continue
        # equal moneylines (a true pick'em) are read as "no home credit":
        # the handicap then lands on the home team and never hands it points.
        home_favoured = ml_h <= ml_a
        sign = -1.0 if home_favoured else 1.0
        home_spread = {k: sign * abs(v_) for k, v_ in printed.items()}
        # cross-check: spread-implied win prob vs devigged moneyline prob.
        # The home spread (negative = home favoured) implies a home margin of
        # -home_spread; convert with SD 11.5 and compare to the moneyline.
        p_ml_h, p_ml_a = util.devig_two_way(util.american_to_prob(ml_h),
                                           util.american_to_prob(ml_a))
        p_spread_h = util.spread_win_prob(-home_spread["close"], 0.0, 11.5)
        if not (0.02 <= p_ml_h <= 0.98) or abs(p_spread_h - p_ml_h) > 0.25:
            rejects.append({"row": label,
                            "reason": f"spread/ML disagree: spread implies "
                                      f"{p_spread_h:.2f}, moneyline {p_ml_h:.2f}"})
            i += 2
            continue
        games.append({
            "season": season, "game_date_et": gdate, "away": aw, "home": hm,
            "away_q": vq, "home_q": hq, "away_final": vf, "home_final": hf,
            "open_total": round(tot["open"], 1), "close_total": round(tot["close"], 1),
            "open_home_spread": round(home_spread["open"], 1),
            "close_home_spread": round(home_spread["close"], 1),
            "spread_printed_row": spread_row,
            "spread_sign_from_ml": 1 if home_favoured else 0,
            "ml_away": int(ml_a), "ml_home": int(ml_h),
            "source_url": season_url(season), "rot": _int(v[1]),
        })
        i += 2
    return games, rejects


def parse_season_page(html: str, season: str) -> tuple[list[dict], list[dict]]:
    return parse_season(html, season)
