"""Signal-validation backtest — OUTCOMES ONLY, NO MARKET PRICES.

Why this exists (2026-09-21, verified): no free historical NBA price series
exists (Kalshi settled markets expose neither candles nor tape — 480-ticker
probe, HTTP 200 with 0 rows; ESPN scoreboards carry no odds for past dates —
probe7; no free sportsbook line archive). A price-taking backtest is therefore
impossible, and fabricating one is forbidden.

What IS available: verified final scores, schedules and box scores for
multiple seasons. That lets us validate each strategy's DECISION RULE — does
the signal side actually win more often than its base rate? — without
inventing a single price. The results are evidence about the predictive
signal, NOT P&L, and are labeled as such on the site.

Method (no look-ahead, same discipline as the price backtest):
  - games walked chronologically; Elo + rolling state fed ONLY from strictly
    prior games (same as-of rules as nbacomp.backtest)
  - each rule evaluated with NO price input (the price-dependent parts of a
    rule — the edge threshold vs a market quote — are omitted; the signal
    side and the rule's own probability, where it has one, are recorded)
  - outcome = whether the picked side won (winner rules) or model-total error
    (total rules); baselines are league base rates / season-mean totals
  - per-season and pooled summaries; per-game rows kept for inspection
"""
from __future__ import annotations

from datetime import timedelta

from . import db, util
from .backtest import margin_to_prob, team_streak
from .model import EloModel, RollingTeamState, expected_total

RULE_LABEL = {
    "NBA-001": "away 2nd-night B2B vs 2+ day-rested home -> bet home",
    "NBA-003": "Elo stronger side",
    "NBA-006": "home rolling home-net - away rolling road-net > 4 -> bet home",
    "NBA-007": "away 5+ road trip + 2+ tz shift -> bet home",
    "NBA-020": "team lost previous game by >= 18 -> bet that team (+2pp)",
    "NBA-021": "fade market overreaction to 4+ streaks (+/-3pp)",
    "NBA-002": "rolling pace/efficiency model total",
    "NBA-022": "model total + measured rest-asymmetry effect (3+ vs <=1 days)",
    "NBA-023": "postseason total shift (measured 9.4 pt drop) -> under",
    "NBA-024": "back a team on a 4+ win streak / fade a 4+ loss streak",
}


def _split_net(hist: list[dict], team: str, is_home: bool, window: int = 15) -> float | None:
    sel = [r for r in hist if bool(r.get("is_home")) == is_home]
    if len(sel) < 8:
        return None
    net, n = 0.0, 0
    for r in sel[-window:]:
        if r.get("pts") is None or r.get("opp_pts") is None:
            continue
        net += float(r["pts"]) - float(r["opp_pts"])
        n += 1
    return net / n if n >= 8 else None


def _winner_signals(ctx: dict) -> list[dict]:
    """Price-free winner-rule signals: (strategy, side, model_prob, trigger)."""
    out = []
    h, a = ctx["h_rest"], ctx["a_rest"]
    p_home = margin_to_prob(ctx["margin"])
    p_away = 1.0 - p_home

    # NBA-001 rest edge
    if a.get("b2b") and h.get("rest_days") is not None and h["rest_days"] >= 2:
        out.append(("NBA-001", "home", p_home,
                    f"away B2B (rest {a.get('rest_days')}d), home rest {h.get('rest_days')}d"))
    # NBA-003 elo
    if ctx["games_h"] >= 5 and ctx["games_a"] >= 5:
        side = "home" if p_home >= p_away else "away"
        out.append(("NBA-003", side, p_home if side == "home" else p_away,
                    f"Elo margin {ctx['margin']:+.1f}"))
    # NBA-006 home/away splits
    nh = _split_net(ctx["hist_h"], ctx["home"], True)
    na = _split_net(ctx["hist_a"], ctx["away"], False)
    if nh is not None and na is not None and nh - na > 4.0:
        out.append(("NBA-006", "home", p_home,
                    f"home net {nh:.1f} vs away road net {na:.1f}"))
    # NBA-007 travel
    if a.get("road_trip_len", 0) >= 5 and a.get("tz_shift", 0) >= 2:
        out.append(("NBA-007", "home", p_home,
                    f"away road-trip {a['road_trip_len']} + tz {a['tz_shift']}"))
    # NBA-020 blowout bounce
    for side, prev, team in (("home", ctx.get("h_prev"), ctx["home"]),
                             ("away", ctx.get("a_prev"), ctx["away"])):
        if not prev or prev.get("pts") is None or prev.get("opp_pts") is None:
            continue
        margin = prev["pts"] - prev["opp_pts"]
        if margin <= -18:
            p_side = (p_home if side == "home" else p_away) + 0.02
            out.append(("NBA-020", side, p_side,
                        f"{team} lost previous game by {abs(margin):.0f}"))
            break
    # NBA-024 momentum (back the streak) — the measured opposite of NBA-021
    for side, streak in (("home", ctx.get("h_streak") or 0),
                         ("away", ctx.get("a_streak") or 0)):
        opp = "away" if side == "home" else "home"
        if streak >= 4:
            p_side = p_home if side == "home" else p_away
            out.append(("NBA-024", side, p_side,
                        f"{ctx[side]} on a {streak}-game win streak"))
        elif streak <= -4:
            p_opp = p_home if opp == "home" else p_away
            out.append(("NBA-024", opp, p_opp,
                        f"opponent of {ctx[side]} ({abs(streak)}-game losing streak)"))
    # NBA-021 streak skeptic
    for side, streak in (("home", ctx.get("h_streak") or 0),
                         ("away", ctx.get("a_streak") or 0)):
        if abs(streak) < 4:
            continue
        if streak > 0:
            bet_side = "away" if side == "home" else "home"
            p_bet = (p_away if bet_side == "away" else p_home) + 0.03
            label = f"fade {ctx[side]} {streak}-game WIN streak"
        else:
            bet_side = side
            p_bet = (p_home if bet_side == "home" else p_away) + 0.03
            label = f"back {ctx[side]} {abs(streak)}-game LOSS streak"
        out.append(("NBA-021", bet_side, min(0.97, p_bet), label))
        break
    return out


def _total_signals(ctx: dict) -> list[dict]:
    """Price-free total-rule signals: (strategy, model_total, trigger)."""
    out = []
    exp = ctx.get("exp_total")
    if exp is None:
        return out
    out.append(("NBA-002", exp, f"model total {exp:.1f}"))
    hr, ar = ctx["h_rest"], ctx["a_rest"]
    if hr.get("rest_days") is None or ar.get("rest_days") is None:
        return out
    rests = (float(hr["rest_days"]), float(ar["rest_days"]))
    if max(rests) >= 3.0 and min(rests) <= 1.0:
        out.append(("NBA-022", exp + 1.82,
                    f"model {exp:.1f}+1.82 (rest {rests[0]:.0f}/{rests[1]:.0f})"))
    if ctx.get("postseason"):
        out.append(("NBA-023", exp - 9.4, f"postseason shift: model {exp:.1f}-9.4"))
    return out


def run_signal_backtest(con, run_id: str = "sigbt-2026-09-21") -> dict:
    games = con.execute(
        "SELECT * FROM games WHERE status='final' AND home_score IS NOT NULL "
        "ORDER BY COALESCE(tipoff_utc, game_date_et || 'T23:59:59Z')").fetchall()
    if not games:
        db.log_collection(con, "signal-backtest", "engine", "empty",
                          "no final games in database")
        return {"games": 0, "signals": 0}

    rolling = RollingTeamState(window=15)
    sched = RollingTeamState(window=15)
    log_by_team: dict[str, list] = {}
    for r in con.execute("SELECT * FROM team_gamelogs ORDER BY game_date_et"):
        log_by_team.setdefault(r["team"], []).append(dict(r))
    log_pos = {t: 0 for t in log_by_team}

    def advance(team: str, before_date: str, season: str | None = None):
        """Same-season, strictly-prior box scores only (see backtest.advance)."""
        rows = log_by_team.get(team) or []
        i = log_pos.get(team, 0)
        while i < len(rows) and rows[i]["game_date_et"] < before_date:
            r = rows[i]
            if season is None or r.get("season") == season:
                rolling.add_game(r)
            i += 1
        log_pos[team] = i

    elo = EloModel()
    n_signals = 0
    season_totals: dict[str, list[float]] = {}
    CUR = util.utcnow_iso()

    for g in games:
        tip = util.parse_iso(g["tipoff_utc"])
        gdate = g["game_date_et"]
        decision = util.to_iso(tip - timedelta(hours=2)) if tip \
            else f"{gdate}T20:00:00Z"
        season = g["season"]
        if season and not elo.season:
            elo.season = season
        elif season and elo.season and elo.season != season:
            elo.new_season()
            elo.season = season

        advance(g["home_team"], gdate, g["season"])
        advance(g["away_team"], gdate, g["season"])
        h_hist_all = [r for r in sched.history.get(g["home_team"], [])
                      if r.get("game_date_et") and r["game_date_et"] < gdate]
        a_hist_all = [r for r in sched.history.get(g["away_team"], [])
                      if r.get("game_date_et") and r["game_date_et"] < gdate]
        h_hist = [r for r in rolling.history.get(g["home_team"], [])
                  if r.get("game_date_et") and r["game_date_et"] < gdate]
        a_hist = [r for r in rolling.history.get(g["away_team"], [])
                  if r.get("game_date_et") and r["game_date_et"] < gdate]
        # stale state is treated as unavailable, exactly as in the price
        # backtest and the forward engine (2026-09-21 fix)
        from .backtest import _roll_state
        _st = _roll_state(rolling, g["home_team"], g["away_team"], gdate)
        h_roll, a_roll = _st["h_roll"], _st["a_roll"]
        exp_total = None
        if h_roll and a_roll and h_roll.get("pace_available") and a_roll.get("pace_available"):
            exp_total = expected_total(h_roll, a_roll)
        ctx = {
            "game": g, "decision": decision, "home": g["home_team"],
            "away": g["away_team"], "season": season,
            "margin": elo.margin(g["home_team"], g["away_team"]),
            "h_rest": sched.rest_and_travel(g["home_team"], gdate, True, g["season"]),
            "a_rest": sched.rest_and_travel(g["away_team"], gdate, False, g["season"]),
            "h_prev": h_hist_all[-1] if h_hist_all else None,
            "a_prev": a_hist_all[-1] if a_hist_all else None,
            "postseason": (g["season_type"] == "postseason"
                           if "season_type" in g.keys() else False),
            "h_streak": team_streak(h_hist_all),
            "a_streak": team_streak(a_hist_all),
            "hist_h": h_hist, "hist_a": a_hist,
            "games_h": h_roll["games"] if h_roll else 0,
            "games_a": a_roll["games"] if a_roll else 0,
            "exp_total": exp_total,
        }
        actual_total = float(g["home_score"] + g["away_score"])
        season_totals.setdefault(season, []).append(actual_total)

        for sid, side, prob, trig in _winner_signals(ctx):
            winner = g["home_team"] if (g["home_score"] > g["away_score"]) else g["away_team"]
            picked = g["home_team"] if side == "home" else g["away_team"]
            outcome = 1 if picked == winner else 0
            db.insert(con, "signal_backtests", {
                "run_id": run_id, "strategy_id": sid,
                "strategy_version": _version(sid), "season": season,
                "game_id": g["game_id"], "game_date_et": gdate,
                "decision_utc": decision, "market": "winner", "selection": side,
                "model_prob": round(prob, 4), "outcome": outcome,
                "metric_value": round(float(g["home_score"]) - float(g["away_score"]), 1),
                "trigger": trig[:300]}, replace=False)
            n_signals += 1
        for sid, model_total, trig in _total_signals(ctx):
            db.insert(con, "signal_backtests", {
                "run_id": run_id, "strategy_id": sid,
                "strategy_version": _version(sid), "season": season,
                "game_id": g["game_id"], "game_date_et": gdate,
                "decision_utc": decision, "market": "total",
                "selection": "model-total", "model_prob": None, "outcome": None,
                "metric_value": round(actual_total - model_total, 1),
                "trigger": trig[:300]}, replace=False)
            n_signals += 1

        # state update strictly after signals for this game
        elo.update(g["home_team"], g["away_team"], g["home_score"], g["away_score"])
        hw = 1 if g["home_score"] > g["away_score"] else 0
        sched.add_game({"team": g["home_team"], "game_date_et": gdate, "is_home": 1,
                        "venue_team": None, "pts": g["home_score"],
                        "opp_pts": g["away_score"], "wl": "W" if hw else "L"})
        sched.add_game({"team": g["away_team"], "game_date_et": gdate, "is_home": 0,
                        "venue_team": g["home_team"], "pts": g["away_score"],
                        "opp_pts": g["home_score"], "wl": "L" if hw else "W"})
        advance(g["home_team"], _next_day(gdate), g["season"])
        advance(g["away_team"], _next_day(gdate), g["season"])

    _write_summaries(con, run_id, season_totals)
    db.log_collection(con, "signal-backtest", "engine", "ok",
                      f"{run_id}: games={len(games)} signals={n_signals} "
                      "(outcome-only validation; no prices used)", rows=n_signals)
    return {"games": len(games), "signals": n_signals}


def _version(sid: str) -> str:
    from . import strategies as S
    m = S.STRATEGIES.get(sid) or {}
    return m.get("version", "unknown")


def _next_day(d: str) -> str:
    from datetime import date, timedelta
    y, m, dd = map(int, d.split("-"))
    return (date(y, m, dd) + timedelta(days=1)).isoformat()


def _write_summaries(con, run_id: str, season_totals: dict[str, list[float]]):
    seasons = sorted({r["season"] for r in
                      con.execute("SELECT DISTINCT season FROM signal_backtests "
                                  "WHERE run_id=?", (run_id,))})
    for season in seasons + ["ALL"]:
        where = "WHERE run_id=? AND market='winner'"
        args: list = [run_id]
        if season != "ALL":
            where += " AND season=?"
            args.append(season)
        rows = con.execute(f"SELECT strategy_id, season, selection, model_prob, outcome "
                           f"FROM signal_backtests {where}", args).fetchall()
        by_sid: dict[str, list] = {}
        for r in rows:
            by_sid.setdefault(r["strategy_id"], []).append(dict(r))
        for sid, rs in by_sid.items():
            n = len(rs)
            hits = sum(r["outcome"] for r in rs)
            # pair prob with its OWN row (a None prob must not shift the rest)
            pairs = [(r["model_prob"], r["outcome"]) for r in rs
                     if r["model_prob"] is not None and r["outcome"] is not None]
            brier = (sum((p - o) ** 2 for p, o in pairs) / len(pairs)
                     if pairs else None)
            # empirical base rate of the picked sides
            sel_counts: dict[str, int] = {}
            for r in rs:
                sel_counts[r["selection"]] = sel_counts.get(r["selection"], 0) + 1
            summary_row = {
                "run_id": run_id, "strategy_id": sid,
                "strategy_version": _version(sid), "season": season,
                "n_signals": n, "n_hits": hits,
                "hit_rate": round(hits / n, 4) if n else None,
                "baseline_rate": None,  # filled below from games table
                "brier": round(brier, 4) if brier is not None else None,
                "mae_total": None, "baseline_mae_total": None,
                "avg_model_prob": (round(sum(p for p, _ in pairs) / len(pairs), 4)
                                   if pairs else None),
                "max_streak_hits": _max_streak(rs, True),
                "max_streak_misses": _max_streak(rs, False),
                "generated_utc": util.utcnow_iso(),
            }
            # empirical base rate: win rate of 'home' in this window
            s2 = " AND season=?" if season != "ALL" else ""
            qargs = [] if season == "ALL" else [season]
            home_wins = con.execute(
                f"SELECT COUNT(*) c, SUM(home_score>away_score) w FROM games "
                f"WHERE status='final' AND home_score IS NOT NULL{s2}", qargs).fetchone()
            home_rate = (home_wins["w"] or 0) / home_wins["c"] if home_wins["c"] else None
            if home_rate is not None:
                # side-weighted base rate across the selections actually made
                wsum, wcount = 0.0, 0
                for s, c in sel_counts.items():
                    sr = home_rate if s == "home" else 1.0 - home_rate
                    wsum += sr * c
                    wcount += c
                summary_row["baseline_rate"] = round(wsum / wcount, 4) if wcount else None
            db.insert(con, "signal_backtest_summary", summary_row, replace=True)
        # totals MAE per strategy
        twhere = "WHERE run_id=? AND market='total'"
        targs: list = [run_id]
        if season != "ALL":
            twhere += " AND season=?"
            targs.append(season)
        trows = con.execute(f"SELECT strategy_id, metric_value FROM signal_backtests "
                            f"{twhere}", targs).fetchall()
        tby: dict[str, list] = {}
        for r in trows:
            tby.setdefault(r["strategy_id"], []).append(abs(r["metric_value"]))
        if season == "ALL":
            # pooled window: flatten every season's observed totals
            pooled = [t for ts in season_totals.values() for t in ts]
        else:
            pooled = season_totals.get(season) or []
        baseline_mae = (sum(abs(t - sum(pooled) / len(pooled)) for t in pooled) / len(pooled)
                        if pooled else None)
        for sid, errs in tby.items():
            existing = con.execute(
                "SELECT 1 FROM signal_backtest_summary WHERE run_id=? AND strategy_id=? "
                "AND season=?", (run_id, sid, season)).fetchone()
            mae = round(sum(errs) / len(errs), 2)
            bmae = round(baseline_mae, 2) if baseline_mae is not None else None
            if existing:
                con.execute(
                    "UPDATE signal_backtest_summary SET mae_total=?, "
                    "baseline_mae_total=? WHERE run_id=? AND strategy_id=? AND season=?",
                    (mae, bmae, run_id, sid, season))
            else:
                con.execute(
                    "INSERT INTO signal_backtest_summary (run_id, strategy_id, "
                    "strategy_version, season, n_signals, n_hits, hit_rate, "
                    "baseline_rate, brier, mae_total, baseline_mae_total, "
                    "avg_model_prob, max_streak_hits, max_streak_misses, "
                    "generated_utc) VALUES (?,?,?,?,0,0,NULL,NULL,NULL,?,?,NULL,"
                    "NULL,NULL,?)",
                    (run_id, sid, _version(sid), season, mae, bmae,
                     util.utcnow_iso()))


def _max_streak(rows: list[dict], want_hit: bool) -> int:
    best = cur = 0
    for r in rows:
        hit = bool(r["outcome"]) == want_hit
        cur = cur + 1 if hit else 0
        best = max(best, cur)
    return best
