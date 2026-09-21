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

from . import db, engine, strategies as S, util, validation
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
            # store_winner files the HOME team's market as "winner" (live
            # shape is one market per team) and keeps the other side around.
            engine.store_winner(by_game.setdefault(info.game_id, {}), info)

    elo = EloModel()
    rolling = RollingTeamState(window=15)
    # Schedule-only state (rest/B2B/road-trip/travel need dates+venues, not
    # box scores): fed from finals chronologically, strictly-prior per game.
    sched = RollingTeamState(window=15)
    log_by_team: dict[str, list] = {}
    for r in con.execute("SELECT * FROM team_gamelogs ORDER BY game_date_et"):
        log_by_team.setdefault(r["team"], []).append(dict(r))
    log_pos: dict[str, int] = {t: 0 for t in log_by_team}

    def advance(team: str, before_date: str, season: str | None = None):
        """Feed same-season box scores strictly before the game date.

        2026-09-21 fix: the rolling window used to be filled with games from
        ANY season still inside the last 15 rows, so a season's first weeks
        were priced off the previous season's form and a game played in
        October could be "explained" by January box scores.
        """
        rows = log_by_team.get(team) or []
        i = log_pos.get(team, 0)
        while i < len(rows) and rows[i]["game_date_et"] < before_date:
            r = rows[i]
            if season is None or r.get("season") == season:
                rolling.add_game(r)
            i += 1
        log_pos[team] = i

    bankroll: dict[str, float] = {}
    n_bets = 0
    CUR = util.utcnow_iso()
    _tiers = validation.all_tiers(con)

    for g in games:
        tip = util.parse_iso(g["tipoff_utc"])
        gdate = g["game_date_et"]
        decision = util.to_iso(tip - timedelta(hours=2)) if tip else None
        if not decision:
            continue

        advance(g["home_team"], gdate, g["season"])
        advance(g["away_team"], gdate, g["season"])

        winner = by_game.get(g["game_id"], {}).get("winner")
        h_hist = [r for r in sched.history.get(g["home_team"], [])
                  if r.get("game_date_et") and r["game_date_et"] < gdate]
        a_hist = [r for r in sched.history.get(g["away_team"], [])
                  if r.get("game_date_et") and r["game_date_et"] < gdate]
        ctx = {
            "game": g, "decision": decision, "elo": elo,
            "home": g["home_team"], "away": g["away_team"],
            **_roll_state(rolling, g["home_team"], g["away_team"], gdate),
            "h_rest": sched.rest_and_travel(g["home_team"], gdate, True, g["season"]),
            "a_rest": sched.rest_and_travel(g["away_team"], gdate, False, g["season"]),
            "h_prev": h_hist[-1] if h_hist else None,
            "a_prev": a_hist[-1] if a_hist else None,
            "h_streak": team_streak(h_hist),
            "a_streak": team_streak(a_hist),
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
            # Validation policy (2026-09-21): tiers derived from stored
            # evidence decide whether a rule may trade at all, blend the model
            # probability toward the market (shrinkage) and cap the credible
            # edge. Identical policy in the forward engine so backtest and
            # paper trading remain the same rule.
            signals = validation.apply_policy(con, signals, tiers=_tiers)
            # de-duplicate within the run (same strategy can emit the same
            # signal twice from two code paths, e.g. both teams hot)
            seen_sigs: set = set()
            placed_markets: set = set()
            for sig in signals:
                key = (sig.strategy_id, sig.market, sig.selection, sig.side)
                if key in seen_sigs:
                    continue
                seen_sigs.add(key)
                n_bets += _place_kalshi_bet(con, ctx, sig, winner, price_h,
                                            run_id, bankroll, CUR, placed_markets)

        # update state WITH this game only after its bets are placed
        elo.update(g["home_team"], g["away_team"], g["home_score"], g["away_score"])
        hw = 1 if (g["home_score"] or 0) > (g["away_score"] or 0) else 0
        # scores + W/L feed the streak/bounce rules (NBA-020/021); the row is
        # only visible to STRICTLY LATER games in the chronological loop.
        sched.add_game({"team": g["home_team"], "game_date_et": gdate,
                        "is_home": 1, "venue_team": None, "pts": g["home_score"],
                        "opp_pts": g["away_score"], "wl": "W" if hw else "L"})
        sched.add_game({"team": g["away_team"], "game_date_et": gdate,
                        "is_home": 0, "venue_team": g["home_team"],
                        "pts": g["away_score"], "opp_pts": g["home_score"],
                        "wl": "L" if hw else "W"})
        advance(g["home_team"], _next_day(gdate))
        advance(g["away_team"], _next_day(gdate))

    db.log_collection(con, "backtest", "engine", "ok", f"{run_id}: bets={n_bets}", rows=n_bets)
    return {"bets": n_bets, "games": len(games)}


def _next_day(d: str) -> str:
    from datetime import date, timedelta
    y, m, dd = map(int, d.split("-"))
    return (date(y, m, dd) + timedelta(days=1)).isoformat()


def team_streak(rows: list[dict]) -> int:
    """Current streak from strictly-prior finals: +N win streak, -N loss streak.

    Uses the final score only (an OT loss is a loss — the games table stores
    final scores, so no regulation/OT distinction is possible or needed).
    """
    n = 0
    for r in reversed(rows or []):
        wl = r.get("wl")
        if not wl:
            break
        if n == 0:
            n = 1 if wl == "W" else -1
            continue
        expect = "W" if n > 0 else "L"
        if wl != expect:
            break
        n = n + 1 if n > 0 else n - 1
    return n


def _roll_state(rolling, home: str, away: str, game_date_et: str) -> dict:
    """Rolling state for both teams, refusing STALE state.

    Returns ``h_roll``/``a_roll`` = None when the team's most recent prior
    observation is older than ``MAX_STATE_AGE_DAYS`` (or missing entirely), so
    every rolling-statistics rule skips the game instead of trading on
    months-old form. ``h_state_age_days``/``a_state_age_days`` are published in
    the context for audit and for the site.
    """
    from .model import MAX_STATE_AGE_DAYS, state_age_days
    out: dict = {}
    for side, team in (("h", home), ("a", away)):
        rows = rolling.history.get(team) or []
        age = state_age_days(rows, game_date_et)
        fresh = age is not None and age <= MAX_STATE_AGE_DAYS
        out[f"{side}_roll"] = rolling.team_rolling(team) if fresh else None
        out[f"{side}_state_age_days"] = age
    return out


def _place_kalshi_bet(con, ctx, sig: S.Signal, winner: engine.KalshiMarketInfo,
                      price_h: engine.PricePoint, run_id: str, bankroll: dict, CUR: str,
                      placed_markets: set | None = None) -> int:
    meta = S.STRATEGIES[sig.strategy_id]
    br = bankroll.setdefault(sig.strategy_id, S.STARTING_BANKROLL)
    prob = sig.model_prob
    if not 0.0 < prob < 1.0:
        return 0
    # One bet per strategy per market per game: a strategy emitting two
    # opposite signals in the same game (e.g. NBA-010 hot home + cold away
    # both on totals) would otherwise hedge itself.
    if placed_markets is not None:
        mk = (sig.strategy_id, sig.market)
        if mk in placed_markets:
            return 0
        placed_markets.add(mk)
    # Never double-bet a game+market already bet by this strategy in this
    # run (re-runs with more rolling data must not add a conflicting bet).
    if con.execute(
            "SELECT 1 FROM bets WHERE strategy_id=? AND game_id=? AND market=? "
            "AND run_id=? LIMIT 1",
            (sig.strategy_id, ctx["game"]["game_id"], sig.market, run_id)).fetchone():
        return 0
    directional = sig.strategy_id == "NBA-004"
    if sig.market == "kalshi:winner":
        tick = price_h if sig.selection == "home" else engine.PricePoint(
            price_h.ts_utc, 100.0 - price_h.price_cents, "no_side_derived")
        mkt_prob = tick.price_cents / 100.0
        scale = validation.classify(con, sig.strategy_id)["policy"]["stake_scale"]
        if directional:
            # NBA-004 (v1.1.0): no independent probability estimate exists for
            # a move-follow rule, so Kelly is undefined (f = 0 for every
            # price). Fixed 1% stake, bet row labeled directional.
            stake = round(br * S.LINE_MOVE_STAKE_PCT * scale, 2)
            if stake < S.MIN_STAKE:
                return 0
            prob = mkt_prob  # honest: no probability edge claimed
        else:
            # Kelly payout odds = the decimal odds actually paid (Kalshi price c
            # pays 1:1 on c/100 staked -> decimal 100/c). Passing the MODEL's fair
            # decimal (1/p) would make Kelly f* identically zero -> no bets ever.
            stake = S.stake_for(br, prob, 100.0 / tick.price_cents) * scale
            if stake < S.MIN_STAKE:
                return 0
        contracts, cost = engine.simulate_fill_kalshi(stake, tick.price_cents)
        if contracts <= 0:
            return 0
        bet_id = engine.make_bet_id("backtest", sig.strategy_id, ctx["game"]["game_id"],
                                    sig.market, sig.selection, ctx["decision"], run_id)
        result = engine.apply_settlement_kalshi(con, winner,
                                                {"bet_id": bet_id, "side": sig.side,
                                                 "selection": sig.selection},
                                                ctx["game"]["home_score"], ctx["game"]["away_score"])
        pnl = (engine.kalshi_bet_pnl(contracts, tick.price_cents, result)
               if result in ("win", "loss") else 0.0)
        fee = util.kalshi_fees_dollars(contracts, tick.price_cents)
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
            "notes": (("[directional move-follow; model_prob = market prob, fixed 1% "
                       "stake] " if directional else "") + sig.trigger[:450]),
        }, replace=False)  # append-only: re-runs must never rewrite history
        if result in ("win", "loss"):
            bankroll[sig.strategy_id] = br + pnl
        return 1
    # line-based totals bets (model vs observed total line): sportsbook-style at
    # fair odds -110 (documented assumption; no free historical total prices)
    if sig.market == "total":
        scale = validation.classify(con, sig.strategy_id)["policy"]["stake_scale"]
        # real observed over/under price when one was captured as-of the
        # decision; otherwise the documented -110 assumption
        price, priced_real = _observed_total_price(con, ctx, sig, -110)
        dec = util.american_to_decimal(price)
        stake = S.stake_for(br, sig.model_prob, dec) * scale
        if stake < S.MIN_STAKE:
            return 0
        hs, as_ = ctx["game"]["home_score"], ctx["game"]["away_score"]
        result = engine.settle_score_based("total", hs, as_, sig.selection, _line_of(sig))
        pnl = engine.american_pnl(stake, price, result)
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
            "price": price, "price_format": "american",
            "source": ("price: espn over/under snapshot" if priced_real
                       else "line: kalshi strike or espn snapshot"),
            "source_url": "https://api.elections.kalshi.com/trade-api/v2/markets",
            "source_ts": ctx["decision"], "model_prob": sig.model_prob,
            "market_prob": round(util.american_to_prob(price), 4),
            "edge": sig.model_prob - util.american_to_prob(price),
            "stake_usd": stake,
            "to_win_usd": round(stake * (util.american_to_decimal(price) - 1.0), 2),
            "execution_status": "simulated_fill", "fill_price": price, "fee_usd": 0,
            "result": result, "settlement_utc": CUR,
            "settlement_source": "verified final score",
            "pnl_usd": round(pnl, 2), "roi": round(pnl / stake, 4),
            "verification": ("observed ESPN over/under price at decision time"
                             if priced_real else
                             "PRICED-ASSUMPTION: no free historical totals price source; "
                             "simulated at standard -110. Labeled, not hidden."),
            "notes": sig.trigger[:500]}, replace=False)  # append-only
        bankroll[sig.strategy_id] = br + pnl
        return 1
    return 0


def _line_of(sig) -> float | None:
    try:
        return float(sig.selection.split()[-1])
    except (ValueError, IndexError):
        return None


def _observed_total_price(con, ctx, sig, fallback: int) -> tuple[float, bool]:
    """Observed american price for the total side at/before the decision.

    ESPN's odds payload carries `overOdds`/`underOdds` for many events; those
    rows are stored with price_format='american'. When no such row exists we
    fall back to the documented standard price and report priced_real=False so
    the bet row can be labelled PRICED-ASSUMPTION.
    """
    side = (sig.side or "").lower()
    if side in ("over", "under"):
        row = con.execute(
            "SELECT price FROM odds_snapshots WHERE game_id=? AND market='total' "
            "AND selection LIKE ? AND price_format='american' AND captured_utc<=? "
            "ORDER BY captured_utc DESC LIMIT 1",
            (ctx["game"]["game_id"], side + " %", ctx["decision"])).fetchone()
        if row and row["price"] not in (None, 0):
            return float(row["price"]), True
    return float(fallback), False


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

def _field(row, name: str, default=None):
    """Safe field access for sqlite3.Row (which has no .get()) and dicts."""
    try:
        if isinstance(row, dict):
            return row.get(name, default)
        if name in row.keys():
            return row[name]
        return default
    except Exception:
        return default


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


def _px(ctx) -> engine.PricePoint | None:
    """Decision-time price: live observed ask in forward, candles in backtest.

    The backtest never sets ctx["live_price"], so its price path is purely
    historical candles (proven by test). The forward engine sets it to the
    observed orderbook ask — without this override the shared evaluators
    would look up (nonexistent) candles and forward would never fire.
    """
    if ctx.get("live_price") is not None:
        return ctx["live_price"]
    w = ctx.get("winner")
    if w is None:
        return None
    return ctx["book"].kalshi_price_at(w.ticker, ctx["decision"])


def eval_rest(ctx):
    h, a = ctx["h_rest"], ctx["a_rest"]
    # rest_days is None when the team has no prior same-season game (season
    # opener): unknown rest can never satisfy "rested >= 2 days".
    if (not h or not a or not a.get("b2b")
            or h.get("rest_days") is None or h["rest_days"] < 2):
        return []
    price = _px(ctx)
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
    price = _px(ctx)
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
    # Forward: live ask + T-48h orderbook-history baseline (both observed).
    # Backtest: decision candle + T-48h candle baseline. No mixing, ever.
    if ctx.get("live_price") is not None:
        now = ctx["live_price"]
        base = ctx.get("live_baseline")
    else:
        tip = util.parse_iso(ctx["game"]["tipoff_utc"])
        if tip is None or ctx.get("winner") is None:
            return []
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
    price = _px(ctx)
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
    price = _px(ctx)
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
    price = _px(ctx)
    if not price:
        return []
    p_home, _ = _winner_probs(ctx)
    mkt_home = price.price_cents / 100.0
    if p_home - mkt_home >= 0.04:
        sig = _sig(ctx, "NBA-007", "home", p_home, mkt_home, price,
                   f"road-trip finale ({a['road_trip_len']}) + tz shift {a['tz_shift']}")
        return [sig]
    return []


def eval_blowout(ctx):
    """NBA-020: the team that lost its previous game by >= 18 bounces."""
    for side, prev, team in (("home", ctx.get("h_prev"), ctx["home"]),
                             ("away", ctx.get("a_prev"), ctx["away"])):
        if not prev or prev.get("pts") is None or prev.get("opp_pts") is None:
            continue
        margin = prev["pts"] - prev["opp_pts"]
        if margin > -18:
            continue
        price = _px(ctx)
        if not price:
            return []
        p_home, p_away = _winner_probs(ctx)
        p_side = (p_home if side == "home" else p_away) + 0.02  # bounce hypothesis
        mkt_home = price.price_cents / 100.0
        mkt_side = mkt_home if side == "home" else 1.0 - mkt_home
        if p_side - mkt_side >= 0.04:
            sig = _sig(ctx, "NBA-020", side, p_side, mkt_side, price,
                       f"{team} lost previous game by {abs(margin):.0f} pts (bounce rule)")
            return [sig]
    return []


def eval_streak(ctx):
    """NBA-021: fade the market's overreaction to 4+ game streaks.

    Winning streak: the market overvalues the hot team -> bet the OTHER side
    (its probability rises by the overvaluation, +0.03). Losing streak: the
    market overfades the cold team -> bet ON it (+0.03).
    """
    for side, streak in (("home", ctx.get("h_streak") or 0),
                         ("away", ctx.get("a_streak") or 0)):
        if abs(streak) < 4:
            continue
        price = _px(ctx)
        if not price:
            return []
        p_home, p_away = _winner_probs(ctx)
        if streak > 0:
            bet_side = "away" if side == "home" else "home"
            p_bet = (p_away if bet_side == "away" else p_home) + 0.03
            label = (f"fade {ctx[side]} {streak}-game WIN streak "
                     "(market assumed to overvalue the hot team)")
        else:
            bet_side = side
            p_bet = (p_home if bet_side == "home" else p_away) + 0.03
            label = (f"back {ctx[side]} {abs(streak)}-game LOSS streak "
                     "(market assumed to overfade the cold team)")
        p_bet = min(0.97, p_bet)
        mkt_home = price.price_cents / 100.0
        mkt_bet = mkt_home if bet_side == "home" else 1.0 - mkt_home
        if p_bet - mkt_bet >= 0.04:
            sig = _sig(ctx, "NBA-021", bet_side, p_bet, mkt_bet, price, label)
            return [sig]
    return []


def eval_rest_total(ctx):
    """NBA-022: rest asymmetry (3+ days vs 1 day) raises the total (+4 pts)."""
    hr, ar = ctx.get("h_rest"), ctx.get("a_rest")
    if not hr or not ar:
        return []
    if hr.get("rest_days") is None or ar.get("rest_days") is None:
        return []
    rests = (float(hr["rest_days"]), float(ar["rest_days"]))
    if not (max(rests) >= 3.0 and min(rests) <= 1.0):
        return []
    setup = _total_setup(ctx)
    if not setup:
        return []
    exp, line = setup
    # 2026-09-21: the +4.0 assumption was replaced by the MEASURED rest-
    # asymmetry effect (+1.82 points, n=289 vs 3,318, Welch t=1.38 — not
    # significant) published in data/research_scan.json.
    exp_adj = exp + REST_ASYMMETRY_POINTS
    diff = exp_adj - line
    if diff < 6.0:
        return []
    p_side = util.norm_cdf(diff / SD_TOTAL)
    if p_side - 0.524 < 0.02:
        return []
    return [S.Signal(
        strategy_id="NBA-022", game_id=ctx["game"]["game_id"], market="total",
        selection=f"over {line:g}", side="over", price=-110, price_format="american",
        source="kalshi:strike|espn:line", model_prob=p_side, market_prob=0.524,
        trigger=(f"rest asymmetry home/away={rests[0]:.0f}/{rests[1]:.0f} days; "
                 f"model {exp:.1f}+4 vs line {line:g} ({diff:+.1f})"),
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"])]


#: Measured extra scoring in games with asymmetric rest (>=2 day gap) vs
#: symmetric rest, from 3,704 verified regular-season games: +1.82 points
#: (Welch t=1.38, not statistically significant). Used by NBA-022 v1.1.0 in
#: place of the previous unmeasured +4.0.
REST_ASYMMETRY_POINTS = 1.82


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


#: Measured drop in average total for games outside the regular-season window
#: (postseason and preseason pooled, 469 verified games): 219.0 vs 228.4 =
#: -9.4 points. NBA-023 applies it ONLY to rows the scheduler labels
#: postseason; unlabelled rows never fire.
POSTSEASON_TOTAL_SHIFT = 9.4


def eval_playoff_grind(ctx):
    """NBA-023: postseason totals run ~9.4 points below the regular-season model."""
    g = ctx["game"]
    if (_field(g, "season_type") or "").lower() != "postseason":
        return []
    setup = _total_setup(ctx)
    if not setup:
        return []
    exp, line = setup
    diff = (exp - POSTSEASON_TOTAL_SHIFT) - line
    if diff > -3.0:
        return []
    p_under = 1.0 - util.norm_cdf(diff / SD_TOTAL)
    if p_under - 0.524 < 0.02:
        return []
    return [S.Signal(
        strategy_id="NBA-023", game_id=g["game_id"], market="total",
        selection=f"under {line:g}", side="under", price=-110,
        price_format="american", source="kalshi:strike|espn:line",
        model_prob=p_under, market_prob=0.524,
        trigger=(f"postseason total shift -{POSTSEASON_TOTAL_SHIFT:g}: model "
                 f"{exp:.1f} - {POSTSEASON_TOTAL_SHIFT:g} vs line {line:g} "
                 f"({diff:+.1f})"),
        game_label=f"{ctx['away']} @ {ctx['home']} {g['game_date_et']}",
        tipoff_utc=g["tipoff_utc"])]


def eval_momentum(ctx):
    """NBA-024: back the streak (measured opposite of NBA-021's fade).

    Fires when a team carries a 4+ game WIN streak (back it) or the opponent
    carries a 4+ game LOSS streak (back the opponent). Streaks are computed
    from strictly prior finals of the same season by the caller.
    """
    hs = int(ctx.get("h_streak") or 0)
    as_ = int(ctx.get("a_streak") or 0)
    side, why = None, ""
    if hs >= 4:
        side, why = "home", f"home {ctx['home']} on a {hs}-game win streak"
    elif as_ >= 4:
        side, why = "away", f"away {ctx['away']} on a {as_}-game win streak"
    elif as_ <= -4:
        side, why = "home", f"away {ctx['away']} on a {abs(as_)}-game losing streak"
    elif hs <= -4:
        side, why = "away", f"home {ctx['home']} on a {abs(hs)}-game losing streak"
    if side is None:
        return []
    price = _px(ctx)
    if price is None:
        return []
    p_home, p_away = _winner_probs(ctx)
    mkt_side = (price.price_cents / 100.0 if side == "home"
                else 1.0 - price.price_cents / 100.0)
    p_side = p_home if side == "home" else p_away
    if p_side - mkt_side < 0.04:
        return []
    return [S.Signal(
        strategy_id="NBA-024", game_id=ctx["game"]["game_id"], market="kalshi:winner",
        selection=side, side=side, price=mkt_side * 100.0, price_format="kalshi_cents",
        source="kalshi:candlesticks", model_prob=p_side, market_prob=mkt_side,
        trigger=f"momentum: {why}",
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"])]


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
    "NBA-020": eval_blowout,
    "NBA-021": eval_streak,
    "NBA-022": eval_rest_total,
    "NBA-023": eval_playoff_grind,
    "NBA-024": eval_momentum,
}
# NBA-013 (1H markets) is forward-only: no historical 1H price series exists
# (KXNBA1H candles accumulate from first listing forward), so it has no
# backtest evaluator by design — not an omission.
