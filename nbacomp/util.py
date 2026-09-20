"""Shared utilities: odds math, probability math, time handling, fees, hashing.

No network access, no I/O side effects. Pure functions only so they are unit
testable. All timestamps are UTC ISO-8601 strings unless noted.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------- time utils

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str | None) -> datetime | None:
    """Parse ISO-8601 timestamps with Z or offset. Returns aware UTC datetime."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def et_game_date(dt: datetime) -> str:
    """NBA 'game date' as used by the league (America/New_York calendar day).

    Uses a fixed UTC-5 offset (EST). During EDT the true local offset is -4,
    which would only shift games that start 8pm ET or later into the *same*
    calendar day either way; an hour offset cannot change the ET date for any
    real tipoff time (earliest 11:00 ET, latest ~02:30 ET next day would only
    be affected if it crossed midnight, which only late PT games do, and those
    are still labelled by ESPN with their local calendar date). We follow the
    UTC date provided by our sources and only use this for schedule logic,
    where an off-by-one-day on a 2am ET game is immaterial to B2B detection
    (documented limitation)."""
    return (dt - timedelta(hours=5)).strftime("%Y-%m-%d")


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


# ------------------------------------------------------------- odds math

def american_to_decimal(odds: int | float) -> float:
    o = float(odds)
    if o > 0:
        return 1.0 + o / 100.0
    if o < 0:
        return 1.0 + 100.0 / abs(o)
    raise ValueError("american odds cannot be 0")


def decimal_to_american(dec: float) -> int:
    if dec <= 1.0:
        raise ValueError("decimal odds must be > 1")
    if dec >= 2.0:
        return int(round((dec - 1.0) * 100.0))
    return int(round(-100.0 / (dec - 1.0)))


def american_to_prob(odds: int | float) -> float:
    """Raw implied probability (includes vig)."""
    return 1.0 / american_to_decimal(odds)


def decimal_to_prob(dec: float) -> float:
    return 1.0 / dec


def prob_to_fair_decimal(p: float) -> float:
    if not 0.0 < p < 1.0:
        raise ValueError("probability must be in (0,1)")
    return 1.0 / p


def cents_to_prob(cents: float) -> float:
    """Kalshi contract price in cents (1..99) -> probability."""
    p = cents / 100.0
    if not 0.0 < p < 1.0:
        raise ValueError("kalshi price must be 1..99 cents")
    return p


def devig_two_way(p_home_vig: float, p_away_vig: float) -> tuple[float, float]:
    """Proportional (multiplicative) vig removal for a two-outcome market."""
    s = p_home_vig + p_away_vig
    if s <= 0:
        raise ValueError("sum of implied probabilities must be > 0")
    return p_home_vig / s, p_away_vig / s


# ------------------------------------------------------- probability math

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def spread_win_prob(margin_mean: float, spread: float, sd: float = 11.5) -> float:
    """P(home team covers `spread`) given expected margin ~ Normal(margin_mean, sd).

    `spread` is the point spread *charged to the home team* in the conventional
    sign convention (negative = home is favored by that many points). Home
    margin must exceed the number; pushes split 50/50 via continuity handling.
    sd=11.5 is the commonly cited historical SD of NBA margins (documented in
    METHODOLOGY.md; not fitted from our data).
    """
    if sd <= 0:
        raise ValueError("sd must be positive")
    # P(margin + spread > 0) with spread charged (spread<0 means favored)
    z = (margin_mean + spread) / sd
    # push probability near integer spread: approximate with tie-density
    return norm_cdf(z)


def elo_expected(ra: float, rb: float, home_adv: float = 0.0) -> float:
    """Elo win expectancy for side a (with optional home advantage points)."""
    return 1.0 / (1.0 + 10.0 ** ((rb - (ra + home_adv)) / 400.0))


def kelly_fraction(p: float, decimal_odds: float, frac: float = 1.0) -> float:
    """Kelly stake fraction of bankroll. frac<1 for fractional Kelly."""
    if not 0.0 < p < 1.0:
        return 0.0
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f) * frac


# ---------------------------------------------------------------- kalshi

KALSHI_FEE_RATE = 0.07  # Kalshi general trading fee rate (per docs, checked 2026-09-20)


def kalshi_fees_dollars(contracts: int, price_cents: float) -> int:
    """Kalshi trading fee: ceil(0.07 * C * P * (1-P)) dollars, P in dollars."""
    if contracts <= 0:
        return 0
    p = price_cents / 100.0
    raw = KALSHI_FEE_RATE * contracts * p * (1.0 - p)
    return int(math.ceil(round(raw, 6) - 1e-9))


def kalshi_payoff(contracts: int, entry_cents: float, result_win: bool) -> float:
    """Net P&L in dollars for Kalshi-style contracts including entry fees only."""
    entry_dollars = contracts * entry_cents / 100.0
    fee = kalshi_fees_dollars(contracts, entry_cents)
    if result_win:
        return contracts - entry_dollars - fee
    return -entry_dollars - fee


TICK = 1  # Kalshi price tick in cents (contracts > $0.99 trade in 0.1c ticks; NBA prices normally 1c)


def round_to_tick(cents: float, tick: int = TICK) -> int:
    return int(round(cents / tick) * tick)


# --------------------------------------------------------------- misc

def stable_hash(obj) -> str:
    j = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(j.encode()).hexdigest()[:16]


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def moving_mean(xs: list[float]) -> float | None:
    if not xs:
        return None
    return sum(xs) / len(xs)


def std(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
