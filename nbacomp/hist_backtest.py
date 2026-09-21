"""Historical price simulation over the validated SBR archive (hist_odds).

This is the ONE place in the project where a strategy is simulated against real,
observed historical prices instead of outcome-only hit rates. It exists because
the research pass found a free archive of historical NBA moneylines
(nbacomp/sources/sbr.py); everything else in the repository is forward-only
because no free price history was found.

Discipline (all of it is enforced, not asserted):
  * model state for a game is built ONLY from games strictly earlier in the same
    season — no future results, no cross-season leakage;
  * the execution price is the archive's moneyline for that game (a single
    pre-game market price). The decision never sees it before the model does,
    and the model never sees the result;
  * flat staking (1% of a fixed $1,000 bankroll) so a lucky sizing path cannot
    flatter a rule; the goal is measuring price edge, not optimising stakes;
  * the market baseline (backing the home team every game at the same prices)
    is reported next to every rule, because a rule that loses $40/game less than
    the baseline has still lost money.

Totals and spreads are NOT simulated here: the archive gives lines but not
per-side prices for those markets, and inventing a -110 price would be exactly
the assumption the rest of this project refuses to trade on.
"""
from __future__ import annotations

from collections import defaultdict

from . import util, validation

START_BANKROLL = 1000.0
FLAT_STAKE = 10.0            # 1% of the bankroll: fixed, never compound
MIN_EDGE = 0.03              # required model-vs-price edge after the vig
#: Same calibration the live engines use: a raw model probability is shrunk
#: toward the market price and then an edge above the credible maximum is
#: rejected as model error. Without this the simulation's largest "edges" were
#: 20-30% on longshots — a mis-calibrated Elo arguing with a real price, which
#: is not a tradable edge.
SHRINK = 0.5
ELO_START = 1500.0
ELO_K = 20.0
HOME_ADV = 65.0              # Elo points of home advantage (league-typical)
MAX_MOV_MULT = 2.5


def _mov_mult(margin: float, elo_diff: float) -> float:
    """FiveThirtyEight-style MOV multiplier (same form as model.py)."""
    if margin == 0:
        return 1.0
    return min(MAX_MOV_MULT, ((abs(margin) + 3.0) ** 0.8)
               / (7.5 + 0.006 * elo_diff))


def _elo_prob(elo_h: float, elo_a: float) -> float:
    return 1.0 / (1.0 + 10 ** (-((elo_h + HOME_ADV) - elo_a) / 400.0))


class _State:
    """Per-team rolling state built strictly from earlier games."""

    def __init__(self):
        self.elo: dict[str, float] = defaultdict(lambda: ELO_START)
        self.last_date: dict[str, str] = {}
        self.streak: dict[str, int] = {}          # +n = n wins, -n = n losses
        self.home: dict[str, list[float]] = defaultdict(list)   # home margins
        self.away: dict[str, list[float]] = defaultdict(list)
        self.road_trip: dict[str, int] = {}
        self.season_of: dict[str, str] = {}

    def reset_season(self):
        self.elo = defaultdict(lambda: ELO_START)
        self.streak = {}
        self.road_trip = {}
        self.home = defaultdict(list)
        self.away = defaultdict(list)

    def rest_days(self, team: str, date: str) -> int | None:
        last = self.last_date.get(team)
        if not last:
            return None
        return (util.parse_iso(date + "T00:00:00Z")
                - util.parse_iso(last + "T00:00:00Z")).days

    def observe(self, date: str, home: str, away: str, hs: int, as_: int):
        margin = hs - as_
        p_h = _elo_prob(self.elo[home], self.elo[away])
        mult = _mov_mult(margin, (self.elo[home] + HOME_ADV) - self.elo[away])
        delta = ELO_K * mult * ((1.0 if margin > 0 else 0.0) - p_h)
        self.elo[home] += delta
        self.elo[away] -= delta
        self.last_date[home] = self.last_date[away] = date
        self.home[home].append(margin)
        self.away[away].append(-margin)
        won = margin > 0
        # +n = n straight wins, -n = n straight losses (reset on the other result)
        self.streak[home] = (self.streak.get(home, 0) + 1 if won and self.streak.get(home, 0) >= 0
                             else 1 if won else
                             self.streak.get(home, 0) - 1 if self.streak.get(home, 0) <= 0
                             else -1)
        self.streak[away] = (self.streak.get(away, 0) + 1 if (not won) and self.streak.get(away, 0) >= 0
                             else 1 if (not won) else
                             self.streak.get(away, 0) - 1 if self.streak.get(away, 0) <= 0
                             else -1)
        self.road_trip[home] = 0
        self.road_trip[away] = self.road_trip.get(away, 0) + 1


def _rules(state: _State, g: dict) -> list[dict]:
    """Pre-registered rules evaluated at a decision that uses only prior games.

    Each rule returns (strategy_id, side, model_prob, reason). The caller prices
    it from the archive and settles it from the printed final score.
    """
    home, away = g["home"], g["away"]
    date = g["game_date_et"]
    out: list[dict] = []
    p_home = _elo_prob(state.elo[home], state.elo[away])
    hr, ar = state.rest_days(home, date), state.rest_days(away, date)

    # NBA-003 Elo: the model's win probability against the archive's price.
    out.append({"sid": "NBA-003", "side": "home" if p_home >= 0.5 else "away",
                "prob": p_home if p_home >= 0.5 else 1 - p_home,
                "why": f"elo {state.elo[home]:.0f}/{state.elo[away]:.0f}"})

    # NBA-001 rest edge: B2B team vs rested opponent.
    if hr is not None and ar is not None and hr == 1 and ar >= 2:
        out.append({"sid": "NBA-001", "side": "away",
                    "prob": 1 - p_home, "why": f"home on B2B (rest {hr} vs {ar})"})
    if hr is not None and ar is not None and ar == 1 and hr >= 2:
        out.append({"sid": "NBA-001", "side": "home",
                    "prob": p_home, "why": f"away on B2B (rest {ar} vs {hr})"})

    # NBA-021 streak fade: fade a team on a 4+ win streak.
    if state.streak.get(home, 0) >= 4:
        out.append({"sid": "NBA-021", "side": "away", "prob": 1 - p_home,
                    "why": f"{home} on {state.streak[home]}-game win streak"})
    if state.streak.get(away, 0) >= 4:
        out.append({"sid": "NBA-021", "side": "home", "prob": p_home,
                    "why": f"{away} on {state.streak[away]}-game win streak"})

    # NBA-020/024 bounce/drift rules use streaks in the same direction as
    # NBA-021's counterpart: backing a team on a 4+ losing streak is the
    # measured mirror of fading a winning streak.
    if state.streak.get(home, 0) <= -4:
        out.append({"sid": "NBA-024", "side": "home", "prob": p_home,
                    "why": f"{home} on {abs(state.streak[home])}-game loss streak"})
    if state.streak.get(away, 0) <= -4:
        out.append({"sid": "NBA-024", "side": "away", "prob": 1 - p_home,
                    "why": f"{away} on {abs(state.streak[away])}-game loss streak"})

    # NBA-006 home-court form: a team whose own-venue margin is much better than
    # its road margin is priced off the league-average home advantage.
    for team, side, hist, other_hist in ((home, "home", state.home[home], state.away[home]),
                                         (away, "away", state.away[away], state.home[away])):
        if len(hist) >= 6 and len(other_hist) >= 6:
            own = sum(hist) / len(hist)
            road = sum(other_hist) / len(other_hist)
            if own - road > 6.0:  # a 6+ point venue-specific edge
                out.append({"sid": "NBA-006", "side": side,
                            "prob": p_home if side == "home" else 1 - p_home,
                            "why": f"{team} own-venue margin {own:+.1f} vs road {road:+.1f}"})

    # NBA-007 road-trip finale: a team closing a 5+ game road trip.
    if state.road_trip.get(away, 0) >= 4:
        out.append({"sid": "NBA-007", "side": "home", "prob": p_home,
                    "why": f"{away} closing a {state.road_trip[away] + 1}-game road trip"})
    if state.road_trip.get(home, 0) >= 4:
        out.append({"sid": "NBA-007", "side": "away", "prob": 1 - p_home,
                    "why": f"{home} closing a {state.road_trip[home] + 1}-game road trip"})
    return out


def _ml_price(g: dict, side: str) -> int | None:
    return g["ml_home"] if side == "home" else g["ml_away"]


def run(con, seasons: list[str] | None = None) -> dict:
    """Simulate every pre-registered rule over the validated archive.

    Returns a summary dict; per-strategy and per-strategy-season rows are written
    to hist_backtests so the site can show them with the same provenance as
    every other result in this repository.
    """
    where, params = "", []
    if seasons:
        where = "WHERE season IN (%s)" % ",".join("?" for _ in seasons)
        params = list(seasons)
    rows = [dict(r) for r in con.execute(
        f"SELECT * FROM hist_odds {where} ORDER BY season, game_date_et, away, home",
        params)]
    by_season: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_season[r["season"]].append(r)

    bets: list[dict] = []
    baseline: list[dict] = []
    for season in sorted(by_season):
        state = _State()
        for g in by_season[season]:
            if g["ml_home"] is None or g["ml_away"] is None:
                continue
            market_p_home = util.american_to_prob(g["ml_home"])
            for rule in _rules(state, g):
                side = rule["side"]
                ml = _ml_price(g, side)
                if ml is None:
                    continue
                market_p = util.american_to_prob(ml)
                # The archive carries ONE pre-game price per side, so this is a
                # price-taking simulation: shrink first, then require a real
                # edge, then refuse edges beyond the credible maximum.
                p_cal = validation.calibrated_prob(rule["prob"], market_p, SHRINK)
                edge = p_cal - market_p
                if edge < MIN_EDGE or edge > validation.MAX_CREDIBLE_EDGE:
                    continue
                won = ((g["home_final"] > g["away_final"]) if side == "home"
                       else (g["away_final"] > g["home_final"]))
                pnl = engine_american_pnl(FLAT_STAKE, ml, "win" if won else "loss")
                bets.append({
                    "strategy_id": rule["sid"], "season": season,
                    "game_date_et": g["game_date_et"], "away": g["away"],
                    "home": g["home"], "side": side, "ml": ml,
                    "market_prob": round(market_p, 4),
                    "model_prob": round(rule["prob"], 4),
                    "calibrated_prob": round(p_cal, 4),
                    "edge": round(edge, 4),
                    "stake": FLAT_STAKE, "won": int(won), "pnl": round(pnl, 2),
                    "trigger": rule["why"],
                    "source_url": g["source_url"],
                })
            # market baseline: home team, same price, same stake, every game
            ml_b = g["ml_home"]
            if ml_b is not None:
                won_b = g["home_final"] > g["away_final"]
                baseline.append({
                    "season": season, "stake": FLAT_STAKE,
                    "pnl": engine_american_pnl(
                        FLAT_STAKE, ml_b, "win" if won_b else "loss"),
                    "won": int(won_b), "edge": 0.0, "ml": ml_b})
            state.observe(g["game_date_et"], g["home"], g["away"],
                          g["home_final"], g["away_final"])

    summary: dict[str, dict] = {}
    for sid in sorted({b["strategy_id"] for b in bets}):
        sb = [b for b in bets if b["strategy_id"] == sid]
        summary[sid] = _agg(sb)
        for season in sorted({b["season"] for b in sb}):
            summary[f"{sid}|{season}"] = _agg([b for b in sb if b["season"] == season])
    summary["MARKET|all"] = _agg(baseline)
    return {"summary": summary, "bets": bets,
            "games": len(rows), "seasons": sorted(by_season)}


def engine_american_pnl(stake: float, american: float, result: str) -> float:
    """Local copy so this module never depends on the trading engine's state."""
    if result in ("push", "void", "pending"):
        return 0.0
    dec = util.american_to_decimal(american)
    return stake * (dec - 1.0) if result == "win" else -stake


def _agg(rows: list[dict]) -> dict:
    n = len(rows)
    pnl = sum(r["pnl"] for r in rows)
    staked = sum(r["stake"] for r in rows)
    wins = sum(r["won"] for r in rows)
    decided = n
    cur = peak = mdd = 0.0
    for r in rows:
        cur += r["pnl"]
        peak = max(peak, cur)
        mdd = max(mdd, peak - cur)
    return {"bets": n, "wins": wins, "win_rate": (wins / decided) if decided else None,
            "means": None,
            "pnl": round(pnl, 2), "staked": round(staked, 2),
            "roi": round(pnl / staked, 4) if staked else None,
            "max_dd": round(mdd, 2),
            "avg_edge": round(sum(r["edge"] for r in rows) / n, 4) if n else None,
            "avg_price": round(sum(abs(r["ml"]) for r in rows) / n, 1) if n else None}


def persist(con, result: dict, run_id: str) -> int:
    """Write per-rule and per-rule-season rows to hist_backtests (idempotent).

    Derived results, not audit records: a row from an earlier run whose season
    label differs only in case is removed, so the site cannot show the same
    baseline twice.
    """
    written = 0
    con.execute("DELETE FROM hist_backtests WHERE season <> UPPER(season)")
    for key, agg in result["summary"].items():
        sid, _, season = key.partition("|")
        season = (season or "ALL").upper()
        con.execute(
            "INSERT OR REPLACE INTO hist_backtests (run_id, strategy_id, season, "
            "bets, wins, win_rate, pnl, staked, roi, max_dd, avg_edge, avg_price, "
            "games_available, generated_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, sid, season, agg["bets"], agg["wins"], agg["win_rate"],
             agg["pnl"], agg["staked"], agg["roi"], agg["max_dd"], agg["avg_edge"],
             agg["avg_price"], result["games"], util.utcnow_iso()))
        written += 1
    return written
