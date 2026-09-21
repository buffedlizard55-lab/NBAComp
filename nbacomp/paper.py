"""Forward paper-trading engine — the live competition.

Generates bets from CURRENT captured snapshots (ESPN odds, Kalshi orderbooks,
Kalshi series for totals / props), sizes them against persistent per-strategy
bankrolls, marks open positions, and settles finished games from verified
results or Kalshi's own result field.

Everything is timestamped; the decision state (prices + injury board) is
frozen in the bet row at creation and never rewritten.

This engine reuses the SAME strategy evaluators as the backtester so the rule
traded in the competition is exactly the rule tested historically.
"""
from __future__ import annotations

import json
from datetime import timedelta

from . import db, engine, strategies as S, util
from .backtest import (ELO_PER_POINT, EVALUATORS, _winner_probs, _total_setup,
                       _season_3p, _split_net, margin_to_prob, team_streak)
from .model import SD_TOTAL, EloModel, RollingTeamState, expected_total


def _bankroll(con, strategy_id: str) -> float:
    row = con.execute(
        "SELECT current FROM bankroll_events WHERE strategy_id=? "
        "ORDER BY as_of_utc DESC, id DESC LIMIT 1",
        (strategy_id,)).fetchone()
    return float(row["current"]) if row else S.STARTING_BANKROLL


def _record_bankroll(con, strategy_id: str, current: float, reason: str):
    settled_open = con.execute(
        "SELECT COALESCE(SUM(stake_usd),0) s FROM bets WHERE strategy_id=? "
        "AND kind='forward' AND result='pending' AND execution_status='simulated_fill'",
        (strategy_id,)).fetchone()["s"]
    starting = S.STARTING_BANKROLL
    db.insert(con, "bankroll_events", {
        "strategy_id": strategy_id, "as_of_utc": util.utcnow_iso(),
        "starting": starting, "current": round(current, 2),
        "available": round(current - settled_open, 2),
        "exposure": round(settled_open, 2), "reason": reason}, replace=True)


# --------------------------------------------------------- generic helpers

def _kalshi_cents_price_for_side(price: engine.PricePoint, side: str) -> float:
    """Price in cents on the indicated (home/away) side of a winner market.

    Kalshi's YES contract is always the home team (we map selection->side that
    way at placement); "away" bets are the NO side priced 100-home_price.
    """
    return price.price_cents if side == "home" else 100.0 - price.price_cents


def _available_to_stake(con, strategy_id: str, br: float) -> float:
    """Compute bankroll less open exposure, clamped to [0, br].

    `br - open_exp` can go negative when exposure exceeds bankroll (the
    position-cap check above this function prevents fresh bets in that state,
    but defensive clamp avoids passing a negative sizing parameter downstream).
    """
    open_exp = con.execute(
        "SELECT COALESCE(SUM(stake_usd),0) s FROM bets WHERE strategy_id=? "
        "AND kind='forward' AND result='pending'",
        (strategy_id,)).fetchone()["s"]
    return max(0.0, br - open_exp)


def _sig_winner(ctx, sid, side, prob, mkt, price, trigger) -> S.Signal:
    team = ctx[side]
    return S.Signal(
        strategy_id=sid, game_id=ctx["game"]["game_id"], market="kalshi:winner",
        selection=team, side=side,
        price=_kalshi_cents_price_for_side(price, side),
        price_format="kalshi_cents", source="kalshi:orderbook",
        model_prob=prob, market_prob=mkt, trigger=trigger,
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"], source_ts=price.ts_utc)


def _sig_total(ctx, sid, side, line, prob, mkt, source, trigger) -> S.Signal:
    """Total markets: priced at -110 (documented assumption)."""
    return S.Signal(
        strategy_id=sid, game_id=ctx["game"]["game_id"],
        market="total", selection=f"{side} {line:g}", side=side,
        price=-110, price_format="american",
        source=source, model_prob=prob, market_prob=mkt, trigger=trigger,
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"])


def _sig_prop_kalshi(ctx, sid, info: engine.KalshiMarketInfo, side: str, prob,
                    mkt, target_price: float, trigger: str,
                    strike: float | None = None,
                    player: str | None = None) -> S.Signal:
    """Player-prop market on a Kalshi prop series (KXNBAREBS / KXNBAASTS / ...).

    strike + market identity + player are frozen into the bet row at
    decision time: settlement later needs exactly the line and the market
    that were observed (never a later re-observation).
    """
    return S.Signal(
        strategy_id=sid, game_id=ctx["game"]["game_id"],
        market=f"kalshi:{info.market_type}",  # e.g. kalshi:prop:rebounds
        selection=info.subtitle or info.ticker, side=side,
        price=target_price, price_format="kalshi_cents",
        source="kalshi:orderbook", model_prob=prob, market_prob=mkt,
        trigger=trigger, game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"],
        strike=strike, market_ticker=info.ticker, prop_player=player)


# --------------------------------------------------------- main loop

def generate_forward_bets(con) -> int:
    now = util.utcnow_iso()
    mapping = engine.map_kalshi_markets(con)
    book = engine.PriceBook(con)
    by_game: dict[str, dict[str, engine.KalshiMarketInfo]] = {}
    for info in mapping.values():
        # store_winner files the HOME team's market as "winner" (live shape
        # is one market per team) and keeps the other side as "winner_away".
        engine.store_winner(by_game.setdefault(info.game_id, {}), info)
    # also keep all prop markets mapped to their game + player
    props_by_game: dict[str, list[engine.KalshiMarketInfo]] = {}
    for info in mapping.values():
        if info.market_type.startswith("prop:"):
            props_by_game.setdefault(info.game_id, []).append(info)

    upcoming = con.execute(
        "SELECT * FROM games WHERE status IN ('scheduled','in') AND tipoff_utc IS NOT NULL "
        "AND tipoff_utc > ? ORDER BY tipoff_utc", (now,)).fetchall()

    elo = _build_elo(con)
    rolling = RollingTeamState(window=15)
    # Schedule state for rest/B2B/road-trip/travel AND streak/bounce rules
    # (all finals are strictly before any upcoming game, so a single upfront
    # feed is safe). Scores/WL feed NBA-020/021.
    sched = RollingTeamState(window=15)
    for gf in con.execute(
            "SELECT home_team, away_team, game_date_et, home_score, away_score "
            "FROM games WHERE status='final' AND home_score IS NOT NULL "
            "ORDER BY game_date_et"):
        hw = 1 if gf["home_score"] > gf["away_score"] else 0
        sched.add_game({"team": gf["home_team"], "game_date_et": gf["game_date_et"],
                        "is_home": 1, "venue_team": None,
                        "pts": gf["home_score"], "opp_pts": gf["away_score"],
                        "wl": "W" if hw else "L"})
        sched.add_game({"team": gf["away_team"], "game_date_et": gf["game_date_et"],
                        "is_home": 0, "venue_team": gf["home_team"],
                        "pts": gf["away_score"], "opp_pts": gf["home_score"],
                        "wl": "L" if hw else "W"})
    log_by_team: dict[str, list] = {}
    for r in con.execute("SELECT * FROM team_gamelogs ORDER BY game_date_et"):
        log_by_team.setdefault(r["team"], []).append(dict(r))
    log_pos = {t: 0 for t in log_by_team}

    def advance(team, before):
        rows = log_by_team.get(team) or []
        i = log_pos.get(team, 0)
        while i < len(rows) and rows[i]["game_date_et"] < before:
            rolling.add_game(rows[i])
            i += 1
        log_pos[team] = i

    n = 0
    for g in upcoming:
        tip = util.parse_iso(g["tipoff_utc"])
        if tip is None:
            continue
        decision = now
        if (tip - util.parse_iso(now)) < timedelta(hours=1):
            continue  # execution latency guard
        gdate = g["game_date_et"]
        advance(g["home_team"], gdate)
        advance(g["away_team"], gdate)
        game_markets = by_game.get(g["game_id"], {})
        winner = game_markets.get("winner")
        h_hist = [r for r in sched.history.get(g["home_team"], [])
                  if r.get("game_date_et") and r["game_date_et"] < gdate]
        a_hist = [r for r in sched.history.get(g["away_team"], [])
                  if r.get("game_date_et") and r["game_date_et"] < gdate]
        ctx = {
            "game": g, "decision": decision, "elo": elo,
            "home": g["home_team"], "away": g["away_team"],
            "h_roll": rolling.team_rolling(g["home_team"]),
            "a_roll": rolling.team_rolling(g["away_team"]),
            "h_rest": sched.rest_and_travel(g["home_team"], gdate, True),
            "a_rest": sched.rest_and_travel(g["away_team"], gdate, False),
            "h_prev": h_hist[-1] if h_hist else None,
            "a_prev": a_hist[-1] if a_hist else None,
            "h_streak": team_streak(h_hist),
            "a_streak": team_streak(a_hist),
            "winner": winner, "total": game_markets.get("total"),
            "half": game_markets.get("1h"),
            "spread": game_markets.get("spread"),
            "book": book, "rolling_history": rolling.history,
            "total_line": _latest_line(con, g["game_id"]),
            "spread_line": _latest_spread(con, g["game_id"]),
            "inj_adj": {g["home_team"]: 0.0, g["away_team"]: 0.0},
            "inj_flag": {g["home_team"]: None, g["away_team"]: None},
        }
        _injury_state(con, ctx, decision)

        # ---- winner-market signals (NBA-001/003/004/005/006/007/012/020/021) ----
        # Winner-market bets need a mapped winner market with a live ask
        # (the price source); totals and props do NOT (they use their own
        # line/market), so they are evaluated below unconditionally.
        if winner is not None:
            live = book.kalshi_live_ask(winner.ticker)
            if live:
                # Shared evaluators price from ctx["live_price"] in forward
                # (candles don't exist forward); the line-move baseline comes
                # from our own timestamped orderbook history.
                ctx["live_price"] = live
                ctx["live_baseline"] = book.kalshi_ask_at(
                    winner.ticker, util.to_iso(tip - timedelta(hours=48)))
                for sig in _winner_signals(con, ctx, live):
                    n += _place_forward_bet(con, ctx, sig, winner, live)

        # ---- 1H-market signals (NBA-013) ----
        n += _half_hour_signals(con, ctx)

        # ---- totals-market signals (NBA-002/010/011/022) ----
        for sig in _total_signals(con, ctx):
            n += _place_total_bet(con, ctx, sig, ctx.get("total"))

        # ---- prop-market signals (NBA-008/009/014) ----
        for sig in _prop_signals(con, ctx, props_by_game.get(g["game_id"], [])):
            n += _place_prop_bet(con, ctx, sig)
    return n


def _winner_signals(con, ctx, live: engine.PricePoint) -> list[S.Signal]:
    """All winner-market strategies share the same evaluator set as backtest."""
    home, away = ctx["home"], ctx["away"]
    p_home, p_away = _winner_probs(ctx)
    mkt_home, mkt_away = (live.price_cents / 100.0, 1.0 - live.price_cents / 100.0)
    sigs: list[S.Signal] = []

    # Run the same evaluators used by the backtest so rules are symmetric.
    # Evaluators return winner-market Signals only.
    for sid in ("NBA-001", "NBA-003", "NBA-004", "NBA-005", "NBA-006",
                "NBA-007", "NBA-020", "NBA-021"):
        try:
            fn = EVALUATORS.get(sid)
            if not fn:
                continue
            for sig in fn(ctx) or []:
                # Use the LIVE observed ask as the price source for forward bets
                # (backtest uses candle close+1tick); everything else identical.
                # NBA-004 (v1.1.0): directional rule — model_prob IS the
                # market probability (no independent probability estimate),
                # which is what the fixed 1% sizing path expects.
                if sig.strategy_id == "NBA-004":
                    mkt_home_p = live.price_cents / 100.0
                    side_p = mkt_home_p if sig.side == "home" else 1.0 - mkt_home_p
                    model_prob = side_p
                else:
                    model_prob = sig.model_prob
                sig = S.Signal(
                    strategy_id=sig.strategy_id,
                    game_id=sig.game_id, market=sig.market,
                    selection=sig.selection, side=sig.side,
                    price=_kalshi_cents_price_for_side(live, sig.side or "home"),
                    price_format="kalshi_cents",
                    source="kalshi:orderbook",
                    model_prob=model_prob, market_prob=sig.market_prob,
                    trigger=sig.trigger, game_label=sig.game_label,
                    tipoff_utc=sig.tipoff_utc, source_ts=live.ts_utc)
                sigs.append(sig)
        except Exception as e:
            db.log_anomaly(con, "warn", "paper-eval-error",
                           {"strategy": sid, "err": str(e)[:200],
                            "game": ctx["game"]["game_id"]})

    # NBA-012 cross-market divergence (devigged ESPN ML vs Kalshi)
    ml = espn_ml_probs(con, ctx["game"]["game_id"], ctx["decision"])
    if ml:
        dev_h, dev_a = ml
        for side, kprob in (("home", mkt_home), ("away", mkt_away)):
            sp = dev_h if side == "home" else dev_a
            if kprob is not None and abs(sp - kprob) >= 0.04 and kprob < sp:
                trigger = f"ESPN devig {sp:.3f} vs Kalshi {kprob:.3f}"
                sigs.append(_sig_winner(ctx, "NBA-012", side, sp, kprob, live, trigger))

    # De-duplicate so a strategy never produces multiple winner signals per game
    seen = set()
    out: list[S.Signal] = []
    for sig in sigs:
        key = (sig.strategy_id, sig.selection, sig.side)
        if key in seen:
            continue
        seen.add(key)
        out.append(sig)
    return out


def _total_signals(con, ctx) -> list[S.Signal]:
    """Totals strategies (NBA-002 / NBA-010 / NBA-011).

    Lines come from ESPN snapshots when present; falls back to Kalshi strike
    if a KXNBATOTAL strike value is stored. If neither is available we don't
    emit totals signals (forward-first).
    """
    sigs: list[S.Signal] = []
    line = ctx.get("total_line")
    info = ctx.get("total")
    if line is None and info and info.strike:
        try:
            line = float(info.strike.get("value") or info.strike.get("strike")
                          or info.strike.get("over_under"))
        except (TypeError, ValueError):
            line = None
    if line is None:
        return sigs
    ctx_local = dict(ctx)
    ctx_local["total_line"] = line
    setup = _total_setup(ctx_local)
    if not setup:
        return sigs
    exp, lineval = setup

    # NBA-002: pace/efficiency total
    diff = exp - lineval
    if abs(diff) >= 8:
        side = "over" if diff > 0 else "under"
        p_side = util.norm_cdf(diff / SD_TOTAL) if side == "over" else 1.0 - util.norm_cdf(diff / SD_TOTAL)
        if p_side - 0.524 >= 0.02:
            sigs.append(_sig_total(
                ctx_local, "NBA-002", side, lineval, p_side, 0.524,
                source="espn:line|kalshi:strike",
                trigger=f"model total {exp:.1f} vs line {lineval:g} ({diff:+.1f})"))

    # NBA-010: 3P% regression
    for team, roll in ((ctx_local["home"], ctx_local["h_roll"]),
                       (ctx_local["away"], ctx_local["a_roll"])):
        if not roll or not roll.get("fg3a") or roll["fg3a"] < 35 or not roll.get("fg3pct"):
            continue
        seas = _season_3p(ctx_local, team)
        if seas is None:
            continue
        delta = roll["fg3pct"] - seas
        if abs(delta) <= 0.035:
            continue
        side = "under" if delta > 0 else "over"
        p_over = util.norm_cdf((exp - lineval) / SD_TOTAL)
        p_side = p_over if side == "over" else 1.0 - p_over
        if p_side - 0.524 >= 0.02:
            sigs.append(_sig_total(
                ctx_local, "NBA-010", side, lineval, p_side, 0.524,
                source="espn:line|kalshi:strike",
                trigger=f"{team} rolling 3P% {roll['fg3pct']:.3f} vs season {seas:.3f} ({delta:+.3f})"))

    # NBA-011: combined OREB matchup + pace
    def _oreb_rate(roll):
        if not roll or not roll.get("fga"):
            return None
        return (roll.get("oreb") or 0) / roll["fga"]
    ro = _oreb_rate(ctx_local["h_roll"]); ao = _oreb_rate(ctx_local["a_roll"])
    if ro is not None and ao is not None:
        thresh = 6.0 if (ro + ao) >= 0.31 else 8.0
        if abs(exp - lineval) >= thresh:
            side = "over" if exp > lineval else "under"
            p_over = util.norm_cdf((exp - lineval) / SD_TOTAL)
            p_side = p_over if side == "over" else 1.0 - p_over
            if p_side - 0.524 >= 0.02:
                sigs.append(_sig_total(
                    ctx_local, "NBA-011", side, lineval, p_side, 0.524,
                    source="espn:line|kalshi:strike",
                    trigger=f"OREB rates {ro:.3f}/{ao:.3f}; model {exp:.1f} vs {lineval:g}"))

    # NBA-022: rest asymmetry (3+ rest days vs 1 day) raises the total +4
    hr, ar = ctx_local.get("h_rest"), ctx_local.get("a_rest")
    if hr and ar:
        rests = (float(hr.get("rest_days", 3.0)), float(ar.get("rest_days", 3.0)))
        if max(rests) >= 3.0 and min(rests) <= 1.0:
            diff = (exp + 4.0) - lineval
            if diff >= 6.0:
                p_side = util.norm_cdf(diff / SD_TOTAL)
                if p_side - 0.524 >= 0.02:
                    sigs.append(_sig_total(
                        ctx_local, "NBA-022", "over", lineval, p_side, 0.524,
                        source="espn:line|kalshi:strike",
                        trigger=(f"rest asymmetry {rests[0]:.0f}/{rests[1]:.0f} "
                                 f"days; model {exp:.1f}+4 vs {lineval:g} ({diff:+.1f})")))

    # One total bet per strategy per game: a strategy emitting both over and
    # under in the same game (e.g. NBA-010 with hot home AND cold away) would
    # otherwise hedge itself. Keep the stronger-edge signal per strategy.
    return _dedup_per_strategy(sigs)


def _dedup_per_strategy(sigs: list[S.Signal]) -> list[S.Signal]:
    """Keep at most one signal per (strategy, market, game) — the
    stronger-edge one — so no strategy can hedge itself in the same game."""
    best: dict[str, S.Signal] = {}
    for s in sigs:
        key = f"{s.strategy_id}|{s.market}|{s.game_id}"
        cur = best.get(key)
        if cur is None or (s.model_prob - s.market_prob) > (cur.model_prob - cur.market_prob):
            best[key] = s
    return list(best.values())


HALF_MARGIN_SD = 8.0  # prior SD of NBA half-margin (documented assumption)


def _half_hour_signals(con, ctx) -> int:
    """NBA-013: KXNBA1H 'team leads at half' market vs Elo half-probability.

    P(home leads at half) = normal_cdf(elo_margin / HALF_MARGIN_SD). The
    market is per-team (same convention as KXNBAGAME: the ticker suffix /
    title names the team YES pays on), so YES-ask = P(that team leads at
    half). Buys the side with >= 4pp model-vs-market gap. Settlement uses
    the Kalshi recorded result, else captured in-game quarter scores, else
    void (stake returned) — see settle_finished.
    """
    info = ctx.get("half")
    if info is None or not info.team:
        return 0
    book = ctx["book"]
    pp = book.kalshi_live_ask(info.ticker)
    if pp is None:
        return 0
    margin = ctx["elo"].margin(ctx["home"], ctx["away"])
    p_home_half = util.norm_cdf(margin / HALF_MARGIN_SD)
    team = info.team
    p_team_half = p_home_half if team == ctx["home"] else 1.0 - p_home_half
    mkt_team = pp.price_cents / 100.0
    if p_team_half - mkt_team >= 0.04:
        side, price, mkt, prob = "yes", pp.price_cents, mkt_team, p_team_half
        trig = f"{team} leads at half: model {p_team_half:.3f} vs ask {mkt_team:.3f}"
    elif mkt_team - p_team_half >= 0.04:
        side, price = "no", 100.0 - pp.price_cents
        mkt, prob = 1.0 - mkt_team, 1.0 - p_team_half
        trig = (f"{team} NOT lead at half: model {prob:.3f} vs "
                f"NO-price {mkt:.3f}")
    else:
        return 0
    sig = S.Signal(
        strategy_id="NBA-013", game_id=ctx["game"]["game_id"], market="kalshi:1h",
        selection=f"{team} leads at half", side=side, price=price,
        price_format="kalshi_cents", source="kalshi:orderbook",
        model_prob=prob, market_prob=mkt, trigger=trig,
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"])
    return _place_1h_bet(con, ctx, sig, info, pp)


def _place_1h_bet(con, ctx, sig: S.Signal, info: engine.KalshiMarketInfo,
                  live: engine.PricePoint) -> int:
    meta = S.STRATEGIES[sig.strategy_id]
    br = _bankroll(con, sig.strategy_id)
    open_exp = con.execute(
        "SELECT COALESCE(SUM(stake_usd),0) s FROM bets WHERE strategy_id=? AND kind='forward' "
        "AND result='pending'", (sig.strategy_id,)).fetchone()["s"]
    if open_exp >= br * 0.25:
        return 0
    stake = S.stake_for(_available_to_stake(con, sig.strategy_id, br),
                        sig.model_prob, 100.0 / sig.price)
    if stake <= 0:
        return 0
    contracts, cost = engine.simulate_fill_kalshi(stake, sig.price)
    if contracts <= 0:
        return 0
    dup = con.execute(
        "SELECT 1 FROM bets WHERE strategy_id=? AND game_id=? AND market=? AND selection=? "
        "AND kind='forward' LIMIT 1",
        (sig.strategy_id, ctx["game"]["game_id"], sig.market, sig.selection)).fetchone()
    if dup:
        return 0
    bet_id = engine.make_bet_id("forward", sig.strategy_id, ctx["game"]["game_id"],
                                sig.market, sig.selection, ctx["decision"], "forward")
    if con.execute("SELECT 1 FROM bets WHERE bet_id=?", (bet_id,)).fetchone():
        return 0
    ev = (sig.model_prob * (contracts - cost)) - ((1 - sig.model_prob) * cost)
    db.insert(con, "bets", {
        "bet_id": bet_id, "run_id": "forward", "kind": "forward",
        "strategy_id": sig.strategy_id, "strategy_version": meta["version"],
        "username": meta["username"], "decision_utc": ctx["decision"],
        "game_id": ctx["game"]["game_id"], "game_label": sig.game_label,
        "tipoff_utc": ctx["game"]["tipoff_utc"], "market": sig.market,
        "selection": sig.selection, "side": sig.side,
        "price": sig.price, "price_format": sig.price_format,
        "source": "kalshi:orderbook",
        "source_url": "https://api.elections.kalshi.com/trade-api/v2/markets/orderbooks",
        "source_ts": live.ts_utc, "model_prob": sig.model_prob,
        "market_prob": sig.market_prob, "edge": sig.model_prob - sig.market_prob,
        "stake_usd": cost, "to_win_usd": round(contracts - cost, 2),
        "ev_usd": round(ev, 2),
        "execution_status": "simulated_fill", "fill_price": sig.price,
        "fee_usd": util.kalshi_fees_dollars(contracts, sig.price),
        "contracts": contracts, "strike": None,
        "market_ticker": info.ticker, "prop_player": None,
        "result": "pending",
        "verification": ("live Kalshi orderbook ask for the 1H market "
                         f"({info.ticker}) at decision; half-margin SD prior "
                         f"{HALF_MARGIN_SD} documented"),
        "notes": sig.trigger[:500]})
    _record_bankroll(con, sig.strategy_id, br, f"bet-placed-1h:{bet_id}")
    db.log_audit(con, "paper-engine", "bet-created", bet_id,
                 {"strategy": sig.strategy_id, "market": sig.market,
                  "selection": sig.selection, "side": sig.side})
    return 1


def _prop_signals(con, ctx, props: list[engine.KalshiMarketInfo]) -> list[S.Signal]:
    """Player-prop strategies (NBA-008/009/014) — forward-test-first.

    Each discovered prop series is evaluated against rolling player statistics.
    """
    if not props:
        return []
    sigs: list[S.Signal] = []
    book = ctx["book"]
    # build rolling player form over all player_gamelogs strictly before the game
    logs_by_player: dict[tuple[str, str], list] = {}
    player_names: set[str] = set()
    for r in con.execute(
            "SELECT player, team, minutes, reb, ast, pts, game_date_et FROM player_gamelogs "
            "WHERE game_date_et<?", (ctx["game"]["game_date_et"],)):
        logs_by_player.setdefault((r["team"], r["player"]), []).append(dict(r))
        player_names.add(r["player"])
    cands = sorted(player_names, key=len, reverse=True)
    for info in props:
        # pickup the YES-side ask (it's the strike-and-over contract)
        kp = book.kalshi_live_ask(info.ticker)
        if kp is None:
            continue
        strike = None
        try:
            for k in ("above", "below", "value", "strike", "strike_value"):
                if k in info.strike and info.strike[k] is not None:
                    strike = float(info.strike[k]); break
        except (TypeError, ValueError):
            strike = None
        if strike is None:
            continue
        # Player identification comes from the subtitle (e.g. "Tatum REB Over 7.5")
        player, stat_name = _parse_prop_subtitle(info, cands)
        if not player or not stat_name:
            continue
        # Rolling 5 stats for that player
        rows = sorted(logs_by_player.get((ctx["home"], player),
                    logs_by_player.get((ctx["away"], player), [])), key=lambda r: r["game_date_et"])
        recent5 = rows[-5:]
        if len(recent5) < 3:
            continue
        avg_pts = sum((r.get("pts") or 0) for r in recent5) / len(recent5)
        avg_reb = sum((r.get("reb") or 0) for r in recent5) / len(recent5)
        avg_ast = sum((r.get("ast") or 0) for r in recent5) / len(recent5)
        avg_mins = sum((r.get("minutes") or 0) for r in recent5) / len(recent5)
        if avg_mins < 20:
            continue
        over_under_target = {
            "reb": (avg_reb, 1.5, "NBA-008"),
            "ast": (avg_ast, 1.2, "NBA-009"),
            "pts": (avg_pts, 1.5, "NBA-014"),
        }.get(stat_name)
        if over_under_target is None:
            continue
        avg_stat, gap, sid = over_under_target
        elo_margin = ctx["elo"].margin(ctx["home"], ctx["away"])
        blowout = abs(elo_margin) >= 13 and sid == "NBA-014"  # only PTO under in blowouts
        diff = avg_stat - strike
        if abs(diff) < gap:
            continue
        side = "over" if diff > 0 else "under"
        # blowout pto: bias toward under
        if blowout and side == "over":
            continue
        # crude prob model: diff / SD scaled to 0.5..0.95; SD heuristic
        sd_stat = {"reb": 1.8, "ast": 1.4, "pts": 5.5}.get(stat_name, 2.0)
        raw = abs(diff) / sd_stat
        prob = util.norm_cdf(raw)  # P(avg consistent with this side)
        if side == "under":
            prob = 1.0 - prob
        prob = max(0.51, min(0.92, prob))  # cap to realistic band
        mkt_price = kp.price_cents / 100.0
        if side == "over":
            mkt = mkt_price
        else:
            mkt = 1.0 - mkt_price
        if prob - mkt < 0.04:
            continue
        trigger = (f"{player} rolling5 {stat_name}={avg_stat:.1f} vs line {strike:g} "
                   f"({sid}: gap={diff:+.1f}, blowout={'yes' if blowout else 'no'})")
        sigs.append(_sig_prop_kalshi(ctx, sid, info, side, prob, mkt,
                                     kp.price_cents, trigger,
                                     strike=strike, player=player))
    return sigs


def _parse_prop_subtitle(info: engine.KalshiMarketInfo,
                          cands: list[str]) -> tuple[str | None, str | None]:
    """Best-effort recovery of (player name, stat name) from a Kalshi subtitle.

    Returns ("Tatum", "reb") etc. Unparseable -> (None, None); never guesses.
    """
    text = (info.subtitle or info.title or "").lower()
    stat = None
    for needle, name in (("reb", "reb"), ("ast", "ast"), ("assist", "ast"),
                         ("points", "pts"), ("pts", "pts"), ("3pt", "3pm"),
                         ("3-point", "3pm"), ("three", "3pm")):
        if needle in text:
            stat = name; break
    if not stat:
        return None, None
    # Player name token: anywhere in subtitle/title that looks like a player
    for c in cands:
        if c.lower() in text or text in c.lower():
            return c, stat
    return None, None


# --------------------------------------------------------- bet placement

def _place_forward_bet(con, ctx, sig: S.Signal, winner,
                       live: engine.PricePoint) -> int:
    """Place a winner-market forward bet on Kalshi with paid-odds Kelly sizing."""
    meta = S.STRATEGIES[sig.strategy_id]
    br = _bankroll(con, sig.strategy_id)
    open_exp = con.execute(
        "SELECT COALESCE(SUM(stake_usd),0) s FROM bets WHERE strategy_id=? AND kind='forward' "
        "AND result='pending'", (sig.strategy_id,)).fetchone()["s"]
    if open_exp >= br * 0.25:
        return 0
    prob = sig.model_prob
    price_cents = sig.price
    if sig.strategy_id == "NBA-004":
        # v1.1.0: directional move-follow — Kelly with model_prob = market
        # prob is identically zero; fixed 1% stake instead.
        stake = round(br * S.LINE_MOVE_STAKE_PCT, 2)
        if stake < S.MIN_STAKE:
            return 0
        prob = price_cents / 100.0 if sig.side == "home" else 1.0 - price_cents / 100.0
    else:
        stake = S.stake_for(_available_to_stake(con, sig.strategy_id, br),
                            prob, 100.0 / price_cents)
        if stake <= 0:
            return 0
    contracts, cost = engine.simulate_fill_kalshi(stake, price_cents)
    if contracts <= 0:
        return 0
    dup = con.execute(
        "SELECT 1 FROM bets WHERE strategy_id=? AND game_id=? AND market=? AND selection=? "
        "AND kind='forward' LIMIT 1",
        (sig.strategy_id, ctx["game"]["game_id"], sig.market, sig.selection)).fetchone()
    if dup:
        return 0
    bet_id = engine.make_bet_id("forward", sig.strategy_id, ctx["game"]["game_id"],
                                sig.market, sig.selection, ctx["decision"], "forward")
    if con.execute("SELECT 1 FROM bets WHERE bet_id=?", (bet_id,)).fetchone():
        return 0
    ev = (prob * (contracts - cost)) - ((1 - prob) * cost)
    directional = sig.strategy_id == "NBA-004"
    db.insert(con, "bets", {
        "bet_id": bet_id, "run_id": "forward", "kind": "forward",
        "strategy_id": sig.strategy_id, "strategy_version": meta["version"],
        "username": meta["username"], "decision_utc": ctx["decision"],
        "game_id": ctx["game"]["game_id"], "game_label": sig.game_label,
        "tipoff_utc": ctx["game"]["tipoff_utc"], "market": sig.market,
        "selection": sig.selection, "side": sig.side,
        "price": price_cents, "price_format": "kalshi_cents",
        "source": "kalshi:orderbook",
        "source_url": "https://api.elections.kalshi.com/trade-api/v2/markets/orderbooks",
        "source_ts": live.ts_utc, "model_prob": prob,
        "market_prob": price_cents / 100.0, "edge": prob - price_cents / 100.0,
        "stake_usd": cost, "to_win_usd": round(contracts - cost, 2),
        "ev_usd": round(ev, 2),
        "execution_status": "simulated_fill", "fill_price": price_cents,
        "fee_usd": util.kalshi_fees_dollars(contracts, price_cents),
        "contracts": contracts, "result": "pending",
        "verification": ("price: " + ("live observed Kalshi orderbook ask (home YES side)"
                                       if sig.side == "home" else
                                       "NO side DERIVED as 100 minus the observed home-market ask")
                         + " at decision time"),
        "notes": (("[directional move-follow; model_prob = market prob, fixed 1% "
                   "stake] " if directional else "") + sig.trigger[:450])})
    _record_bankroll(con, sig.strategy_id, br, f"bet-placed:{bet_id}")
    db.log_audit(con, "paper-engine", "bet-created", bet_id, {
        "strategy": sig.strategy_id, "price_cents": price_cents,
        "market": sig.market, "selection": sig.selection})
    return 1


def _place_total_bet(con, ctx, sig: S.Signal, info) -> int:
    """Place a totals bet priced at -110 (standard sportsbook assumption).

    No free historical total prices exist; the -110 pricing is documented in
    methodology and the bet row is labeled `PRICED-ASSUMPTION`.
    """
    meta = S.STRATEGIES[sig.strategy_id]
    br = _bankroll(con, sig.strategy_id)
    open_exp = con.execute(
        "SELECT COALESCE(SUM(stake_usd),0) s FROM bets WHERE strategy_id=? AND kind='forward' "
        "AND result='pending'", (sig.strategy_id,)).fetchone()["s"]
    if open_exp >= br * 0.25:
        return 0
    stake = S.stake_for(_available_to_stake(con, sig.strategy_id, br),
                        sig.model_prob, 1.909)  # -110
    if stake <= 0:
        return 0
    dup = con.execute(
        "SELECT 1 FROM bets WHERE strategy_id=? AND game_id=? AND market=? AND selection=? "
        "AND kind='forward' LIMIT 1",
        (sig.strategy_id, ctx["game"]["game_id"], sig.market, sig.selection)).fetchone()
    if dup:
        return 0
    bet_id = engine.make_bet_id("forward", sig.strategy_id, ctx["game"]["game_id"],
                                sig.market, sig.selection, ctx["decision"], "forward")
    if con.execute("SELECT 1 FROM bets WHERE bet_id=?", (bet_id,)).fetchone():
        return 0
    db.insert(con, "bets", {
        "bet_id": bet_id, "run_id": "forward", "kind": "forward",
        "strategy_id": sig.strategy_id, "strategy_version": meta["version"],
        "username": meta["username"], "decision_utc": ctx["decision"],
        "game_id": ctx["game"]["game_id"], "game_label": sig.game_label,
        "tipoff_utc": ctx["game"]["tipoff_utc"], "market": sig.market,
        "selection": sig.selection, "side": sig.side,
        "price": -110, "price_format": "american",
        "source": "line: espn snapshot or kalshi strike",
        "source_url": ("https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
                        if sig.source and "espn" in sig.source else
                        "https://api.elections.kalshi.com/trade-api/v2/markets"),
        "source_ts": ctx["decision"], "model_prob": sig.model_prob,
        "market_prob": 0.524, "edge": sig.model_prob - 0.524,
        "stake_usd": stake, "to_win_usd": round(stake * 0.909, 2),
        "ev_usd": round((sig.model_prob * stake * 0.909) - ((1.0 - sig.model_prob) * stake), 2),
        "execution_status": "simulated_fill", "fill_price": -110, "fee_usd": 0,
        "contracts": 0, "result": "pending",
        "verification": ("PRICED-ASSUMPTION: no free historical totals price source; "
                         "simulated at standard -110. Labeled, not hidden."),
        "notes": sig.trigger[:500]})
    _record_bankroll(con, sig.strategy_id, br, f"bet-placed-total:{bet_id}")
    db.log_audit(con, "paper-engine", "bet-created", bet_id, {
        "strategy": sig.strategy_id, "market": sig.market, "selection": sig.selection})
    return 1


def _place_prop_bet(con, ctx, sig: S.Signal) -> int:
    """Place a Kalshi player-prop bet (KXNBAREBS / KXNBAASTS / KXNBAPTS)."""
    meta = S.STRATEGIES[sig.strategy_id]
    br = _bankroll(con, sig.strategy_id)
    open_exp = con.execute(
        "SELECT COALESCE(SUM(stake_usd),0) s FROM bets WHERE strategy_id=? AND kind='forward' "
        "AND result='pending'", (sig.strategy_id,)).fetchone()["s"]
    if open_exp >= br * 0.25:
        return 0
    price_cents = sig.price
    stake = S.stake_for(_available_to_stake(con, sig.strategy_id, br),
                        sig.model_prob, 100.0 / price_cents)
    if stake <= 0:
        return 0
    contracts, cost = engine.simulate_fill_kalshi(stake, price_cents)
    if contracts <= 0:
        return 0
    dup = con.execute(
        "SELECT 1 FROM bets WHERE strategy_id=? AND game_id=? AND market=? AND selection=? "
        "AND kind='forward' LIMIT 1",
        (sig.strategy_id, ctx["game"]["game_id"], sig.market, sig.selection)).fetchone()
    if dup:
        return 0
    bet_id = engine.make_bet_id("forward", sig.strategy_id, ctx["game"]["game_id"],
                                sig.market, sig.selection, ctx["decision"], "forward")
    if con.execute("SELECT 1 FROM bets WHERE bet_id=?", (bet_id,)).fetchone():
        return 0
    ev = (sig.model_prob * (contracts - cost)) - ((1.0 - sig.model_prob) * cost)
    db.insert(con, "bets", {
        "bet_id": bet_id, "run_id": "forward", "kind": "forward",
        "strategy_id": sig.strategy_id, "strategy_version": meta["version"],
        "username": meta["username"], "decision_utc": ctx["decision"],
        "game_id": ctx["game"]["game_id"], "game_label": sig.game_label,
        "tipoff_utc": ctx["game"]["tipoff_utc"], "market": sig.market,
        "selection": sig.selection, "side": sig.side,
        "price": price_cents, "price_format": "kalshi_cents",
        "source": "kalshi:orderbook",
        "source_url": "https://api.elections.kalshi.com/trade-api/v2/markets/orderbooks",
        "source_ts": ctx["decision"], "model_prob": sig.model_prob,
        "market_prob": price_cents / 100.0,
        "edge": sig.model_prob - price_cents / 100.0,
        "stake_usd": cost, "to_win_usd": round(contracts - cost, 2),
        "ev_usd": round(ev, 2),
        "execution_status": "simulated_fill", "fill_price": price_cents,
        "fee_usd": util.kalshi_fees_dollars(contracts, price_cents),
        "contracts": contracts, "strike": sig.strike,
        "market_ticker": sig.market_ticker, "prop_player": sig.prop_player,
        "result": "pending",
        "verification": ("live Kalshi orderbook ask for player-prop contract at decision; "
                         "label unverified for player name → prop mapping until first settled result"),
        "notes": sig.trigger[:500]})
    _record_bankroll(con, sig.strategy_id, br, f"bet-placed-prop:{bet_id}")
    db.log_audit(con, "paper-engine", "bet-created", bet_id, {
        "strategy": sig.strategy_id, "market": sig.market, "selection": sig.selection})
    return 1


# --------------------------------------------------------- settlement

def mark_open_positions(con):
    """Open positions don't update bet rows (immutable); marks are surfaced
    via current-bankroll computations on the Positions page."""
    return 0


def settle_finished(con) -> int:
    """Settle pending forward bets whose game is final.

    Settlement sources, in priority order (all OBSERVED data only):
      total         verified final score vs the line frozen at decision
      kalshi:winner Kalshi's recorded result (cross-checked vs the score)
      kalshi:1h     Kalshi's recorded result for the 1H market, else the
                    captured in-game Q1+Q2 scores; when neither exists the
                    bet is VOID (stake returned) 48h after the final — a
                    settlement-data gap is never resolved by guessing
      kalshi:prop:* verified box score (player_gamelogs) vs the strike frozen
                    at decision; cross-checked vs any captured Kalshi result;
                    void (stake returned) when no box score exists 48h on
    Also captures the last observed price (closing) at settlement time when
    available, for the per-spec requirement to surface closing-line context.
    """
    mapping = engine.map_kalshi_markets(con)
    by_game: dict[str, engine.KalshiMarketInfo] = {}
    for info in mapping.values():
        if info.market_type == "winner":
            cur = by_game.get(info.game_id)
            if cur is None or (info.team and info.home and info.team == info.home
                               and cur.team != cur.home):
                by_game[info.game_id] = info
    markets_by_ticker = {m["ticker"]: m for m in
                         con.execute("SELECT * FROM kalshi_markets")}
    pending = con.execute(
        "SELECT * FROM bets WHERE kind='forward' AND result='pending'").fetchall()
    book = engine.PriceBook(con)
    n = 0
    CUR = util.utcnow_iso()
    now = util.parse_iso(CUR)
    for b in pending:
        # bets keep their original game_id (append-only); follow aliases to
        # the canonical row when a merge re-keyed the game.
        gid = engine.resolve_game_id(con, b["game_id"])
        g = con.execute("SELECT * FROM games WHERE game_id=?", (gid,)).fetchone()
        if not g or g["status"] != "final" or g["home_score"] is None:
            continue
        tip = util.parse_iso(b["tipoff_utc"] or g.get("tipoff_utc"))
        stale = tip is not None and (now - tip) > timedelta(hours=48)
        result, src = _settle_one(con, b, by_game, markets_by_ticker, g, stale)
        if result not in ("win", "loss", "push", "void"):
            continue  # not resolvable yet (box score / quarter data pending)
        if b["market"] == "total":
            pnl = engine.american_pnl(b["stake_usd"], -110, result)
        elif b["market"].startswith(("kalshi:winner", "kalshi:1h", "kalshi:prop")):
            pnl = (0.0 if result in ("push", "void")
                   else engine.kalshi_bet_pnl(b["contracts"] or 0,
                                              b["fill_price"] or 50, result))
        else:
            continue
        closing = None
        try:
            info = by_game.get(gid)
            if info:
                pp = book.kalshi_price_at(info.ticker, util.utcnow_iso())
                if pp and pp.ts_utc:
                    closing = pp.price_cents
        except Exception:
            closing = None
        update_args = [result, CUR, src, round(pnl, 2),
                       round(pnl / b["stake_usd"], 4) if b["stake_usd"] else 0]
        update_sql = ("UPDATE bets SET result=?, settlement_utc=?, "
                      "settlement_source=?, pnl_usd=?, roi=?")
        if closing is not None:
            update_sql += ", closing_price=?"
            update_args.append(closing)
        update_sql += " WHERE bet_id=?"
        update_args.append(b["bet_id"])
        con.execute(update_sql, update_args)
        db.log_audit(con, "paper-engine", "bet-settled", b["bet_id"], {
            "result": result, "pnl": round(pnl, 2),
            "market": b["market"], "closing_price": closing, "source": src})
        _record_bankroll(con, b["strategy_id"],
                         _bankroll(con, b["strategy_id"]) + pnl,
                         f"settle:{b['bet_id']}")
        n += 1
    return n


_PROP_STAT_KEY = {
    "kalshi:prop:points": "pts", "kalshi:prop:rebounds": "reb",
    "kalshi:prop:assists": "ast", "kalshi:prop:steals": "stl",
    "kalshi:prop:blocks": "blk",
}


def _settle_one(con, b: dict, by_game: dict, markets_by_ticker: dict, g: dict,
                stale: bool) -> tuple[str, str]:
    """Return (result, source) for one pending forward bet.

    result is 'pending' while the needed observed data has not landed yet
    (the caller skips the bet); 'void' is a terminal NO-P&L outcome used
    ONLY when settlement data is unattainable (stale=True) — never a guess.
    """
    if b["market"] == "total":
        line = _strike_from_selection(b["selection"])
        r = engine.settle_score_based("total", g["home_score"], g["away_score"],
                                      b["selection"], line)
        return r, "verified final score vs decision-time line"
    if b["market"] == "kalshi:winner":
        info = by_game.get(g["game_id"])
        if info:
            r = engine.apply_settlement_kalshi(con, info, {"side": b["side"]},
                                                g["home_score"], g["away_score"])
            return r, ("kalshi result cross-checked vs verified score"
                       if r in ("win", "loss") else "verified final score")
        r = engine.settle_score_based("winner", g["home_score"], g["away_score"],
                                      b["side"], None)
        return r, "verified final score (no stored kalshi market)"
    if b["market"] == "kalshi:1h":
        return _settle_1h(con, b, markets_by_ticker, g, stale)
    if b["market"].startswith("kalshi:prop:"):
        return _settle_prop(con, b, markets_by_ticker, g, stale)
    return "pending", "no settlement path for market"


def _settle_1h(con, b: dict, markets_by_ticker: dict, g: dict, stale: bool) -> tuple[str, str]:
    """1H settlement: Kalshi result > captured Q1+Q2 scores > void (stale)."""
    ticker = b["market_ticker"]
    mrow = markets_by_ticker.get(ticker) if ticker else None
    if mrow and mrow["result"] in ("yes", "no"):
        # the market's YES pays on the team named in the bet selection
        want = "yes" if b["side"] == "yes" else "no"
        r = "win" if mrow["result"] == want else "loss"
        return r, f"kalshi result for {ticker}"
    q2 = con.execute(
        "SELECT home_score, away_score FROM quarter_scores WHERE game_id=? AND quarter=2",
        (g["game_id"],)).fetchone()
    if q2:
        hs, as_ = q2["home_score"], q2["away_score"]
        leader = "home" if hs > as_ else ("away" if as_ > hs else None)
        team = (g["home_team"] if leader == "home"
                else g["away_team"] if leader == "away" else None)
        if leader is None:
            return "push", "captured in-game Q1+Q2 scores: tied half (no Kalshi " \
                           "result captured; push is the honest resolution)"
        sel_team = b["selection"].split()[0]
        won = (team == sel_team) if b["side"] == "yes" else (team != sel_team)
        return ("win" if won else "loss"), "captured in-game Q1+Q2 scores (espn)"
    if stale:
        db.log_anomaly(con, "warn", "unresolvable-1h-settlement",
                       {"bet": b["bet_id"], "market": b["market_ticker"],
                        "detail": "no kalshi result and no captured quarter "
                                  "scores; bet voided, stake returned, no P&L"})
        return "void", ("void: no 1H settlement data observable from free sources "
                        "(stake returned; documented)")
    return "pending", "waiting for quarter scores / kalshi result"


def _settle_prop(con, b: dict, markets_by_ticker: dict, g: dict, stale: bool) -> tuple[str, str]:
    """Prop settlement from the verified box score vs the frozen strike."""
    stat_key = _PROP_STAT_KEY.get(b["market"])
    if stat_key is None:
        if stale:
            return "void", "void: no observable settlement path for this prop " \
                           "stat (stake returned; documented)"
        return "pending", "no settlement path for this prop stat"
    player = b["prop_player"]
    strike = b["strike"]
    if not player or strike is None:
        if stale:
            db.log_anomaly(con, "warn", "prop-bet-missing-decision-state",
                           {"bet": b["bet_id"], "player": player, "strike": strike})
            return "void", "void: decision state incomplete (stake returned)"
        return "pending", "decision state incomplete"
    row = con.execute(
        "SELECT pts, reb, ast, stl, blk, source FROM player_gamelogs "
        "WHERE game_id=? AND player=? LIMIT 1", (g["game_id"], player)).fetchone()
    if not row or row[stat_key] is None:
        if stale:
            db.log_anomaly(con, "warn", "prop-unresolvable",
                           {"bet": b["bet_id"], "player": player,
                            "detail": "no box-score row 48h after final; "
                                      "bet voided, stake returned, no P&L"})
            return "void", "void: no verified box score for the player " \
                           "(stake returned; documented)"
        return "pending", "box score not collected yet"
    stat = row[stat_key]
    # box-score inference first (the stat we actually verified)
    if stat > strike:
        over_wins = True
    elif stat < strike:
        over_wins = False
    else:
        over_wins = None  # exact integer line: push
    # cross-check vs any captured Kalshi result (YES = over on prop series)
    ticker = b["market_ticker"]
    mrow = markets_by_ticker.get(ticker) if ticker else None
    src = f"verified box score ({row['source']}) vs strike {strike:g} ({player} {stat_key}={stat})"
    if mrow and mrow["result"] in ("yes", "no") and over_wins is not None:
        kalshi_over = (mrow["result"] == "yes")
        if kalshi_over != over_wins:
            db.log_anomaly(con, "critical", "prop-settlement-mismatch",
                           {"bet": b["bet_id"], "player": player, "stat": stat,
                            "strike": strike, "kalshi_result": mrow["result"],
                            "box_score_says": "over" if over_wins else "under"})
            # the box score is the independently verified truth of the stat
    won = (b["side"] == "over") == over_wins if over_wins is not None else None
    if won is None:
        return "push", src + " (exact line: push)"
    return ("win" if won else "loss"), src


def _strike_from_selection(sel: str) -> float | None:
    try:
        return float(sel.split()[-1])
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------- elo build

def _build_elo(con) -> EloModel:
    elo = EloModel()
    last_season = None
    for g in con.execute(
            "SELECT * FROM games WHERE status='final' AND home_score IS NOT NULL "
            "ORDER BY COALESCE(tipoff_utc, game_date_et || 'T23:59:59Z')"):
        if last_season and g["season"] != last_season:
            elo.new_season()
        elo.update(g["home_team"], g["away_team"], g["home_score"], g["away_score"])
        last_season = g["season"]
    return elo


def _latest_line(con, game_id) -> float | None:
    row = con.execute(
        "SELECT line FROM odds_snapshots WHERE game_id=? AND market='total' "
        "ORDER BY captured_utc DESC LIMIT 1", (game_id,)).fetchone()
    return row["line"] if row else None


def _latest_spread(con, game_id) -> float | None:
    row = con.execute(
        "SELECT line FROM odds_snapshots WHERE game_id=? AND market='spread' "
        "ORDER BY captured_utc DESC LIMIT 1", (game_id,)).fetchone()
    return row["line"] if row else None


def _injury_state(con, ctx, decision):
    from .backtest import _injury_adjustment
    _injury_adjustment(con, ctx, decision)


def espn_ml_probs(con, game_id, decision, max_age_hours: int = 24):
    """Latest devigged ESPN moneyline probs at/before decision, if fresh enough.

    A days-old snapshot is genuinely stale information; trading divergence
    against it would manufacture an edge that no longer exists.
    """
    import datetime as _dt
    cutoff = util.to_iso(util.parse_iso(decision) - _dt.timedelta(hours=max_age_hours))
    rows = con.execute(
        "SELECT * FROM odds_snapshots WHERE game_id=? AND market='ml' AND captured_utc<=? "
        "AND captured_utc>=? ORDER BY captured_utc DESC LIMIT 2",
        (game_id, decision, cutoff)).fetchall()
    home = away = None
    for r in rows:
        if r["selection"] == "home" and r["price"]:
            home = r["price"]
        if r["selection"] == "away" and r["price"]:
            away = r["price"]
    if home and away and ((home > 0) != (away > 0)):
        h, a = engine.devig_home_away(home, away)
        return h, a
    return None
