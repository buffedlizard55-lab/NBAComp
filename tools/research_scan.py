"""Empirical research scan over VERIFIED game results only.

Everything in here is computed from `games` rows whose `status='final'` and
whose scores exist. No prices are used or invented: this tool measures
*basketball facts* (totals, margins, rest effects, month effects), not P&L.
Any hypothesis that survives here is still only a hypothesis until it is
priced and forward-tested.

Output: JSON on stdout (and optionally to a file) so results can be stored in
`research_log` / published on the site. Every number carries its sample size.

Usage:
    python tools/research_scan.py [--out data/research_scan.json]
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import db  # noqa: E402

REG_SEASON_START = {"2023-24": "2023-10-24", "2024-25": "2024-10-22",
                    "2025-26": "2025-10-21", "2026-27": "2026-10-20"}
REG_SEASON_END = {"2023-24": "2024-04-14", "2024-25": "2025-04-13",
                  "2025-26": "2026-04-12", "2026-27": "2027-04-11"}


def _mean(xs):
    return round(st.mean(xs), 3) if xs else None


def _sd(xs):
    return round(st.pstdev(xs), 3) if len(xs) > 1 else None


def _welch(a, b):
    """Welch t-statistic for difference of means (no scipy dependency)."""
    if len(a) < 3 or len(b) < 3:
        return None
    ma, mb = st.mean(a), st.mean(b)
    va, vb = st.variance(a) / len(a), st.variance(b) / len(b)
    if va + vb <= 0:
        return None
    return round((ma - mb) / (va + vb) ** 0.5, 3)


def load_finals(con):
    return [dict(r) for r in con.execute(
        "SELECT game_id, season, game_date_et, home_team, away_team, home_score, away_score "
        "FROM games WHERE status='final' AND home_score IS NOT NULL AND away_score IS NOT NULL "
        "ORDER BY game_date_et, game_id")]


def is_regular(g, season_start, season_end) -> bool:
    """Regular season only: the source does not label preseason/postseason.

    Bounds are the PUBLISHED NBA regular-season dates for each season
    (league announcements / Basketball-Reference schedule pages). Games
    outside them are preseason exhibitions or playoffs and are excluded from
    month/rest effect measurement. This is a factual calendar, not a guess:
    any game outside the window is simply not classified as regular season.
    """
    return season_start <= g["game_date_et"] <= season_end


def scan(con) -> dict:
    finals = load_finals(con)
    reg = [g for g in finals
           if is_regular(g, REG_SEASON_START.get(g["season"], "9999"),
                         REG_SEASON_END.get(g["season"], "0000"))]
    out = {
        "generated_utc": None,
        "source": "games table (ESPN + Basketball-Reference cross-verified subset)",
        "n_finals_all": len(finals),
        "n_finals_regular_season": len(reg),
        "regular_season_bounds": {s: [REG_SEASON_START[s], REG_SEASON_END[s]]
                                  for s in REG_SEASON_START},
        "note": ("Outcome-only measurements. No odds, no prices, no P&L. "
                 "Regular-season windows are the published NBA calendars; "
                 "preseason and playoff games are excluded from these windows."),
    }
    # ---------------------------------------------------------------- overall
    totals = [g["home_score"] + g["away_score"] for g in reg]
    margins = [g["home_score"] - g["away_score"] for g in reg]
    out["regular_season_baseline"] = {
        "n": len(reg), "avg_total": _mean(totals), "sd_total": _sd(totals),
        "avg_home_margin": _mean(margins), "sd_margin": _sd(margins),
        "home_win_pct": _mean([1.0 if m > 0 else 0.0 for m in margins]),
    }

    # ------------------------------------------------- 1. opening-fortnight
    # Hypothesis H1: the first days of a season produce lower scoring than the
    # rest of the same season (defenses ahead of offenses, new rotations,
    # foul/travel rules applying).
    per_season = {}
    first_pool, rest_pool = [], []
    for s in sorted({g["season"] for g in reg}):
        gs = [g for g in reg if g["season"] == s]
        if not gs:
            continue
        start = REG_SEASON_START.get(s)
        if not start:
            continue
        d0 = datetime.strptime(start, "%Y-%m-%d")
        cut = (d0 + timedelta(days=6)).strftime("%Y-%m-%d")
        early = [g for g in gs if g["game_date_et"] <= cut]
        rest = [g for g in gs if g["game_date_et"] > cut]
        fe = [g["home_score"] + g["away_score"] for g in early]
        fr = [g["home_score"] + g["away_score"] for g in rest]
        first_pool += fe
        rest_pool += fr
        per_season[s] = {"n_early": len(fe), "avg_early": _mean(fe),
                         "n_rest": len(fr), "avg_rest": _mean(fr)}
    out["H1_opening_week_low_scoring"] = {
        "definition": "first 6 days from the published season start vs the rest of the regular season",
        "per_season": per_season,
        "pooled_early": {"n": len(first_pool), "avg_total": _mean(first_pool)},
        "pooled_rest": {"n": len(rest_pool), "avg_total": _mean(rest_pool)},
        "difference_early_minus_rest": (round(_mean(first_pool) - _mean(rest_pool), 3)
                                        if first_pool and rest_pool else None),
        "welch_t": _welch(first_pool, rest_pool),
    }

    # ------------------------------------------------- 2. back-to-back effect
    # Hypothesis H2: a team on the 2nd night of a back-to-back scores less,
    # allows more, and loses by more than its season baseline.
    last_team_game: dict[str, str] = {}
    b2b_team_games, nonb2b_team_games = [], []
    for g in reg:
        for team, opp, pts, opp_pts in (
                (g["home_team"], g["away_team"], g["home_score"], g["away_score"]),
                (g["away_team"], g["home_team"], g["away_score"], g["home_score"])):
            prev = last_team_game.get(team)
            if prev is not None:
                days = (datetime.strptime(g["game_date_et"], "%Y-%m-%d")
                        - datetime.strptime(prev, "%Y-%m-%d")).days
                rec = {"team": team, "pts": pts, "opp_pts": opp_pts,
                       "net": pts - opp_pts, "rest_days": days - 1}
                if days - 1 <= 0:
                    b2b_team_games.append(rec)
                else:
                    nonb2b_team_games.append(rec)
        last_team_game[g["home_team"]] = g["game_date_et"]
        last_team_game[g["away_team"]] = g["game_date_et"]
    out["H2_back_to_back"] = {
        "definition": "team-games with 0 rest days (played previous calendar day) vs all others",
        "b2b": {"n": len(b2b_team_games),
                "avg_pts": _mean([r["pts"] for r in b2b_team_games]),
                "avg_net": _mean([r["net"] for r in b2b_team_games])},
        "non_b2b": {"n": len(nonb2b_team_games),
                    "avg_pts": _mean([r["pts"] for r in nonb2b_team_games]),
                    "avg_net": _mean([r["net"] for r in nonb2b_team_games])},
        "net_point_cost_of_b2b": (
            round(_mean([r["net"] for r in b2b_team_games])
                  - _mean([r["net"] for r in nonb2b_team_games]), 3)
            if b2b_team_games and nonb2b_team_games else None),
        "welch_t_net": _welch([r["net"] for r in b2b_team_games],
                              [r["net"] for r in nonb2b_team_games]),
    }

    # ------------------------------------------- 3. rest asymmetry on totals
    last_team_game = {}
    sym, asym = [], []
    for g in reg:
        rest = {}
        for team in (g["home_team"], g["away_team"]):
            prev = last_team_game.get(team)
            rest[team] = (
                (datetime.strptime(g["game_date_et"], "%Y-%m-%d")
                 - datetime.strptime(prev, "%Y-%m-%d")).days - 1 if prev else None)
        rh, ra = rest[g["home_team"]], rest[g["away_team"]]
        total = g["home_score"] + g["away_score"]
        if rh is not None and ra is not None and 0 <= rh <= 6 and 0 <= ra <= 6:
            (asym if abs(rh - ra) >= 2 else sym).append(total)
        last_team_game[g["home_team"]] = g["game_date_et"]
        last_team_game[g["away_team"]] = g["game_date_et"]
    out["H3_rest_asymmetry_totals"] = {
        "definition": "|rest_home - rest_away| >= 2 days vs < 2 days (both teams 0-6 rest days)",
        "asymmetric": {"n": len(asym), "avg_total": _mean(asym)},
        "symmetric": {"n": len(sym), "avg_total": _mean(sym)},
        "difference": (round(_mean(asym) - _mean(sym), 3) if asym and sym else None),
        "welch_t": _welch(asym, sym),
    }

    # ------------------------------------------ 4. month-by-month scoring drift
    by_month = defaultdict(list)
    for g in reg:
        by_month[g["game_date_et"][:7]].append(g["home_score"] + g["away_score"])
    out["H4_month_scoring"] = {m: {"n": len(v), "avg_total": _mean(v)}
                               for m, v in sorted(by_month.items())}

    # --------------------------------------------- 5. home underdog behaviour
    # No historical lines exist, so "underdog" is proxied by a team's
    # pre-game win record within the season (strictly prior games). This is a
    # deliberate, disclosed proxy - not a spread.
    wins = defaultdict(int)
    losses = defaultdict(int)
    dog_rows, fav_rows = [], []
    for g in reg:
        hw, aw = wins[g["home_team"]], wins[g["away_team"]]
        hl, al = losses[g["home_team"]], losses[g["away_team"]]
        if hw + hl >= 5 and aw + al >= 5:
            hwp = hw / (hw + hl)
            awp = aw / (aw + al)
            if hwp < awp:
                dog_rows.append(g["home_score"] - g["away_score"])
            else:
                fav_rows.append(g["home_score"] - g["away_score"])
        if g["home_score"] > g["away_score"]:
            wins[g["home_team"]] += 1
            losses[g["away_team"]] += 1
        else:
            wins[g["away_team"]] += 1
            losses[g["home_team"]] += 1
    out["H5_home_dog_record_proxy"] = {
        "definition": ("home team whose prior-season-record win% is lower than the "
                       "visitor's (>=5 games each) vs the opposite; record proxy is "
                       "NOT a spread and is used only to test direction"),
        "home_dog_n": len(dog_rows), "home_dog_avg_margin": _mean(dog_rows),
        "home_fav_n": len(fav_rows), "home_fav_avg_margin": _mean(fav_rows),
        "welch_t": _welch(dog_rows, fav_rows),
    }

    # --------------------------------- 6. long-rest (3+) vs short-rest teams
    last_team_game = {}
    long_rest, short_rest = [], []
    for g in reg:
        for team, pts, opp_pts in ((g["home_team"], g["home_score"], g["away_score"]),
                                   (g["away_team"], g["away_score"], g["home_score"])):
            prev = last_team_game.get(team)
            if prev:
                days = (datetime.strptime(g["game_date_et"], "%Y-%m-%d")
                        - datetime.strptime(prev, "%Y-%m-%d")).days - 1
                if days >= 3:
                    long_rest.append(pts - opp_pts)
                elif days <= 1:
                    short_rest.append(pts - opp_pts)
        last_team_game[g["home_team"]] = g["game_date_et"]
        last_team_game[g["away_team"]] = g["game_date_et"]
    out["H6_long_vs_short_rest_net"] = {
        "long_rest_3plus": {"n": len(long_rest), "avg_net": _mean(long_rest)},
        "short_rest_0_1": {"n": len(short_rest), "avg_net": _mean(short_rest)},
        "welch_t": _welch(long_rest, short_rest),
    }

    # ------------------------------------ 7. season phase (playoffs) scoring
    post = [g for g in finals if not is_regular(
        g, REG_SEASON_START.get(g["season"], "9999"),
        REG_SEASON_END.get(g["season"], "0000"))]
    post_total = [g["home_score"] + g["away_score"] for g in post]
    out["H7_playoff_vs_regular_scoring"] = {
        "playoffs_or_preseason_n": len(post),
        "avg_total_outside_regular_window": _mean(post_total),
        "avg_total_regular": _mean(totals),
        "difference": (round(_mean(post_total) - _mean(totals), 3)
                       if post_total and totals else None),
    }
    out["factor_sweep"] = factor_sweep(con)
    return out


def factor_sweep(con, min_n: int = 60) -> dict:
    """Chronological sweep of candidate betting rules on VERIFIED results only.

    For each rule: how often the picked side won, compared with the
    side-weighted base rate for the sides it picked, with a two-proportion
    z-statistic. No prices are used, so a rule can only be *promising* here,
    never proven profitable — the price test comes later, forward.

    Every evaluated rule is reported, including the ones that fail, so the
    research log can show the nulls as well as the hits.
    """
    from nbacomp.backtest import margin_to_prob, team_streak
    from nbacomp.model import EloModel, RollingTeamState, expected_total

    finals = [dict(r) for r in con.execute(
        "SELECT * FROM games WHERE status='final' AND home_score IS NOT NULL "
        "AND away_score IS NOT NULL "
        "ORDER BY COALESCE(tipoff_utc, game_date_et || 'T23:59:59Z')")]
    logs: dict[str, list] = {}
    for r in con.execute("SELECT * FROM team_gamelogs ORDER BY game_date_et"):
        logs.setdefault(r["team"], []).append(dict(r))
    pos = {t: 0 for t in logs}
    rolling = RollingTeamState(window=15)
    sched = RollingTeamState(window=15)
    elo = EloModel()
    season = None

    rules: dict[str, dict] = {}

    # home-win rate over the whole sample: the side-weighted baseline for each
    # rule is built from THIS value and the side each pick actually took
    _hw = con.execute(
        "SELECT AVG(1.0*(home_score>away_score)) r FROM games WHERE status='final' "
        "AND home_score IS NOT NULL").fetchone()["r"]
    home_rate = float(_hw or 0.5)

    def note(name, side, won, margin, label=""):
        r = rules.setdefault(name, {"n": 0, "hits": 0, "margin_sum": 0.0,
                                    "examples": [], "base_sum": 0.0, "base_n": 0})
        r["n"] += 1
        r["hits"] += 1 if won else 0
        r["margin_sum"] += margin
        if side in ("home", "away"):
            r["base_sum"] += home_rate if side == "home" else 1.0 - home_rate
        else:
            # totals / over-under rules have no home/away baseline: the null is
            # a coin flip between the two sides of the model's own number
            r["base_sum"] += 0.5
        r["base_n"] += 1
        if len(r["examples"]) < 3:
            r["examples"].append(label)

    for g in finals:
        gdate = g["game_date_et"]
        if g["season"] != season:
            if season is not None:
                elo.new_season()
            season = g["season"]
        for team in (g["home_team"], g["away_team"]):
            rows = logs.get(team) or []
            i = pos.get(team, 0)
            while i < len(rows) and rows[i]["game_date_et"] < gdate:
                if rows[i].get("season") == season:
                    rolling.add_game(rows[i])
                i += 1
            pos[team] = i
        h_hist = [r for r in sched.history.get(g["home_team"], [])
                  if r.get("game_date_et") and r["game_date_et"] < gdate]
        a_hist = [r for r in sched.history.get(g["away_team"], [])
                  if r.get("game_date_et") and r["game_date_et"] < gdate]
        h_rest = sched.rest_and_travel(g["home_team"], gdate, True, season)
        a_rest = sched.rest_and_travel(g["away_team"], gdate, False, season)
        hs, as_ = g["home_score"], g["away_score"]
        home_won = hs > as_
        margin = hs - as_
        label = f"{g['away_team']}@{g['home_team']} {gdate}"

        def state_age(team):
            prior = [r["game_date_et"] for r in (logs.get(team) or [])
                     if r["game_date_et"] < gdate and r.get("season") == season]
            if not prior:
                return None
            from datetime import date as _d
            return (_d.fromisoformat(gdate) - _d.fromisoformat(max(prior))).days

        fresh = (state_age(g["home_team"]) is not None
                 and state_age(g["home_team"]) <= 14
                 and state_age(g["away_team"]) is not None
                 and state_age(g["away_team"]) <= 14)

        # ---- rule R1: away team on 2nd night of a B2B -> back the home team
        if a_rest.get("rest_days") == 0 and (h_rest.get("rest_days") or 0) >= 2:
            note("R1_away_b2b_back_home", "home", home_won, margin, label)
        # ---- rule R2: home team on 2nd night of a B2B -> back the visitor
        if h_rest.get("rest_days") == 0 and (a_rest.get("rest_days") or 0) >= 2:
            note("R2_home_b2b_back_away", "away", not home_won, -margin, label)
        # ---- R3: either side on 3-in-4 nights -> fade that side
        for side, rest, won_if_home in (("home", h_rest, True), ("away", a_rest, False)):
            if rest.get("three_in_four") and rest.get("rest_days") == 1:
                if side == "home":
                    note("R3_fade_3in4_home", "away", not home_won, -margin, label)
                else:
                    note("R3_fade_3in4_away", "home", home_won, margin, label)
        # ---- R4: favours a team coming off a >=18 point loss (blowout bounce)
        for side, prev in (("home", h_hist[-1] if h_hist else None),
                           ("away", a_hist[-1] if a_hist else None)):
            if prev and prev.get("pts") is not None and prev.get("opp_pts") is not None \
                    and prev["pts"] - prev["opp_pts"] <= -18:
                if side == "home":
                    note("R4_back_team_after_blowout_loss", "home", home_won, margin, label)
                else:
                    note("R4_back_team_after_blowout_loss", "away", not home_won, -margin, label)
        # ---- R5: fade a 4+ game win streak (zig-zag / mean reversion)
        for side, hist in (("home", h_hist), ("away", a_hist)):
            if team_streak(hist) >= 4:
                if side == "home":
                    note("R5_fade_win_streak", "away", not home_won, -margin, label)
                else:
                    note("R5_fade_win_streak", "home", home_won, margin, label)
        # ---- R6: back a 4+ game losing streak (regression to the mean)
        for side, hist in (("home", h_hist), ("away", a_hist)):
            if team_streak(hist) <= -4:
                if side == "home":
                    note("R6_back_loss_streak", "home", home_won, margin, label)
                else:
                    note("R6_back_loss_streak", "away", not home_won, -margin, label)
        # ---- R7: long road trip finale (5+ away games) -> back the home team
        if a_rest.get("road_trip_len", 0) >= 5:
            note("R7_road_trip_finale_back_home", "home", home_won, margin, label)
        # ---- R8: Elo disagreement with the home side when Elo is confident
        if fresh:
            h_roll = rolling.team_rolling(g["home_team"])
            a_roll = rolling.team_rolling(g["away_team"])
            p_home = margin_to_prob(elo.margin(g["home_team"], g["away_team"]))
            if p_home <= 0.42:
                note("R8_elo_fade_home", "away", not home_won, -margin, label)
            elif p_home >= 0.58:
                note("R8_elo_fade_home", "home", home_won, margin, label)
            # ---- R9: model total above/below the season-average total
            if h_roll and a_roll and h_roll.get("pace_available") and a_roll.get("pace_available"):
                exp = expected_total(h_roll, a_roll)
                if exp is not None:
                    actual = hs + as_
                    note("R9_model_total_extreme",
                         "over" if exp > 232 else "under",
                         (actual > exp) if exp > 232 else (actual < exp),
                         abs(actual - exp), label)
        # state updates strictly after the rule evaluation
        elo.update(g["home_team"], g["away_team"], hs, as_)
        hw = 1 if home_won else 0
        sched.add_game({"team": g["home_team"], "game_date_et": gdate, "is_home": 1,
                        "venue_team": None, "pts": hs, "opp_pts": as_,
                        "wl": "W" if hw else "L", "season": season})
        sched.add_game({"team": g["away_team"], "game_date_et": gdate, "is_home": 0,
                        "venue_team": g["home_team"], "pts": as_, "opp_pts": hs,
                        "wl": "L" if hw else "W", "season": season})

    # base rate for each rule = mean side-weighted base rate of the picks it
    # actually made (so a mixed-side rule is not measured against the home rate)
    out = {}
    for name, r in sorted(rules.items()):
        n, hits = r["n"], r["hits"]
        if n < 1:
            continue
        base = (r["base_sum"] / r["base_n"]) if r["base_n"] else None
        hr = hits / n
        z = None
        if base is not None and 0 < base < 1:
            se = (base * (1 - base) / n) ** 0.5
            z = round((hr - base) / se, 2) if se else None
        out[name] = {"n": n, "hit_rate": round(hr, 4),
                     "baseline_rate": (round(base, 4) if base is not None else None),
                     "z_vs_baseline": z, "avg_margin_for_pick": round(r["margin_sum"] / n, 3),
                     "usable_sample": bool(n >= min_n),
                     "examples": r["examples"]}
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/research_scan.json")
    args = ap.parse_args()
    with db.get_db() as con:
        res = scan(con)
    res["generated_utc"] = __import__("nbacomp.util", fromlist=["util"]).utcnow_iso()
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(res, f, indent=2)
    print(json.dumps(res, indent=2)[:6000])
    print(f"\n[wrote {args.out}]")


if __name__ == "__main__":
    main()
