#!/usr/bin/env python
"""Mark-to-market for open paper positions — from captured data only.

The competition spec requires an open position to show its **current price**.
That is only honest if the current price was actually observed, so this module
never estimates one. For every open bet it reports exactly what was captured,
and says so in words when nothing was:

``price``  a real, timestamped price for *this* selection, from the source that
           priced the bet (Kalshi candle close or live ask, or an ESPN snapshot
           that carried a side price rather than a bare line). ``None`` when no
           such observation exists — never a midpoint, never a model guess.

``line``   the market's current line for the bet's game and market (spread or
           total). ESPN's free feed publishes lines without side prices, so this
           is usually the only mark that genuinely exists; it is reported as a
           line, not smuggled in as a price.

``line_move``  current line minus the line observed at the decision timestamp.
           This is the observable line movement the spec asks the engine to
           account for, computed from our own captured snapshots.

``status`` one of:
    ``marked``       a real current price exists; unrealized P&L is computed
    ``line-only``    the market's line moved but no side price was ever captured
    ``unavailable``  nothing at all has been captured for this game/market

Unrealized P&L is emitted only for ``marked`` positions. A ``line-only``
position reports its line move and nothing else — converting a line into a
price would require assuming a vig we never observed.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from nbacomp import util

#: Matches the numeric line inside a selection label ("under 231.5",
#: "home -4.5", "over 227.5"). Used to read the line the bet was struck at.
_LINE_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def selection_line(selection: str | None) -> float | None:
    """The numeric line embedded in a bet's selection label, if there is one."""
    if not selection:
        return None
    m = _LINE_RE.search(str(selection))
    return float(m.group(1)) if m else None


def _latest_line(con, game_id: str, market: str, as_of: str | None = None) -> dict | None:
    """Newest captured line for a game/market, at or before ``as_of``.

    Only rows that actually carry a line are considered; ESPN writes
    ``price_format='line'`` with the line value in ``line``, and a bare 0.0 in
    ``price``. Reading that 0.0 as a price would be a fabrication, so it is
    excluded here.
    """
    sql = ("SELECT line, price, price_format, source, captured_utc FROM odds_snapshots "
           "WHERE game_id=? AND market=? AND line IS NOT NULL")
    params: list[Any] = [game_id, market]
    if as_of:
        sql += " AND captured_utc<=?"
        params.append(as_of)
    sql += " ORDER BY captured_utc DESC, id DESC LIMIT 1"
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None


def _latest_price(con, game_id: str, market: str, selection: str | None) -> dict | None:
    """Newest captured *price* (not a line) for a game/market/selection.

    ESPN's snapshots are line-only, so in practice this returns a row only where
    a source published an actual side price. Kalshi markets are matched by
    ticker instead (see ``_kalshi_mark``).
    """
    sql = ("SELECT price, price_format, selection, source, captured_utc FROM odds_snapshots "
           "WHERE game_id=? AND market=? AND price IS NOT NULL AND price<>0 "
           "AND price_format<>'line'")
    params: list[Any] = [game_id, market]
    if selection:
        sql += " AND selection=?"
        params.append(selection)
    sql += " ORDER BY captured_utc DESC, id DESC LIMIT 1"
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None


def _kalshi_mark(con, ticker: str) -> dict | None:
    """Live ask, else last closed candle, for a Kalshi market ticker.

    Both are real observations with their own timestamps. The orderbook is
    preferred because it is the price a taker would actually pay; the candle is
    the fallback and is labelled as such.
    """
    if not ticker:
        return None
    r = con.execute(
        "SELECT yes_ask, captured_utc FROM kalshi_orderbooks WHERE ticker=? "
        "AND yes_ask IS NOT NULL ORDER BY captured_utc DESC, ticker LIMIT 1",
        (ticker,)).fetchone()
    if r and r["yes_ask"] is not None:
        return {"price": float(r["yes_ask"]), "price_format": "kalshi_cents",
                "kind": "orderbook_ask", "source": "kalshi", "as_of": r["captured_utc"]}
    r = con.execute(
        "SELECT close, ts_utc FROM kalshi_candles WHERE ticker=? AND close IS NOT NULL "
        "ORDER BY ts_utc DESC LIMIT 1", (ticker,)).fetchone()
    if r and r["close"] is not None:
        return {"price": float(r["close"]), "price_format": "kalshi_cents",
                "kind": "candle_close", "source": "kalshi", "as_of": r["ts_utc"]}
    return None


def mark_bet(con, bet: sqlite3.Row | dict) -> dict:
    """Build the mark-to-market record for one open bet.

    Returns a dict that always contains ``status`` and ``reason``, so a caller
    can never render a blank where an explanation belongs.
    """
    b = dict(bet) if isinstance(bet, sqlite3.Row) else bet
    game_id = b.get("game_id")
    market = (b.get("market") or "").replace("kalshi:", "").split(":")[0]
    if market in ("winner", "spread", "total", "1h", "1q"):
        snap_market = {"winner": "ml", "1h": "1h", "1q": "1q"}.get(market, market)
    else:
        snap_market = market
    decision = b.get("decision_utc")

    out: dict[str, Any] = {
        "bet_id": b.get("bet_id"),
        "current_price": None, "current_price_format": None,
        "current_price_source": None, "current_price_as_of": None,
        "current_line": None, "line_at_decision": None, "line_move": None,
        "mark_status": "unavailable", "mark_reason": "", "unrealized_usd": None,
    }

    # --- price: only from the ticker the bet was actually priced on ---------
    mk = _kalshi_mark(con, b.get("market_ticker") or "")
    if mk:
        out.update({"current_price": mk["price"], "current_price_format": mk["price_format"],
                    "current_price_source": f"kalshi:{mk['kind']}",
                    "current_price_as_of": mk["as_of"], "mark_status": "marked",
                    "mark_reason": f"observed {mk['kind']} for {b.get('market_ticker')}"})
    else:
        p = _latest_price(con, game_id, snap_market, b.get("selection"))
        if p:
            out.update({"current_price": float(p["price"]),
                        "current_price_format": p["price_format"],
                        "current_price_source": p["source"],
                        "current_price_as_of": p["captured_utc"], "mark_status": "marked",
                        "mark_reason": f"observed side price from {p['source']}"})

    # --- line: what the market is showing now vs at the decision ------------
    now_line = _latest_line(con, game_id, snap_market)
    then_line = _latest_line(con, game_id, snap_market, as_of=decision) if decision else None
    if now_line:
        out["current_line"] = float(now_line["line"])
        out["line_as_of"] = now_line["captured_utc"]
    if then_line:
        out["line_at_decision"] = float(then_line["line"])
    if out["current_line"] is not None and out["line_at_decision"] is not None:
        out["line_move"] = round(out["current_line"] - out["line_at_decision"], 2)

    if out["mark_status"] != "marked":
        if out["current_line"] is not None:
            out["mark_status"] = "line-only"
            out["mark_reason"] = ("the market's line is captured, but no side PRICE has ever "
                                  "been observed for this selection (ESPN's free feed publishes "
                                  "lines without prices), so no current price is claimed")
        else:
            out["mark_status"] = "unavailable"
            out["mark_reason"] = ("no captured line or price exists for this game/market, so "
                                  "nothing is marked — UNAVAILABLE, not estimated")

    # --- unrealized P&L: only where a real price exists ---------------------
    if out["mark_status"] == "marked" and b.get("price") and out["current_price"] is not None:
        out["unrealized_usd"] = _unrealized(b, float(out["current_price"]))
    return out


def _unrealized(bet: dict, current: float) -> float | None:
    """Mark-to-market P&L of an open position, in the bet's own price format.

    The position's value is what it is worth *if it settles a winner*, weighted
    by the probability the current market price implies:

        value_now = p_now * (stake + to_win)
        unrealized = value_now - stake

    Using ``p_now * to_win - stake`` instead is wrong by a whole stake: at the
    entry price it returns -52.38 on a $100 bet at -110 rather than ~0, because
    the returned stake drops out. Sanity check that this function must satisfy:
    at a price equal to the entry price the mark is ~0.

    Kalshi cents: a contract pays $1 if it wins, so a position of N contracts
    marked at C cents is worth N * C / 100.
    """
    stake = bet.get("stake_usd")
    if stake is None:
        return None
    fmt = (bet.get("price_format") or "").lower()
    try:
        stake = float(stake)
        if fmt.startswith("kalshi"):
            contracts = bet.get("contracts")
            if not contracts:
                return None
            return round(float(contracts) * current / 100.0 - stake, 2)
        p_now = util.american_to_prob(current) if fmt.startswith("american") else (
            1.0 / current if fmt.startswith("decimal") else None)
        if p_now is None:
            return None
        to_win = bet.get("to_win_usd")
        if to_win is None:
            return None
        return round(p_now * (stake + float(to_win)) - stake, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def mark_open_positions(con) -> list[dict]:
    """Marks for every open forward position, ordered by tipoff."""
    rows = con.execute(
        "SELECT * FROM bets WHERE kind='forward' AND result='pending' "
        "AND execution_status='simulated_fill' ORDER BY tipoff_utc").fetchall()
    return [mark_bet(con, r) for r in rows]


def mark_summary(con) -> dict:
    """Counts by mark status — published so an all-UNAVAILABLE book is visible."""
    marks = mark_open_positions(con)
    counts: dict[str, int] = {}
    for m in marks:
        counts[m["mark_status"]] = counts.get(m["mark_status"], 0) + 1
    unreal = [m["unrealized_usd"] for m in marks if m["unrealized_usd"] is not None]
    return {"n_open": len(marks), "by_status": counts,
            "n_with_price_mark": len(unreal),
            "unrealized_usd": round(sum(unreal), 2) if unreal else None,
            "marks": marks}
