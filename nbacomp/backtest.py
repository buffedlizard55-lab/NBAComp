"""Backtest engine — chronological, price-as-of, Kalshi-fee aware.

Reconstructs decisions ONLY from information available at decision time:
  - model state (Elo, rolling stats) built from strictly prior games
  - prices from hourly Kalshi candlesticks strictly before decision (+1 tick slippage)
  - injuries published strictly before decision
Settlement uses Kalshi's own recorded result when present (ground truth),
otherwise the verified final score; mismatches raise anomalies.

The backtest never uses ESPN odds snapshots (those only exist forward from
collection start) — backtest and forward universes stay honestly separated.
"""
from __future__ import annotations

from datetime import timedelta

from . import db, engine, strategies as S, util
from .model import SD_TOTAL, EloModel, RollingTeamState, expected_total

ELO_PER_POINT = 28.0  # Elo points per expected margin point (standard conversion)


def margin_to_prob(margin: float) -> float:
    """P(home wins) from expected margin via logistic Elo equivalent."""
    return 1.0 / (1.0 + 10.0 ** (-(margin * ELO_PER_POINT) / 400.0))


def run_backtest(con, seasons: list[str], run_id: str, start_iso: str | None = None,
                 end_iso: str | None = None, strategy_ids: list[str] | None = None) -> dict:
    games = con.execute(
        f"SELECT * FROM games WHERE season IN ({','.join('?' * len(seasons))}) "
        "AND status='final' AND home_score IS NOT NULL "
        "ORDER BY COALESCE(tipoff_utc, game_date_et || 'T23:59:59Z')", seasons).fetchall()
    if start_iso:
        games = [g for g in games if (g["tipoff_utc"] or g["game_date_et"]) >= start_iso]
    if end_iso:
        games = [g for g in games if (g["tipoff_utc"] or g["game_date_et"]) <= end_iso]
    if not games:
        db.log_collection(con, "backtest", "engine", "empty", "no games in window")
        return {"bets": 0, "games": 0}

    mapping = engine.map_kalshi_markets(con)
    book = engine.PriceBook(con)
    by_game: dict[str, dict[str, engine.KalshiMarketInfo]] = {}
    for info in mapping.values():
        if info.market_type in ("winner", "spread", "total"):
            by_game.setdefault(info.game_id, {})[info.market_type] = info

    elo = EloModel()
    rolling = RollingTeamState(window=15)
    log_by_team: dict[str, list] = {}
    for r in con.execute("SELECT * FROM team_gamelogs ORDER BY game_date_et"):
        log_by_team.setdefault(r["team"], []).append(dict(r))
    log_pos: dict[str, int] = {t: 0 for t in log_by_team}

    def advance(team: str, before_date: str):
        rows = log_by_team.get(team) or []
        i = log_pos.get(team, 0)
        while i < len(rows) and rows[i]["game_date_et"] < before_date:
            rolling.add_game(rows[i])
            i += 1
        log_pos[team] = i

    bankroll: dict[str, float] = {}
    n_bets = 0
    CUR = util.utcnow_iso()

    for g in games:
        tip = util.parse_iso(g["tipoff_utc"])
        gdate = g["game_date_et"]
        decision = util.to_iso(tip - timedelta(hours=2)) if tip else None
        if not decision:
            continue

        advance(g["home_team"], gdate)
        advance(g["away_team"], gdate)

        winner = by_game.get(g["game_id"], {}).get("winner")
        ctx = {
            "game": g, "decision": decision, "elo": elo,
            "home": g["home_team"], "away": g["away_team"],
            "h_roll": rolling.team_rolling(g["home_team"]),
            "a_roll": rolling.team_rolling(g["away_team"]),
            "h_rest": rolling.rest_and_travel(g["home_team"], gdate, True),
            "a_rest": rolling.rest_and_travel(g["away_team"], gdate, False),
            "winner": winner, "total": by_game.get(g["game_id"], {}).get("total"),
            "book": book, "rolling_history": rolling.history,
            "total_line": espn_total_line(con, g["game_id"], decision),
            "inj_adj": {g["home_team"]: 0.0, g["away_team"]: 0.0},
            "inj_flag": {g["home_team"]: None, g["away_team"]: None},
        }
        _injury_adjustment(con, ctx, decision)

        if winner is not None:
            price_h = book.kalshi_price_at(winner.ticker, decision)
            signals: list[S.Signal] = []
            if price_h:
                for sid in (strategy_ids or list(EVALUATORS.keys())):
                    fn = EVALUATORS.get(sid)
                    if not fn:
                        continue
                    try:
                        signals.extend(fn(ctx) or [])
                    except Exception as e:
                        db.log_anomaly(con, "warn", "strategy-eval-error",
                                       {"strategy": sid, "err": str(e)[:200], "game": g["game_id"]})
            for sig in signals:
                n_bets += _place_kalshi_bet(con, ctx, sig, winner, price_h, run_id, bankroll, CUR)

        # update state WITH this game only after its bets are placed
        elo.update(g["home_team"], g["away_team"], g["home_score"], g["away_score"])
        advance(g["home_team"], _next_day(gdate))
        advance(g["away_team"], _next_day(gdate))

    db.log_collection(con, "backtest", "engine", "ok", f"{run_id}: bets={n_bets}", rows=n_bets)
    return {"bets": n_bets, "games": len(games)}


def _next_day(d: str) -> str:
    from datetime import date, timedelta
    y, m, dd = map(int, d.split("-"))
    return (date(y, m, dd) + timedelta(days=1)).isoformat()


def _place_kalshi_bet(con, ctx, sig: S.Signal, winner: engine.KalshiMarketInfo,
                      price_h: engine.PricePoint, run_id: str, bankroll: dict, CUR: str) -> int:
    meta = S.STRATEGIES[sig.strategy_id]
    br = bankroll.setdefault(sig.strategy_id, S.STARTING_BANKROLL)
    prob = sig.model_prob
    if not 0.0 < prob < 1.0:
        return 0
    if sig.market == "kalshi:winner":
        tick = price_h if sig.selection == "home" else engine.PricePoint(
            price_h.ts_utc, 100.0 - price_h.price_cents, "no_side_derived")
        mkt_prob = tick.price_cents / 100.0
        stake = S.stake_for(br, prob, util.prob_to_fair_decimal(prob))
        if stake <= 0:
            return 0
        contracts, cost = engine.simulate_fill_kalshi(stake, tick.price_cents)
        if contracts <= 0:
            return 0
        result = engine.apply_settlement_kalshi(con, winner, {"selection": sig.selection},
                                                ctx["game"]["home_score"], ctx["game"]["away_score"])
        pnl = engine.kalshi_bet_pnl(contracts, tick.price_cents, result) if result in ("win", "loss") else 0.0
        fee = util.kalshi_fees_dollars(contracts, tick.price_cents)
        bet_id = engine.make_bet_id("backtest", sig.strategy_id, ctx["game"]["game_id"],
                                    sig.market, sig.selection, ctx["decision"], run_id)
        db.insert(con, "bets", {
            "bet_id": bet_id, "run_id": run_id, "kind": "backtest",
            "strategy_id": sig.strategy_id, "strategy_version": meta["version"],
            "username": meta["username"], "decision_utc": ctx["decision"],
            "game_id": ctx["game"]["game_id"],
            "game_label": f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
            "tipoff_utc": ctx["game"]["tipoff_utc"], "market": "kalshi:winner",
            "selection": sig.selection, "side": sig.side,
            "price": tick.price_cents, "price_format": "kalshi_cents",
            "source": "kalshi:candlesticks", "source_url": engine.KALSHI_CANDLE_URL,
            "source_ts": tick.ts_utc, "model_prob": prob, "market_prob": mkt_prob,
            "edge": prob - mkt_prob, "stake_usd": cost, "to_win_usd": round(contracts - cost, 2),
            "execution_status": "simulated_fill", "fill_price": tick.price_cents,
            "fee_usd": fee, "contracts": contracts,
            "result": result if result in ("win", "loss") else "pending",
            "settlement_utc": None if result == "pending" else CUR,
            "settlement_source": "kalshi result cross-checked vs verified score",
            "pnl_usd": round(pnl, 2), "roi": round(pnl / cost, 4) if cost else 0,
            "verification": ("price: last closed hourly candle before decision +1 tick "
                             "(historical book depth unobservable — documented)"),
            "notes": sig.trigger[:500]}, replace=True)
        if result in ("win", "loss"):
            bankroll[sig.strategy_id] = br + pnl
        return 1
    # line-based totals bets (model vs observed total line): sportsbook-style at
    # fair odds -110 (documented assumption; no free historical total prices)
    if sig.market == "total":
        stake = S.stake_for(br, sig.model_prob, 1.909)  # -110 decimal
        if stake <= 0:
            return 0
        hs, as_ = ctx["game"]["home_score"], ctx["game"]["away_score"]
        result = engine.settle_score_based("total", hs, as_, sig.selection, _line_of(sig))
        pnl = engine.american_pnl(stake, -110, result)
        bet_id = engine.make_bet_id("backtest", sig.strategy_id, ctx["game"]["game_id"],
                                    sig.market, sig.selection, ctx["decision"], run_id)
        db.insert(con, "bets", {
            "bet_id": bet_id, "run_id": run_id, "kind": "backtest",
            "strategy_id": sig.strategy_id, "strategy_version": meta["version"],
            "username": meta["username"], "decision_utc": ctx["decision"],
            "game_id": ctx["game"]["game_id"],
            "game_label": f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
            "tipoff_utc": ctx["game"]["tipoff_utc"], "market": "total",
            "selection": sig.selection, "side": sig.side,
            "price": -110, "price_format": "american",
            "source": "line: kalshi strike or espn snapshot",
            "source_url": "https://api.elections.kalshi.com/trade-api/v2/markets",
            "source_ts": ctx["decision"], "model_prob": sig.model_prob,
            "market_prob": 0.524, "edge": sig.model_prob - 0.524,
            "stake_usd": stake, "to_win_usd": round(stake * 0.909, 2),
            "execution_status": "simulated_fill", "fill_price": -110, "fee_usd": 0,
            "result": result, "settlement_utc": CUR,
            "settlement_source": "verified final score",
            "pnl_usd": round(pnl, 2), "roi": round(pnl / stake, 4),
            "verification": ("PRICED-ASSUMPTION: no free historical totals price source; "
                             "simulated at standard -110. Labeled, not hidden."),
            "notes": sig.trigger[:500]}, replace=True)
        bankroll[sig.strategy_id] = br + pnl
        return 1
    return 0


def _line_of(sig) -> float | None:
    try:
        return float(sig.selection.split()[-1])
    except (ValueError, IndexError):
        return None


def espn_total_line(con, game_id, decision) -> float | None:
    row = con.execute(
        "SELECT line FROM odds_snapshots WHERE game_id=? AND market='total' AND captured_utc<=? "
        "ORDER BY captured_utc DESC LIMIT 1", (game_id, decision)).fetchone()
    return row["line"] if row else None


def _injury_adjustment(con, ctx, decision):
    g = ctx["game"]
    for team in (ctx["home"], ctx["away"]):
        inj = con.execute(
            "SELECT player FROM injuries WHERE team=? AND status IN ('Out','Doubtful','Out For Season') "
            "AND COALESCE(published_utc, captured_utc) <= ?", (team, decision)).fetchall()
        if not inj:
            continue
        top = _recent_minutes(con, team, g["game_date_et"])
        if not top:
            continue
        hurt = {i["player"] for i in inj}
        adj, flagged = 0.0, []
        for player, _mins in top[:2]:
            if player in hurt:
                adj += 2.5
                flagged.append(player)
        if adj:
            ctx["inj_adj"][team] = adj
            ctx["inj_flag"][team] = flagged


def _recent_minutes(con, team, before_date):
    from collections import defaultdict
    rows = con.execute(
        "SELECT player, minutes FROM player_gamelogs WHERE team=? AND game_date_et<? "
        "AND minutes IS NOT NULL ORDER BY game_date_et DESC LIMIT 500",
        (team, before_date)).fetchall()
    per_player = defaultdict(list)
    for r in rows:
        per_player[r["player"]].append(float(r["minutes"] or 0))
    if not per_player:
        return None
    avg = [(p, sum(v[:5]) / min(len(v), 5)) for p, v in per_player.items()]
    return sorted(avg, key=lambda kv: -kv[1])


# ------------------------------------------------------------- evaluators

def _winner_probs(ctx):
    elo = ctx["elo"]
    margin = elo.margin(ctx["home"], ctx["away"])
    margin -= ctx["inj_adj"].get(ctx["home"], 0.0)
    margin += ctx["inj_adj"].get(ctx["away"], 0.0)
    p_home = margin_to_prob(margin)
    return p_home, 1.0 - p_home


def _kalshi_side_prices(ctx, price_h):
    p_home = price_h.price_cents / 100.0
    return p_home, 1.0 - p_home


def eval_rest(ctx):
    h, a = ctx["h_rest"], ctx["a_rest"]
    if not h or not a or not (a.get("b2b") and h.get("rest_days", 0) >= 2):
        return []
    price = ctx["book"].kalshi_price_at(ctx["winner"].ticker, ctx["decision"])
    if not price:
        return []
    p_home, _ = _winner_probs(ctx)
    mkt_home = price.price_cents / 100.0
    if p_home - mkt_home >= 0.04:
        sig = _sig(ctx, "NBA-001", "home", p_home, mkt_home, price,
                   f"away 2nd-night B2B (rest={a.get('rest_days')}), home rest={h.get('rest_days')}")
        return [sig]
    return []


def eval_elo(ctx):
    price = ctx["book"].kalshi_price_at(ctx["winner"].ticker, ctx["decision"])
    if not price:
        return []
    p_home, p_away = _winner_probs(ctx)
    mkt_home, mkt_away = _kalshi_side_prices(ctx, price)
    for side, prob, mkt in (("home", p_home, mkt_home), ("away", p_away, mkt_away)):
        if prob - mkt >= 0.04:
            sig = _sig(ctx, "NBA-003", side, prob, mkt, price,
                       f"Elo margin {ctx['elo'].margin(ctx['home'], ctx['away']):.1f} pts")
            return [sig]
    return []


def eval_linemove(ctx):
    tip = util.parse_iso(ctx["game"]["tipoff_utc"])
    base_target = util.to_iso(tip - timedelta(hours=48))
    base = ctx["book"].kalshi_price_around(ctx["winner"].ticker, base_target, window_hours=12)
    now = ctx["book"].kalshi_price_at(ctx["winner"].ticker, ctx["decision"])
    if not base or not now:
        return []
    move = now.price_cents - base.price_cents
    if abs(move) < 8:
        return []
    side = "home" if move > 0 else "away"
    mkt_home, mkt_away = _kalshi_side_prices(ctx, now)
    prob = mkt_home if side == "home" else mkt_away
    sig = _sig(ctx, "NBA-004", side, prob, prob, now,
               f"follow move {base.price_cents:.0f}c->" + f"{now.price_cents:.0f}c (T-48h->T-2h)")
    return [sig]


def eval_injury(ctx):
    flags = ctx["inj_flag"]
    if not (flags.get(ctx["home"]) or flags.get(ctx["away"])):
        return []
    price = ctx["book"].kalshi_price_at(ctx["winner"].ticker, ctx["decision"])
    if not price:
        return []
    p_home, p_away = _winner_probs(ctx)
    mkt_home, mkt_away = _kalshi_side_prices(ctx, price)
    out = []
    for side, prob, mkt in (("home", p_home, mkt_home), ("away", p_away, mkt_away)):
        team = ctx["home"] if side == "home" else ctx["away"]
        if flags.get(team) and prob - mkt >= 0.04:
            sig = _sig(ctx, "NBA-005", side, prob, mkt, price,
                       "opponent missing " + ",".join(flags[team]) + " (Out/Doubtful, -2.5pt adj each)")
            out.append(sig)
    return out


def eval_homecourt(ctx):
    h, a = ctx["h_roll"], ctx["a_roll"]
    if not h or not a or h.get("games", 0) < 8 or a.get("games", 0) < 8:
        return []
    nh = _split_net(ctx, ctx["home"], True)
    na = _split_net(ctx, ctx["away"], False)
    if nh is None or na is None or nh - na <= 4.0:
        return []
    price = ctx["book"].kalshi_price_at(ctx["winner"].ticker, ctx["decision"])
    if not price:
        return []
    p_home, _ = _winner_probs(ctx)
    mkt_home = price.price_cents / 100.0
    if p_home - mkt_home >= 0.04:
        sig = _sig(ctx, "NBA-006", "home", p_home, mkt_home, price,
                   f"home net {nh:.1f} vs away road net {na:.1f} ({nh - na:+.1f})")
        return [sig]
    return []


def _split_net(ctx, team, is_home):
    hist = ctx["rolling_history"].get(team) or []
    sel = [r for r in hist if bool(r.get("is_home")) == is_home]
    if len(sel) < 8:
        return None
    net, n = 0.0, 0
    for r in sel[-15:]:
        if r.get("pts") is None or r.get("opp_pts") is None:
            continue
        net += float(r["pts"]) - float(r["opp_pts"])
        n += 1
    return net / n if n >= 8 else None


def eval_travel(ctx):
    a = ctx["a_rest"]
    if not a or a.get("road_trip_len", 0) < 5 or a.get("tz_shift", 0) < 2:
        return []
    price = ctx["book"].kalshi_price_at(ctx["winner"].ticker, ctx["decision"])
    if not price:
        return []
    p_home, _ = _winner_probs(ctx)
    mkt_home = price.price_cents / 100.0
    if p_home - mkt_home >= 0.04:
        sig = _sig(ctx, "NBA-007", "home", p_home, mkt_home, price,
                   f"road-trip finale ({a['road_trip_len']}) + tz shift {a['tz_shift']}")
        return [sig]
    return []


def _total_setup(ctx):
    if not ctx["h_roll"] or not ctx["a_roll"]:
        return None
    exp = expected_total(ctx["h_roll"], ctx["a_roll"])
    line = ctx.get("total_line") or (_strike_of(ctx["total"]) if ctx.get("total") else None)
    if exp is None or line is None:
        return None
    return exp, float(line)


def eval_pace_total(ctx):
    setup = _total_setup(ctx)
    if not setup:
        return []
    exp, line = setup
    diff = exp - line
    if abs(diff) < 8:
        return []
    side = "over" if diff > 0 else "under"
    p_side = util.norm_cdf(diff / SD_TOTAL) if side == "over" else 1.0 - util.norm_cdf(diff / SD_TOTAL)
    if p_side - 0.524 < 0.02:  # must beat -110 vig breakeven + margin
        return []
    sig = S.Signal(
        strategy_id="NBA-002", game_id=ctx["game"]["game_id"], market="total",
        selection=f"{side} {line:g}", side=side, price=-110, price_format="american",
        source="kalshi:strike|espn:line", model_prob=p_side, market_prob=0.524,
        trigger=f"model total {exp:.1f} vs line {line:g} ({diff:+.1f})",
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"])
    return [sig]


def eval_regime(ctx):
    setup = _total_setup(ctx)
    if not setup:
        return []
    exp, line = setup
    out = []
    for team, roll in ((ctx["home"], ctx["h_roll"]), (ctx["away"], ctx["a_roll"])):
        if not roll.get("fg3a") or roll["fg3a"] < 35 or not roll.get("fg3pct"):
            continue
        seas = _season_3p(ctx, team)
        if not seas:
            continue
        delta = roll["fg3pct"] - seas
        if abs(delta) <= 0.035:
            continue
        side = "under" if delta > 0 else "over"
        p_over = util.norm_cdf((exp - line) / SD_TOTAL)
        p_side = p_over if side == "over" else 1.0 - p_over
        if p_side - 0.524 >= 0.02:
            out.append(S.Signal(
                strategy_id="NBA-010", game_id=ctx["game"]["game_id"], market="total",
                selection=f"{side} {line:g}", side=side, price=-110, price_format="american",
                source="kalshi:strike|espn:line", model_prob=p_side, market_prob=0.524,
                trigger=f"{team} rolling 3P% {roll['fg3pct']:.3f} vs season {seas:.3f} ({delta:+.3f})",
                game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
                tipoff_utc=ctx["game"]["tipoff_utc"]))
    return out


def eval_oreb_total(ctx):
    setup = _total_setup(ctx)
    if not setup:
        return []
    exp, line = setup

    def oreb_rate(roll):
        if not roll.get("fga"):
            return None
        return (roll.get("oreb") or 0) / roll["fga"]

    ro, ao = oreb_rate(ctx["h_roll"]), oreb_rate(ctx["a_roll"])
    if ro is None or ao is None:
        return []
    thresh = 6.0 if (ro + ao) >= 0.31 else 8.0
    diff = exp - line
    if abs(diff) < thresh:
        return []
    side = "over" if diff > 0 else "under"
    p_over = util.norm_cdf(diff / SD_TOTAL)
    p_side = p_over if side == "over" else 1.0 - p_over
    if p_side - 0.524 < 0.02:
        return []
    return [S.Signal(
        strategy_id="NBA-011", game_id=ctx["game"]["game_id"], market="total",
        selection=f"{side} {line:g}", side=side, price=-110, price_format="american",
        source="kalshi:strike|espn:line", model_prob=p_side, market_prob=0.524,
        trigger=f"OREB rates {ro:.3f}/{ao:.3f}; model {exp:.1f} vs {line:g}",
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"])]


def _season_3p(ctx, team):
    sel = [r for r in (ctx["rolling_history"].get(team) or [])
           if r.get("game_date_et") < ctx["game"]["game_date_et"]]
    m = sum(float(r.get("fg3m") or 0) for r in sel)
    a = sum(float(r.get("fg3a") or 0) for r in sel)
    return (m / a) if a else None


def _sig(ctx, sid, side, prob, mkt, price, trigger) -> S.Signal:
    team = ctx[side]
    return S.Signal(
        strategy_id=sid, game_id=ctx["game"]["game_id"], market="kalshi:winner",
        selection=team, side=side,
        price=price.price_cents if side == "home" else 100.0 - price.price_cents,
        price_format="kalshi_cents", source="kalshi:candlesticks",
        model_prob=prob, market_prob=mkt,
        trigger=trigger,
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"], source_ts=price.ts_utc)


def _strike_of(info):
    return engine._strike(info, None) if info else None


EVALUATORS = {
    "NBA-001": eval_rest,
    "NBA-002": eval_pace_total,
    "NBA-003": eval_elo,
    "NBA-004": eval_linemove,
    "NBA-005": eval_injury,
    "NBA-006": eval_homecourt,
    "NBA-007": eval_travel,
    "NBA-010": eval_regime,
    "NBA-011": eval_oreb_total,
}
