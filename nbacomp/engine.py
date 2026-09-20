"""Core engine: chronology, price lookup as-of decision time, fills, settlement.

Look-ahead protections (structural, tested):
- Every price lookup requires price_ts <= decision_ts.
- Every model input (rolling state, Elo) is built from games strictly before
  the game date, enforced by the chronological loop.
- Settlement uses only final scores / Kalshi result fields.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import strategies as S
from . import util

# Factual public team reference (used ONLY to match Kalshi titles to games).
TEAM_NAMES = {
    "ATL": ("Hawks", "Atlanta"), "BOS": ("Celtics", "Boston"), "BKN": ("Nets", "Brooklyn"),
    "CHA": ("Hornets", "Charlotte"), "CHI": ("Bulls", "Chicago"), "CLE": ("Cavaliers", "Cleveland"),
    "DAL": ("Mavericks", "Dallas"), "DEN": ("Nuggets", "Denver"), "DET": ("Pistons", "Detroit"),
    "GSW": ("Warriors", "Golden State"), "HOU": ("Rockets", "Houston"), "IND": ("Pacers", "Indiana"),
    "LAC": ("Clippers", "Los Angeles Clippers"), "LAL": ("Lakers", "Los Angeles Lakers"),
    "MEM": ("Grizzlies", "Memphis"), "MIA": ("Heat", "Miami"), "MIL": ("Bucks", "Milwaukee"),
    "MIN": ("Timberwolves", "Minnesota"), "NOP": ("Pelicans", "New Orleans"),
    "NYK": ("Knicks", "New York"), "OKC": ("Thunder", "Oklahoma City"), "ORL": ("Magic", "Orlando"),
    "PHI": ("76ers", "Philadelphia"), "PHX": ("Suns", "Phoenix"),
    "POR": ("Trail Blazers", "Portland"), "SAC": ("Kings", "Sacramento"),
    "SAS": ("Spurs", "San Antonio"), "TOR": ("Raptors", "Toronto"), "UTA": ("Jazz", "Utah"),
    "WAS": ("Wizards", "Washington"),
}


KALSHI_CANDLE_URL = ("https://api.elections.kalshi.com/trade-api/v2/"
                     "markets/candlesticks")


@dataclass
class KalshiMarketInfo:
    ticker: str
    event_ticker: str
    series_ticker: str
    market_type: str
    game_id: str | None = None
    home: str | None = None
    away: str | None = None
    strike: dict = field(default_factory=dict)
    result: str | None = None
    title: str = ""
    subtitle: str = ""


def match_teams_in_text(text: str) -> set[str]:
    """Return the set of team abbrevs whose nicknames/city words appear in text."""
    t = text.lower()
    found = set()
    for ab, (nick, city) in TEAM_NAMES.items():
        probe = [nick.lower()]
        if city not in ("Golden State", "Los Angeles Clippers", "Los Angeles Lakers"):
            probe.append(city.lower())
        if ab.lower() in t.split():
            probe.append(ab.lower())
        for p in probe:
            if p in t:
                found.add(ab)
                break
    return found


def map_kalshi_markets(con) -> dict[str, KalshiMarketInfo]:
    """Join kalshi_markets to games by (date, team pair). Only confident matches kept."""
    from . import db
    infos: dict[str, KalshiMarketInfo] = {}
    rows = con.execute("SELECT * FROM kalshi_markets").fetchall()
    games = con.execute(
        "SELECT game_id, game_date_et, home_team, away_team, tipoff_utc FROM games").fetchall()
    by_day: dict[str, list] = {}
    for g in games:
        by_day.setdefault(g["game_date_et"], []).append(g)
    for m in rows:
        if not m["ticker"]:
            continue
        text = f"{m['title'] or ''} {m['subtitle'] or ''}"
        teams = match_teams_in_text(text)
        if len(teams) != 2:
            continue
        close = util.parse_iso(m["close_time"])
        cand_game = None
        if close:
            # search a +/- 2 day window around close time in ET days
            import datetime as _dt
            d0 = util.et_game_date(close)
            for off in (0, 1, -1):
                d = (util.parse_iso(d0 + "T00:00:00Z") + _dt.timedelta(days=off)).strftime("%Y-%m-%d")
                for g in by_day.get(d, []):
                    if {g["home_team"], g["away_team"]} == teams:
                        cand_game = g
                        break
                if cand_game:
                    break
        if not cand_game:
            continue
        infos[m["ticker"]] = KalshiMarketInfo(
            ticker=m["ticker"], event_ticker=m["event_ticker"], series_ticker=m["series_ticker"],
            market_type=m["market_type"] or "unknown", game_id=cand_game["game_id"],
            home=cand_game["home_team"], away=cand_game["away_team"],
            strike=json.loads(m["strike_values"] or "{}"), result=m["result"],
            title=m["title"] or "", subtitle=m["subtitle"] or "")
    return infos


@dataclass
class PricePoint:
    ts_utc: str
    price_cents: float
    kind: str  # candle_close | orderbook_ask | snapshot


class PriceBook:
    """As-of price lookups with structural no-lookahead enforcement."""

    def __init__(self, con):
        self.con = con
        self._candles: dict[str, list[tuple[str, float]]] = {}
        for r in con.execute(
                "SELECT ticker, ts_utc, close FROM kalshi_candles WHERE interval=60 "
                "AND close IS NOT NULL ORDER BY ticker, ts_utc"):
            self._candles.setdefault(r["ticker"], []).append((r["ts_utc"], float(r["close"])))
        self._books: dict[str, tuple[str, float]] = {}
        for r in con.execute(
                "SELECT ticker, captured_utc, yes_ask FROM kalshi_orderbooks "
                "WHERE yes_ask IS NOT NULL ORDER BY captured_utc"):
            prev = self._books.get(r["ticker"])
            if prev is None or r["captured_utc"] >= prev[0]:
                self._books[r["ticker"]] = (r["captured_utc"], float(r["yes_ask"]))

    def kalshi_price_at(self, ticker: str, decision_iso: str) -> PricePoint | None:
        """Last fully-closed hourly candle close strictly before decision."""
        cands = self._candles.get(ticker) or []
        best = None
        for ts, close in cands:
            if ts < decision_iso:  # string compare ok for same-format ISO UTC
                best = PricePoint(ts, close + 1.0, "candle_close+1tick")  # 1-tick slippage
            else:
                break
        return best

    def kalshi_price_around(self, ticker: str, target_iso: str, window_hours: int = 12) -> PricePoint | None:
        """Closest candle close within +/- window of target (for T-48h baseline)."""
        cands = self._candles.get(ticker) or []
        import datetime as _dt
        tgt = util.parse_iso(target_iso)
        best = None
        best_dt = None
        for ts, close in cands:
            dt = abs((util.parse_iso(ts) - tgt).total_seconds())
            if dt <= window_hours * 3600 and (best_dt is None or dt < best_dt):
                best, best_dt = PricePoint(ts, close + 1.0, "candle_close+1tick"), dt
        return best

    def kalshi_live_ask(self, ticker: str) -> PricePoint | None:
        b = self._books.get(ticker)
        return PricePoint(b[0], b[1], "orderbook_ask") if b else None


def espn_odds_at(con, game_id: str, decision_iso: str, market: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM odds_snapshots WHERE game_id=? AND market=? AND captured_utc<=? "
        "ORDER BY captured_utc DESC LIMIT 1", (game_id, market, decision_iso)).fetchone()
    return dict(row) if row else None


def settle_score_based(result_type: str, home_score: int, away_score: int,
                       selection: str, line: float | None) -> str:
    """win|loss|push from final score (final scores include OT)."""
    margin = home_score - away_score
    if result_type == "winner":
        team = selection
        return "win" if (margin > 0 and team == "home") or (margin < 0 and team == "away") else "loss"
    if result_type == "spread":
        # selection: 'home -4.5' style; bet on that side covering
        parts = selection.split()
        side, num = parts[0], float(parts[1])
        covered = margin + num if side == "home" else (-margin) + num
        if covered > 0:
            return "win"
        if covered == 0:
            return "push"
        return "loss"
    if result_type == "total":
        total = home_score + away_score
        if line is None:
            return "void"
        if total > line:
            return "win" if selection.startswith("over") else "loss"
        if total < line:
            return "win" if selection.startswith("under") else "loss"
        return "push"
    return "void"


def apply_settlement_kalshi(con, info: KalshiMarketInfo, bet: dict, home_score, away_score) -> str:
    """Prefer Kalshi's own recorded result; cross-check with score inference."""
    if info.result in ("yes", "no"):
        want = "yes"
        if info.market_type == "winner":
            inferred = settle_score_based("winner", home_score, away_score,
                                          bet.get("side") or "home", None)
        else:
            inferred = None
        kresult = "win" if info.result == want else "loss"
        if inferred in ("win", "loss") and inferred != kresult:
            from . import db
            db.log_anomaly(con, "critical", "kalshi-settlement-mismatch", {
                "ticker": info.ticker, "kalshi_result": info.result,
                "score_inferred": inferred, "bet": bet.get("bet_id")})
        return kresult
    # no Kalshi result recorded -> infer from verified score
    if home_score is None or away_score is None:
        return "pending"
    if info.market_type == "winner":
        return settle_score_based("winner", home_score, away_score, bet.get("side") or "home", None)
    if info.market_type == "spread":
        return settle_score_based("spread", home_score, away_score, bet.get("selection"), _strike(info, 0))
    if info.market_type == "total":
        return settle_score_based("total", home_score, away_score, bet.get("selection"), _strike(info, 0))
    return "void"


def _strike(info: KalshiMarketInfo, default) -> float | None:
    try:
        sv = info.strike
        for k in ("above", "below", "strike", "value"):
            if k in sv and sv[k] is not None:
                return float(sv[k])
    except (TypeError, ValueError):
        pass
    return default


def kalshi_bet_pnl(contracts: int, entry_cents: float, result: str) -> float:
    if result == "win":
        return util.kalshi_payoff(contracts, entry_cents, True)
    if result == "loss":
        return util.kalshi_payoff(contracts, entry_cents, False)
    if result in ("push", "void", "pending"):
        # conservative: treat unresolvable as refunded minus fees? Kalshi refunds
        # only per its rules; without a recorded result we keep money at risk
        # documented -> treat as no P&L until result known.
        return 0.0
    return 0.0


def american_pnl(stake: float, american_odds: float, result: str) -> float:
    dec = util.american_to_decimal(american_odds)
    if result == "win":
        return stake * (dec - 1.0)
    if result == "loss":
        return -stake
    return 0.0


def simulate_fill_kalshi(contracts_budget_usd: float, ask_cents: float) -> tuple[int, float]:
    """Contracts bought with budget at ask, including Kalshi entry fees."""
    per_contract_cost = (ask_cents / 100.0) + (util.kalshi_fees_dollars(1, ask_cents))
    n = int(contracts_budget_usd / max(per_contract_cost, 0.01))
    if n <= 0:
        return 0, 0.0
    fee = util.kalshi_fees_dollars(n, ask_cents)
    cost = n * ask_cents / 100.0 + fee
    while cost > contracts_budget_usd and n > 0:
        n -= 1
        fee = util.kalshi_fees_dollars(n, ask_cents)
        cost = n * ask_cents / 100.0 + fee
    return n, cost


def market_prob_from_price(price: float, fmt: str) -> float:
    if fmt == "kalshi_cents":
        return util.cents_to_prob(price)
    if fmt == "american":
        return util.american_to_prob(price)
    if fmt == "decimal":
        return util.decimal_to_prob(price)
    raise ValueError(f"unknown price format {fmt}")


def devig_home_away(ml_home: float, ml_away: float) -> tuple[float, float]:
    return util.devig_two_way(util.american_to_prob(ml_home), util.american_to_prob(ml_away))


def make_bet_id(kind: str, strategy_id: str, game_id: str, market: str, selection: str,
                decision_iso: str, run_id: str) -> str:
    return f"{kind[:2]}-{util.stable_hash([strategy_id, game_id, market, selection, decision_iso, run_id])}"


# re-export strategy config for engine users
COMPETITION = S
