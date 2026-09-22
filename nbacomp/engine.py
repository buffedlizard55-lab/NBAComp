"""Core engine: chronology, price lookup as-of decision time, fills, settlement.

Look-ahead protections (structural, tested):
- Every price lookup requires price_ts <= decision_ts.
- Every model input (rolling state, Elo) is built from games strictly before
  the game date, enforced by the chronological loop.
- Settlement uses only final scores / Kalshi result fields.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import strategies as S
from . import util

# Factual public team reference (used ONLY to match Kalshi titles to games).
# ESPN's site API abbreviates six franchises differently from Basketball-
# Reference and from Kalshi's event tickers (verified 2026-09-21 by diffing the
# abbreviations each source actually returned: ESPN GS/NO/NY/SA/UTAH/WSH vs
# BRef GSW/NOP/NYK/SAS/UTA/WAS). Without one canonical form the same game was
# stored twice under different ids (17 `duplicate-game` anomalies in run
# 35554765928) and Kalshi markets could not be joined to games at all,
# because KXNBAGAME tickers use the BRef/NBA.com set.
ESPN_ABBR_TO_CANON = {
    "GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA",
    "WSH": "WAS", "PNX": "PHX",
}


def canon_team(abbrev: str | None) -> str | None:
    """Canonical (NBA.com / BRef / Kalshi) abbreviation for a team, or None."""
    if not abbrev:
        return None
    a = str(abbrev).strip().upper()
    return ESPN_ABBR_TO_CANON.get(a, a)


def is_nba_team(abbrev: str | None) -> bool:
    """True only for the 30 NBA franchises.

    ESPN's NBA scoreboard also lists preseason exhibitions against non-NBA
    clubs; run 35554765928 stored rows for GUANGZHOU, HAPOEL, LON, MEL, STARS,
    STRIPES and WORLD as if they were NBA games, which would have corrupted
    every league-wide feature and the standings.
    """
    return canon_team(abbrev) in TEAM_NAMES


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
    # For winner markets: the team YES pays on (live shape 2026-09-20 is one
    # market per team, e.g. ...OKCSAS-SAS "San Antonio wins"). None = unknown
    # (legacy single-market rows) -> legacy home=YES behavior is kept.
    team: str | None = None


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


_EVENT_TICKER_RE = None


_SUBTITLE_DATE_RE = re.compile(r"\(([A-Z][a-z]{2})\s+(\d{1,2})\)")
_MONTHS = {m: i + 1 for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


def subtitle_dates(sub) -> list[str]:
    """'(Jun 13)' -> candidate ET dates for plausible season years.

    Runner evidence (probe5): settled event subtitles carry month/day with no
    year. An NBA season runs Oct-Jun, so the calendar year of today and the
    year before cover any subtitle we are currently mapping; wrong guesses
    simply find no game and return nothing (never a mis-assignment).
    """
    m = _SUBTITLE_DATE_RE.search(sub or "")
    if not m:
        return []
    mo = _MONTHS.get(m.group(1))
    try:
        dd = int(m.group(2))
    except ValueError:
        return []
    if not mo or not 1 <= dd <= 31:
        return []
    this_year = int(util.utcnow_iso()[:4])
    import datetime as _dt
    out = []
    for yy in (this_year, this_year - 1):
        try:
            out.append(_dt.datetime(yy, mo, dd).strftime("%Y-%m-%d"))
        except ValueError:
            continue
    return out


def market_team(ticker: str | None, title: str | None, subtitle: str | None) -> str | None:
    """Which team a winner market pays YES on, or None if unknowable.

    Primary: the market ticker suffix (live shape ...{EVENT}-{TEAM}, verified
    2026-09-20). Fallback: title/subtitle naming exactly one team
    ("San Antonio wins"). Anything ambiguous returns None and callers keep
    the legacy home=YES assumption (never a guess).
    """
    if ticker and "-" in ticker:
        seg = ticker.rsplit("-", 1)[1].upper()
        if seg in TEAM_NAMES:
            return seg
    teams = match_teams_in_text(f"{title or ''} {subtitle or ''}")
    if len(teams) == 1:
        return next(iter(teams))
    return None


def store_winner(entry: dict, info: KalshiMarketInfo) -> None:
    """File a winner market into a per-game entry, preferring the home side.

    entry["winner"] is the home team's market when one is stored (so the YES
    side is always the home team, as the pricing code assumes); the other
    side's market is kept as entry["winner_away"] for observed (non-derived)
    away pricing and team-exact settlement.
    """
    if info.market_type != "winner":
        entry[info.market_type] = info
        return
    if info.team and info.home and info.team != info.home:
        # Away-team market: NEVER the priced leg (pricing assumes YES=home).
        entry.setdefault("winner_away", info)
        return
    cur = entry.get("winner")
    if cur is None:
        entry["winner"] = info
    elif info.team and info.home and info.team == info.home and cur.team != cur.home:
        entry["winner"] = info


def parse_event_ticker(event_ticker: str):
    """KXNBAGAME-26OCT20OKCSAS -> (date '2026-10-20', {'OKC','SAS'}).

    Kalshi encodes the game date and both team abbreviations in the event
    ticker (runner-verified). Returns (None, set()) when the pattern doesn't
    match or the abbreviations aren't NBA teams.
    """
    global _EVENT_TICKER_RE
    import re
    if _EVENT_TICKER_RE is None:
        _EVENT_TICKER_RE = re.compile(
            r"^[A-Z0-9]+-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})"
            r"([A-Z]{3})([A-Z]{3})$")
    m = _EVENT_TICKER_RE.match(event_ticker or "")
    if not m:
        return None, set()
    yy, mon, dd, t1, t2 = m.groups()
    month = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
             "OCT", "NOV", "DEC"].index(mon) + 1
    year = 2000 + int(yy)
    if t1 not in TEAM_NAMES or t2 not in TEAM_NAMES:
        return None, set()
    return f"{year}-{month:02d}-{int(dd):02d}", {t1, t2}


def map_kalshi_markets(con) -> dict[str, KalshiMarketInfo]:
    """Join kalshi_markets to games by (date, team pair).

    Primary: event ticker encoding (date + abbrevs, runner-verified format).
    Fallback: title/subtitle text matching. Only confident matches are kept.
    """
    from . import db
    infos: dict[str, KalshiMarketInfo] = {}
    rows = con.execute("SELECT * FROM kalshi_markets").fetchall()
    games = con.execute(
        "SELECT game_id, game_date_et, home_team, away_team, tipoff_utc FROM games").fetchall()
    by_day: dict[str, list] = {}
    for g in games:
        by_day.setdefault(g["game_date_et"], []).append(g)

    def find_game(teams: set, date_et: str | None):
        if len(teams) != 2:
            return None
        cands = []
        if date_et:
            import datetime as _dt
            d = util.parse_iso(date_et + "T00:00:00Z")
            for off in (0, 1, -1):
                dd = (d + _dt.timedelta(days=off)).strftime("%Y-%m-%d")
                cands.extend(by_day.get(dd, []))
                if cands:
                    break
        else:
            cands = [g for day in by_day.values() for g in day]
        for g in cands:
            if {g["home_team"], g["away_team"]} == teams:
                return g
        return None

    for m in rows:
        if not m["ticker"]:
            continue
        date_et, teams = parse_event_ticker(m["event_ticker"] or "")
        if not teams:
            text = f"{m['title'] or ''} {m['subtitle'] or ''}"
            teams = match_teams_in_text(text)
            close = util.parse_iso(m["close_time"])
            if close:
                date_et = util.et_game_date(close)
        if len(teams) != 2:
            continue
        # candidate ET dates to search, best first: ticker encoding > market
        # close time > subtitle '(Jun 13)' (year resolved to plausible seasons)
        cand_dates: list = []
        if date_et:
            cand_dates.append(date_et)
        elif close:
            cand_dates.append(util.et_game_date(close))
        else:
            cand_dates.extend(subtitle_dates(m["subtitle"]))
        g = None
        for cd in cand_dates:
            g = find_game(teams, cd)
            if g:
                break
        if not g and not cand_dates:
            g = find_game(teams, None)  # last resort: unique team-pair scan
        if not g:
            continue
        infos[m["ticker"]] = KalshiMarketInfo(
            ticker=m["ticker"], event_ticker=m["event_ticker"], series_ticker=m["series_ticker"],
            market_type=m["market_type"] or "unknown", game_id=g["game_id"],
            home=g["home_team"], away=g["away_team"],
            strike=json.loads(m["strike_values"] or "{}"), result=m["result"],
            title=m["title"] or "", subtitle=m["subtitle"] or "",
            team=market_team(m["ticker"], m["title"], m["subtitle"]))
    return infos


def alias_game(con, old_game_id: str, new_game_id: str, reason: str) -> None:
    """Record that a game row was superseded (bets keep the original id).

    The bets table is append-only and its trigger ABORTS any game_id rewrite,
    so duplicate-merge repairs must never `UPDATE bets SET game_id`. The
    alias table is the only bridge between a historical bet's game_id and the
    canonical games row (2026-09-21 latent defect: `UPDATE bets` inside
    repair_team_vocab / _merge_espn_game_row would have raised the moment
    the first forward bet existed).
    """
    from . import db, util
    db.insert(con, "game_aliases", {
        "old_game_id": old_game_id, "new_game_id": new_game_id,
        "reason": reason, "created_utc": util.utcnow_iso()}, replace=True)
    db.log_audit(con, "alias", "game-alias", new_game_id,
                 {"old": old_game_id, "reason": reason})


def resolve_game_id(con, game_id: str) -> str:
    """Follow alias chains (max 5 hops) to the canonical game id."""
    cur = game_id
    for _ in range(5):
        row = con.execute("SELECT new_game_id FROM game_aliases WHERE old_game_id=?",
                          (cur,)).fetchone()
        if not row:
            return cur
        cur = row["new_game_id"]
    return cur


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
        self._ask_history: dict[str, list[tuple[str, float]]] = {}
        for r in con.execute(
                "SELECT ticker, captured_utc, yes_ask FROM kalshi_orderbooks "
                "WHERE yes_ask IS NOT NULL ORDER BY captured_utc"):
            self._ask_history.setdefault(r["ticker"], []).append(
                (r["captured_utc"], float(r["yes_ask"])))
            prev = self._books.get(r["ticker"])
            if prev is None or r["captured_utc"] >= prev[0]:
                self._books[r["ticker"]] = (r["captured_utc"], float(r["yes_ask"]))

    def kalshi_price_at(self, ticker: str, decision_iso: str) -> PricePoint | None:
        """Last hourly candle that fully CLOSED strictly before the decision.

        A candle's ts is its OPEN time; its close lands one hour later, so
        using any candle that opens before the decision but closes after it
        would be look-ahead. Require close_time = ts + 60min <= decision.
        """
        cands = self._candles.get(ticker) or []
        dec = util.parse_iso(decision_iso)
        best = None
        import datetime as _dt
        for ts, close in cands:
            ts_dt = util.parse_iso(ts)
            if ts_dt is None:
                continue
            close_time = ts_dt + _dt.timedelta(minutes=60)
            if close_time <= dec:
                best = PricePoint(util.to_iso(close_time), close + 1.0,
                                  "candle_close+1tick")  # 1-tick slippage
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

    def kalshi_ask_at(self, ticker: str, ts_iso: str) -> PricePoint | None:
        """Last observed orderbook ask at or before ts (forward baselines).

        Our own snapshots are timestamped observations, so an as-of lookup is
        look-ahead-safe by construction. Returns None until snapshots exist.
        """
        hist = (getattr(self, "_ask_history", None) or {}).get(ticker) or []
        best = None
        for ts, ask in hist:
            if ts <= ts_iso:
                best = PricePoint(ts, ask, "orderbook_ask")
            else:
                break
        return best


def espn_odds_at(con, game_id: str, decision_iso: str, market: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM odds_snapshots WHERE game_id=? AND market=? AND captured_utc<=? "
        "ORDER BY captured_utc DESC LIMIT 1", (game_id, market, decision_iso)).fetchone()
    return dict(row) if row else None


#: Which side vocabulary is legal per market. Settlement must never be able to
#: interpret a row whose shape is impossible (2026-09-21 adversarial pass:
#: a winner bet carrying side="under" was settled as a LOSS instead of being
#: refused, because settle_score_based() trusted the selection string).
MARKET_SIDES: dict[str, tuple[str, ...]] = {
    "ml": ("home", "away"),
    "kalshi:winner": ("home", "away"),
    "spread": ("home", "away"),
    "kalshi:spread": ("home", "away"),
    "total": ("over", "under"),
    "kalshi:total": ("over", "under"),
    "team_total": ("over", "under"),
    "1h": ("home", "away", "yes", "no"),
    "kalshi:1h": ("yes", "no", "home", "away"),
    "kalshi:1h_winner": ("yes", "no"),
}


def side_vocabulary(market: str) -> tuple[str, ...] | None:
    """Legal `side` values for a market, or None for prop/free-form markets."""
    if market in MARKET_SIDES:
        return MARKET_SIDES[market]
    if "prop" in (market or ""):
        # prop bets are placed as over/under on the strike (paper._prop_signals)
        return ("over", "under", "yes", "no")
    return None


def _shape_field(bet, key, default=None):
    """Read a column from either a dict or an sqlite3.Row (no KeyError)."""
    try:
        val = bet[key]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


def bet_shape(bet) -> tuple[bool, str]:
    """Is this stored bet row internally consistent and settleable?

    Returns (ok, reason). A False answer means the row can never be settled
    honestly, so the caller must flag it instead of interpreting it.
    """
    market = (_shape_field(bet, "market", "") or "")
    side = (_shape_field(bet, "side", "") or "")
    sel = (_shape_field(bet, "selection", "") or "")
    vocab = side_vocabulary(market)
    if vocab is None:
        return False, f"unknown market vocabulary: {market!r}"
    if side.lower() not in vocab:
        return False, f"market {market!r} cannot carry side {side!r} (legal: {list(vocab)})"
    if not sel:
        return False, "empty selection"
    if market in ("total", "kalshi:total"):
        num = sel.split()[-1] if sel.split() else ""
        try:
            float(num)
        except ValueError:
            return False, f"total selection has no numeric strike: {sel!r}"
    if _shape_field(bet, "price") in (None, 0):
        return False, "no price recorded"
    mp = _shape_field(bet, "model_prob")
    if mp is not None and not 0.0 < float(mp) < 1.0:
        return False, f"impossible model probability {mp!r}"
    stake = _shape_field(bet, "stake_usd")
    if stake is not None and float(stake) <= 0:
        return False, f"non-positive stake {stake!r}"
    return True, ""


def settle_score_based(result_type: str, home_score: int, away_score: int,
                       selection: str, line: float | None) -> str:
    """win|loss|push from final score (final scores include OT).

    Uninterpretable input returns 'void' (no P&L) rather than a guessed result:
    a settlement that cannot be justified is not recorded as a win or a loss.
    """
    margin = home_score - away_score
    if result_type == "winner":
        team = selection
        if team not in ("home", "away"):
            return "void"
        return "win" if (margin > 0 and team == "home") or (margin < 0 and team == "away") else "loss"
    if result_type == "spread":
        # selection: 'home -4.5' style; bet on that side covering
        parts = selection.split()
        if len(parts) < 2 or parts[0] not in ("home", "away"):
            return "void"
        try:
            side, num = parts[0], float(parts[1])
        except ValueError:
            return "void"
        covered = margin + num if side == "home" else (-margin) + num
        if covered > 0:
            return "win"
        if covered == 0:
            return "push"
        return "loss"
    if result_type == "total":
        total = home_score + away_score
        if line is None or not (selection.startswith("over")
                                or selection.startswith("under")):
            return "void"
        if total > line:
            return "win" if selection.startswith("over") else "loss"
        if total < line:
            return "win" if selection.startswith("under") else "loss"
        return "push"
    return "void"


def apply_settlement_kalshi(con, info: KalshiMarketInfo, bet: dict, home_score, away_score) -> str:
    """Prefer Kalshi's own recorded result; cross-check with score inference.

    Winner bets are always placed on the HOME-team market: a "home" bet is
    YES (price = home price), an "away" bet is the NO side of that same
    market (priced 100 − home price). The desired resolution therefore
    inverts with bet["side"] — this must be honored for BOTH the recorded
    Kalshi result and the score inference.

    Live shape 2026-09-20 is one market per team, so when info.team names the
    team YES pays on, resolution is computed against THAT team instead of the
    legacy home=YES assumption (which mis-settles away-team markets).
    """
    if info.market_type == "winner":
        side = bet.get("side") or "home"
        if info.team and info.home and info.away:
            bet_team = info.home if side == "home" else info.away
            took_no = (bet_team != info.team)
        else:
            took_no = side == "away"
    else:
        took_no = False  # non-winner markets are bet YES only
        side = "home"
    if info.result in ("yes", "no"):
        want = "no" if took_no else "yes"
        if info.market_type == "winner" and home_score is not None and away_score is not None:
            inferred = settle_score_based("winner", home_score, away_score, side, None)
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
        for k in ("above", "below", "strike", "value", "strike_value",
                  "greater", "less", "over_under", "line", "total_line",
                  "floor_strike", "cap_strike"):
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
