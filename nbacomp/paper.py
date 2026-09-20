"""Forward paper-trading engine — the live competition.

Generates bets from CURRENT captured snapshots (ESPN odds, Kalshi orderbooks),
sizes them against persistent per-strategy bankrolls, marks open positions,
and settles finished games from verified results or Kalshi's own result field.

Everything is timestamped; the decision state (prices + injury board) is
frozen in the bet row at creation and never rewritten.
"""
from __future__ import annotations

import json
from datetime import timedelta

from . import db, engine, strategies as S, util
from .backtest import ELO_PER_POINT, margin_to_prob
from .model import SD_TOTAL, EloModel, RollingTeamState, expected_total


def _bankroll(con, strategy_id: str) -> float:
    row = con.execute(
        "SELECT current FROM bankroll_events WHERE strategy_id=? ORDER BY as_of_utc DESC, id DESC LIMIT 1",
        (strategy_id,)).fetchone()
    return float(row["current"]) if row else S.STARTING_BANKROLL


def _record_bankroll(con, strategy_id: str, current: float, reason: str):
    settled_open = con.execute(
        "SELECT COALESCE(SUM(stake_usd),0) s FROM bets WHERE strategy_id=? AND kind='forward' "
        "AND result='pending' AND execution_status='simulated_fill'", (strategy_id,)).fetchone()["s"]
    starting = S.STARTING_BANKROLL
    db.insert(con, "bankroll_events", {
        "strategy_id": strategy_id, "as_of_utc": util.utcnow_iso(),
        "starting": starting, "current": round(current, 2),
        "available": round(current - settled_open, 2),
        "exposure": round(settled_open, 2), "reason": reason}, replace=True)


def generate_forward_bets(con) -> int:
    now = util.utcnow_iso()
    mapping = engine.map_kalshi_markets(con)
    book = engine.PriceBook(con)
    by_game: dict[str, dict[str, engine.KalshiMarketInfo]] = {}
    for info in mapping.values():
        if info.market_type in ("winner", "spread", "total"):
            by_game.setdefault(info.game_id, {})[info.market_type] = info

    upcoming = con.execute(
        "SELECT * FROM games WHERE status IN ('scheduled','in') AND tipoff_utc IS NOT NULL "
        "AND tipoff_utc > ? ORDER BY tipoff_utc", (now,)).fetchall()
    # also allow pre-tip games that already started within grace (skip: never bet in-running)

    elo = _build_elo(con)
    rolling = RollingTeamState(window=15)
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
        decision = now  # decisions are made at collection time, before tipoff
        if (tip - util.parse_iso(now)) < timedelta(hours=1):
            continue  # never bet within 1h of tipoff (execution latency guard)
        gdate = g["game_date_et"]
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
            "total_line": _latest_line(con, g["game_id"]),
            "inj_adj": {g["home_team"]: 0.0, g["away_team"]: 0.0},
            "inj_flag": {g["home_team"]: None, g["away_team"]: None},
        }
        _injury_state(con, ctx, decision)
        if winner is None:
            continue

        live = book.kalshi_live_ask(winner.ticker)
        if not live:
            continue
        signals: list[S.Signal] = []
        p_home, p_away = margin_to_prob(elo.margin(g["home_team"], g["away_team"])
                                        - ctx["inj_adj"][g["home_team"]]
                                        + ctx["inj_adj"][g["away_team"]])
        mkt_home, mkt_away = live.price_cents / 100.0, 1.0 - live.price_cents / 100.0
        flags = ctx["inj_flag"]

        if flags.get(g["away"]) or flags.get(g["home"]):
            for side, prob, mkt in (("home", p_home, mkt_home), ("away", p_away, mkt_away)):
                team = g["home_team"] if side == "home" else g["away_team"]
                if flags.get(team) and prob - mkt >= 0.04:
                    signals.append(engine_make_sig("NBA-005", ctx, side, prob, mkt, live,
                                                   "opponent missing " + ",".join(flags[team])))
        for side, prob, mkt in (("home", p_home, mkt_home), ("away", p_away, mkt_away)):
            if prob - mkt >= 0.04:
                signals.append(engine_make_sig("NBA-003", ctx, side, prob, mkt, live,
                                               f"Elo margin {elo.margin(g['home_team'], g['away_team']):.1f}"))
        if ctx["a_rest"].get("road_trip_len", 0) >= 5 and ctx["a_rest"].get("tz_shift", 0) >= 2:
            if p_home - mkt_home >= 0.04:
                signals.append(engine_make_sig("NBA-007", ctx, "home", p_home, mkt_home, live,
                                               "road-trip finale + tz shift"))
        if ctx["a_rest"].get("b2b") and ctx["h_rest"].get("rest_days", 0) >= 2 and p_home - mkt_home >= 0.04:
            signals.append(engine_make_sig("NBA-001", ctx, "home", p_home, mkt_home, live, "B2B fade"))

        # cross-market divergence NBA-012
        ml = espn_ml_probs(con, g["game_id"], decision)
        if ml:
            dev_h, dev_a = ml
            for side, kprob, kside_price in (("home", mkt_home, live.price_cents),
                                             ("away", mkt_away, 100.0 - live.price_cents)):
                sp = dev_h if side == "home" else dev_a
                if kprob is not None and abs(sp - kprob) >= 0.04 and kprob < sp:
                    signals.append(engine_make_sig("NBA-012", ctx, side, sp, kprob, live,
                                                   f"ESPN devig {sp:.3f} vs Kalshi {kprob:.3f}"))

        for sig in signals:
            n += _place_forward_bet(con, ctx, sig, winner, live)
    return n


def engine_make_sig(sid, ctx, side, prob, mkt, live, trigger) -> S.Signal:
    team = ctx[side]
    return S.Signal(
        strategy_id=sid, game_id=ctx["game"]["game_id"], market="kalshi:winner",
        selection=team, side=side,
        price=live.price_cents if side == "home" else 100.0 - live.price_cents,
        price_format="kalshi_cents", source="kalshi:orderbook",
        model_prob=prob, market_prob=mkt, trigger=trigger,
        game_label=f"{ctx['away']} @ {ctx['home']} {ctx['game']['game_date_et']}",
        tipoff_utc=ctx["game"]["tipoff_utc"], source_ts=live.ts_utc)


def _place_forward_bet(con, ctx, sig: S.Signal, winner, live: engine.PricePoint) -> int:
    meta = S.STRATEGIES[sig.strategy_id]
    br = _bankroll(con, sig.strategy_id)
    # open exposure cap 25% of current bankroll
    open_exp = con.execute(
        "SELECT COALESCE(SUM(stake_usd),0) s FROM bets WHERE strategy_id=? AND kind='forward' "
        "AND result='pending'", (sig.strategy_id,)).fetchone()["s"]
    if open_exp >= br * 0.25:
        return 0
    prob = sig.model_prob
    stake = S.stake_for(min(br, br - open_exp), prob, util.prob_to_fair_decimal(prob))
    if stake <= 0:
        return 0
    price_cents = sig.price
    contracts, cost = engine.simulate_fill_kalshi(stake, price_cents)
    if contracts <= 0:
        return 0
    bet_id = engine.make_bet_id("forward", sig.strategy_id, ctx["game"]["game_id"],
                                sig.market, sig.selection, ctx["decision"], "forward")
    existing = con.execute("SELECT bet_id FROM bets WHERE bet_id=?", (bet_id,)).fetchone()
    if existing:
        return 0
    ev = (prob * (contracts - cost)) - ((1 - prob) * cost)
    db.insert(con, "bets", {
        "bet_id": bet_id, "run_id": "forward", "kind": "forward",
        "strategy_id": sig.strategy_id, "strategy_version": meta["version"],
        "username": meta["username"], "decision_utc": ctx["decision"],
        "game_id": ctx["game"]["game_id"], "game_label": sig.game_label,
        "tipoff_utc": ctx["game"]["tipoff_utc"], "market": "kalshi:winner",
        "selection": sig.selection, "side": sig.side,
        "price": price_cents, "price_format": "kalshi_cents",
        "source": "kalshi:orderbook", "source_url": "https://api.elections.kalshi.com/trade-api/v2/markets/orderbooks",
        "source_ts": live.ts_utc, "model_prob": prob, "market_prob": price_cents / 100.0,
        "edge": prob - price_cents / 100.0, "stake_usd": cost,
        "to_win_usd": round(contracts - cost, 2), "ev_usd": round(ev, 2),
        "execution_status": "simulated_fill", "fill_price": price_cents,
        "fee_usd": util.kalshi_fees_dollars(contracts, price_cents),
        "contracts": contracts, "result": "pending",
        "verification": "price: live observed Kalshi orderbook ask at decision time",
        "notes": sig.trigger[:500]}, replace=True)
    _record_bankroll(con, sig.strategy_id, br, "bet-placed")
    db.log_audit(con, "paper-engine", "bet-created", bet_id, {
        "strategy": sig.strategy_id, "price_cents": price_cents, "contracts": contracts})
    return 1


def mark_open_positions(con):
    """Update nothing in bet rows (immutable); expose marks via bankroll page data."""
    return 0


def settle_finished(con) -> int:
    """Settle pending forward bets whose game is final (verified or Kalshi result)."""
    mapping = engine.map_kalshi_markets(con)
    by_game: dict[str, engine.KalshiMarketInfo] = {}
    for info in mapping.values():
        if info.market_type == "winner":
            by_game[info.game_id] = info
    pending = con.execute(
        "SELECT * FROM bets WHERE kind='forward' AND result='pending'").fetchall()
    n = 0
    CUR = util.utcnow_iso()
    for b in pending:
        g = con.execute("SELECT * FROM games WHERE game_id=?", (b["game_id"],)).fetchone()
        if not g or g["status"] != "final" or g["home_score"] is None:
            continue
        info = by_game.get(b["game_id"])
        if info and info.ticker.startswith("KXNBA"):
            result = engine.apply_settlement_kalshi(con, info, {"side": b["side"]},
                                                    g["home_score"], g["away_score"])
        else:
            result = engine.settle_score_based("winner", g["home_score"], g["away_score"],
                                               b["side"], None)
        if result not in ("win", "loss"):
            continue
        pnl = engine.kalshi_bet_pnl(b["contracts"], b["fill_price"], result)
        # bets are immutable: record settlement by appending an audit entry and
        # updating ONLY the settlement columns (not decision columns)
        con.execute(
            "UPDATE bets SET result=?, settlement_utc=?, settlement_source=?, pnl_usd=?, roi=? "
            "WHERE bet_id=?",
            (result, CUR, "kalshi result / verified score", round(pnl, 2),
             round(pnl / b["stake_usd"], 4) if b["stake_usd"] else 0, b["bet_id"]))
        db.log_audit(con, "paper-engine", "bet-settled", b["bet_id"],
                     {"result": result, "pnl": round(pnl, 2),
                      "kalshi_result": info.result if info else None})
        _record_bankroll(con, b["strategy_id"], _bankroll(con, b["strategy_id"]) + pnl,
                         f"settle:{b['bet_id']}")
        n += 1
    return n


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


def _injury_state(con, ctx, decision):
    from .backtest import _injury_adjustment
    _injury_adjustment(con, ctx, decision)


def espn_ml_probs(con, game_id, decision) -> tuple[float, float] | None:
    rows = con.execute(
        "SELECT * FROM odds_snapshots WHERE game_id=? AND market='ml' AND captured_utc<=? "
        "ORDER BY captured_utc DESC LIMIT 2", (game_id, decision)).fetchall()
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
