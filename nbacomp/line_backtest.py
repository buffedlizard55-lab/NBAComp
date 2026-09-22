"""Line-based historical validation over the validated SBR archive.

The project has three evidence tracks and they are never mixed:

1. ``signal_backtest``  — decision rule vs verified OUTCOMES. No prices at all.
2. ``hist_backtest``    — real PRICES (the archive's moneylines) -> real P&L.
3. ``line_backtest``    — this module: the archive's OBSERVED opening/closing
   SPREADS and TOTALS. Those are lines, not prices: the archive prints no
   per-side price for a spread or total bet, so **no P&L is computed here and
   none is claimed.** What a line does allow, without inventing anything, is
   the two questions a totals/ATS rule actually lives or dies on:

   * does the model's expected margin beat the market's own closing spread?
     (measured as a cover rate — the fraction of firings where the picked side
     covered the observed line);
   * is the model's total closer to the real total than the market's line is?
     (measured as mean absolute error against the verified total).

A cover rate is a frequency, not a profit. Where an economic threshold is
quoted it is derived, not assumed: ``BREAKEVEN_AMERICAN`` prices the standard
-110 juice (``util.american_to_prob(-110)`` = 52.38%) and every row that quotes
it says so in its own ``detail`` text. The live engines trade totals/spreads at
-110 as a labeled assumption (``PRICED-ASSUMPTION`` + the ``price-not-observed``
quarantine flag); this track never converts that assumption into a P&L figure.

Look-ahead discipline is the same as the priced track and is enforced the same
way: per-season state (Elo, rest, streaks) is built strictly from games earlier
in that season, and the line used for a game is the line the archive prints for
that game.
"""
from __future__ import annotations

from collections import defaultdict

from . import util, validation
from .backtest import SPREAD_MARGIN_SD, SPREAD_MIN_COVER
from .hist_backtest import HOME_ADV, _State

#: The juice the live engines assume for spread/total markets. Quoted as a
#: breakeven frequency only — never as a simulated fill price.
BREAKEVEN_AMERICAN = -110

#: Opening→closing spread move (points) that counts as "the line moved" for the
#: momentum test. Half a point is the archive's own resolution: the printed
#: lines are halves, so a smaller move cannot be observed.
LINE_MOVE_MIN = 0.5

#: Minimum decided firings before a cover rate is treated as usable evidence
#: (same floor the outcome-only track uses).
MIN_N = validation.MIN_N_OUTCOME


def breakeven_prob(american: int = BREAKEVEN_AMERICAN) -> float:
    """Win frequency a bet must reach to break even at the quoted juice."""
    return util.american_to_prob(american)


# ------------------------------------------------------------------ metrics

class _Acc:
    """Accumulates one metric's observations, then aggregates them."""

    __slots__ = ("vals", "hits", "n")

    def __init__(self):
        self.vals: list[float] = []
        self.hits = 0
        self.n = 0

    def rate(self, hit: bool):
        self.n += 1
        self.hits += 1 if hit else 0

    def value(self, v: float):
        self.vals.append(float(v))

    def mean(self) -> float | None:
        return sum(self.vals) / len(self.vals) if self.vals else None


def _z(p_hat: float, n: int, p0: float) -> float | None:
    """Two-sided z of an observed frequency against a reference frequency."""
    if n <= 0 or not (0.0 < p0 < 1.0):
        return None
    se = (p0 * (1.0 - p0) / n) ** 0.5
    return (p_hat - p0) / se if se > 0 else None


def _t(diffs: list[float]) -> float | None:
    """Paired t statistic of a list of per-game differences (mean / SE)."""
    n = len(diffs)
    if n < 2:
        return None
    m = sum(diffs) / n
    sd = util.std(diffs)
    if not sd:
        return None
    return m / (sd / n ** 0.5)


# -------------------------------------------------------------------- core

def _ats_outcome(home_margin: float, home_spread: float, side: str) -> bool | None:
    """Did `side` cover the observed home spread? None = push (excluded)."""
    cover = home_margin + home_spread
    if cover == 0:
        return None
    return cover > 0 if side == "home" else cover < 0


def run(con, seasons: list[str] | None = None) -> dict:
    """Measure every line-based rule over the validated archive.

    Returns ``{"summary": {...}, "games": n, "seasons": [...], "firings": {...}}``
    where ``summary`` maps ``"<rule>|<season|ALL>"`` to a metric row.
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

    # (rule, metric, season) -> accumulator; 'ALL' is accumulated in parallel.
    acc: dict[tuple[str, str, str], _Acc] = defaultdict(_Acc)
    # paired per-game differences for the open-vs-close MAE comparison
    paired: dict[tuple[str, str], list[float]] = defaultdict(list)
    firings: dict[str, list[dict]] = defaultdict(list)

    def A(rule: str, metric: str, season: str) -> tuple[_Acc, _Acc]:
        return acc[(rule, metric, season)], acc[(rule, metric, "ALL")]

    for season in sorted(by_season):
        state = _State()
        for g in by_season[season]:
            hs, as_ = g["home_final"], g["away_final"]
            margin = hs - as_
            total = hs + as_
            o_sp, c_sp = g["open_home_spread"], g["close_home_spread"]
            o_tot, c_tot = g["open_total"], g["close_total"]
            home, away = g["home"], g["away"]

            # --- model margin BEFORE this game is observed -------------------
            elo_margin = (((state.elo[home] + HOME_ADV) - state.elo[away])
                          / 28.0)  # backtest.ELO_PER_POINT

            # --- NBA-026: Elo margin vs the archive's closing spread --------
            cover = elo_margin + c_sp
            if abs(cover) >= SPREAD_MIN_COVER and \
                    util.norm_cdf(abs(cover) / SPREAD_MARGIN_SD) - 0.524 >= 0.02:
                side = "home" if cover > 0 else "away"
                hit = _ats_outcome(margin, c_sp, side)
                if hit is not None:
                    for a in A("NBA-026", "cover_rate", season):
                        a.rate(hit)
                    firings["NBA-026"].append({
                        "season": season, "game_date_et": g["game_date_et"],
                        "away": away, "home": home, "side": side,
                        "elo_margin": round(elo_margin, 2),
                        "close_home_spread": c_sp, "expected_cover": round(cover, 2),
                        "actual_margin": margin, "covered": int(hit),
                        "trigger": (f"elo margin {elo_margin:+.1f} vs close spread "
                                    f"{c_sp:+g} (cover {cover:+.1f})"),
                        "source_url": g["source_url"]})

            # --- NBA-004: does a line move continue? (spread-move analogue) --
            move = c_sp - o_sp
            if abs(move) >= LINE_MOVE_MIN:
                # line moved toward home when the home spread got more negative
                side = "home" if move < 0 else "away"
                hit = _ats_outcome(margin, c_sp, side)
                if hit is not None:
                    for a in A("NBA-004", "cover_rate", season):
                        a.rate(hit)
                    firings["NBA-004"].append({
                        "season": season, "game_date_et": g["game_date_et"],
                        "away": away, "home": home, "side": side,
                        "open_home_spread": o_sp, "close_home_spread": c_sp,
                        "move": round(move, 2), "actual_margin": margin,
                        "covered": int(hit),
                        "trigger": (f"spread moved {move:+g} "
                                    f"({o_sp:+g} -> {c_sp:+g}); back {side}"),
                        "source_url": g["source_url"]})

            # --- MARKET baselines: home ATS, and the lines' own accuracy -----
            hit_home = _ats_outcome(margin, c_sp, "home")
            if hit_home is not None:
                for a in A("MARKET", "home_ats_cover_rate", season):
                    a.rate(hit_home)
            for metric, line in (("margin_mae_open", o_sp), ("margin_mae_close", c_sp)):
                if line is not None:
                    for a in A("MARKET", metric, season):
                        a.value(abs(margin - line))
            for metric, line in (("total_mae_open", o_tot), ("total_mae_close", c_tot)):
                if line is not None:
                    for a in A("MARKET", metric, season):
                        a.value(abs(total - line))
            if o_sp is not None and c_sp is not None:
                paired[("margin", season)].append(abs(margin - o_sp) - abs(margin - c_sp))
                paired[("margin", "ALL")].append(abs(margin - o_sp) - abs(margin - c_sp))
            if o_tot is not None and c_tot is not None:
                paired[("total", season)].append(abs(total - o_tot) - abs(total - c_tot))
                paired[("total", "ALL")].append(abs(total - o_tot) - abs(total - c_tot))

            state.observe(g["game_date_et"], home, away, hs, as_)

    return {"summary": _summarise(acc, paired), "firings": dict(firings),
            "games": len(rows), "seasons": sorted(by_season),
            "breakeven": breakeven_prob()}


def _summarise(acc: dict, paired: dict) -> dict[str, dict]:
    """Aggregate accumulators into one row per (rule, season, metric).

    Key shape: ``"<rule>|<season>"`` for a rule's cover rate (a rule has one)
    and ``"<rule>|<season>|<metric>"`` for everything else, so the MARKET
    baseline row a rule is quoted against is always ``MARKET|<season>``.
    """
    out: dict[str, dict] = {}
    be = breakeven_prob()
    for (rule, metric, season), a in acc.items():
        if metric.endswith("cover_rate"):
            if not a.n:
                continue
            rate = a.hits / a.n
            key = f"{rule}|{season}"
            subject = ("the home team covered the observed closing spread"
                       if metric == "home_ats_cover_rate"
                       else "decided firings covered the observed line")
            out[key] = {
                "rule_id": rule, "season": season, "metric": metric, "n": a.n,
                "value": round(rate, 4), "baseline": None, "breakeven": round(be, 4),
                "z": _round(_z(rate, a.n, be)),
                "detail": (f"{a.hits}/{a.n} games: {subject}; {be:.2%} is the "
                           f"break-even frequency at standard -110 juice (a "
                           f"reference, not a simulated price)"),
            }
            continue
        m = a.mean()
        if m is None:
            continue
        out[f"{rule}|{season}|{metric}"] = {
            "rule_id": rule, "season": season, "metric": metric, "n": len(a.vals),
            "value": round(m, 4), "baseline": None, "breakeven": None, "z": None,
            "detail": f"mean |observed result − observed line| over {len(a.vals)} games",
        }
    # paired open-vs-close comparisons (the shrinkage policy's evidence)
    for (kind, season), diffs in paired.items():
        if not diffs:
            continue
        m = sum(diffs) / len(diffs)
        t = _t(diffs)
        better = ("neither line is better (identical errors)" if m == 0
                  else "the opening line is the better predictor of the observed result"
                  if m < 0 else
                  "the closing line is the better predictor of the observed result")
        t_txt = (f"paired t={t:.2f}" if t is not None
                 else "paired t undefined: every game's difference is identical")
        out[f"MARKET|{season}|paired_{kind}_mae"] = {
            "rule_id": "MARKET", "season": season,
            "metric": f"paired_{kind}_mae_open_minus_close", "n": len(diffs),
            "value": round(m, 4), "baseline": 0.0, "breakeven": None,
            "z": _round(t),
            "detail": (f"per-game (open MAE − close MAE) on {kind}: {m:+.3f} pts "
                       f"({t_txt} on {len(diffs)} games) — {better}"),
        }
    # every ATS rule is quoted next to the same-season market baseline
    for row in out.values():
        if row["metric"].endswith("cover_rate") and row["rule_id"] != "MARKET":
            base = out.get(f"MARKET|{row['season']}")
            if base:
                row["baseline"] = base["value"]
                row["detail"] += (f"; market baseline (home ATS every game, same "
                                  f"season) covered {base['value']:.2%}")
    return out


def _round(x: float | None, nd: int = 2) -> float | None:
    return round(x, nd) if x is not None else None


# ----------------------------------------------------------------- storage

def persist(con, result: dict, run_id: str) -> int:
    """Write one row per (rule, season, metric). Idempotent per run_id."""
    con.execute("DELETE FROM line_backtests WHERE run_id=?", (run_id,))
    written = 0
    for row in result["summary"].values():
        con.execute(
            "INSERT OR REPLACE INTO line_backtests (run_id, rule_id, season, metric, "
            "n, value, baseline, breakeven, z, detail, generated_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, row["rule_id"], row["season"], row["metric"], row["n"],
             row["value"], row["baseline"], row["breakeven"], row["z"],
             row["detail"], util.utcnow_iso()))
        written += 1
    return written


def latest_run(con) -> str | None:
    row = con.execute("SELECT run_id FROM line_backtests ORDER BY generated_utc "
                      "DESC, run_id DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


def pooled(con, rule_id: str, metric: str, run_id: str | None = None) -> dict | None:
    """The pooled (season='ALL') row for one rule+metric, if the track has it."""
    run_id = run_id or latest_run(con)
    if not run_id:
        return None
    row = con.execute(
        "SELECT * FROM line_backtests WHERE run_id=? AND rule_id=? AND metric=? "
        "AND season='ALL'", (run_id, rule_id, metric)).fetchone()
    return dict(row) if row else None
