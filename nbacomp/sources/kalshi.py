"""Kalshi public (keyless) market-data client + NBA series discovery.

Base: https://api.elections.kalshi.com/trade-api/v2 (docs.kalshi.com, checked
2026-09-20: market-data GET endpoints are public/no-auth; fallback mirror host
external-api.kalshi.com). Endpoints used:
  GET /series/{ticker}                 series existence/metadata
  GET /markets?series_ticker=&status=  quotes/volume/OI (paginated cursor)
  GET /events?series_ticker=
  GET /markets/{ticker}/orderbook      live book (yes side; no side derived)
  GET /markets/candlesticks            batch OHLCV, interval 1|60|1440 minutes
  GET /markets/trades                  tape
Rate limit: Kalshi documents a basic tier (~10 rps); we keep <=3 rps.
NBA series tickers are DISCOVERED at runtime (never hard-coded as fact):
we probe a candidate list and record which series actually exist.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import http

BASE = "https://api.elections.kalshi.com/trade-api/v2"
FALLBACK_BASE = "https://external-api.kalshi.com/trade-api/v2"

# Candidate NBA series tickers to probe. Existence is NOT assumed; whatever the
# API confirms is recorded in the source registry / series table.
# (KXNBAPTS/REBS/ASTS/PTSALT added 2026-09-20: prior discovery missed the
# points-props series the registry had already confirmed.)
CANDIDATE_SERIES = [
    "KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBA1H", "KXNBAQ1",
    "KXNBAMVP", "KXNBACHAMP", "KXNBACHAMPS", "KXNBAFINAL",
    "KXNBAPOINT", "KXNBAPT", "KXNBAPTS", "KXNBAPTSALT",
    "KXNBAREB", "KXNBAREBOUND", "KXNBAREBOUNDS", "KXNBAREBS",
    "KXNBAAST", "KXNBAASSIST", "KXNBAASSISTS", "KXNBAASTS",
    "KXNBATHREE", "KXNBATHREES", "KXNBASTL", "KXNBABLK",
    "KXNBADD", "KXNBADBLDBL", "KXNBAPRA", "KXNBATO", "KXNBAPOINTS",
    "KXNBA3PM",
]


def _get(path: str, params: dict | None = None, retries: int = 2) -> http.HttpResult:
    r = http.get(f"{BASE}{path}", params, min_interval=0.35, retries=retries)
    if not r.ok and r.status == 0:  # network failover
        r = http.get(f"{FALLBACK_BASE}{path}", params, min_interval=0.35, retries=1)
    return r


def get_series(ticker: str) -> http.HttpResult:
    return _get(f"/series/{ticker}")


def get_markets(series_ticker: str, status: str | None = None,
                max_pages: int = 20, min_close_ts: int | None = None,
                max_close_ts: int | None = None) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"series_ticker": series_ticker, "limit": 1000}
        if status:
            params["status"] = status
        if min_close_ts:
            params["min_close_ts"] = int(min_close_ts)
        if max_close_ts:
            params["max_close_ts"] = int(max_close_ts)
        if cursor:
            params["cursor"] = cursor
        r = _get("/markets", params)
        if not r.ok:
            break
        js = r.json or {}
        out.extend(js.get("markets") or [])
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


def get_events(series_ticker: str, status: str | None = None,
               max_pages: int = 20) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"series_ticker": series_ticker, "limit": 200}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        r = _get("/events", params)
        if not r.ok:
            break
        js = r.json or {}
        out.extend(js.get("events") or [])
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


def get_markets_by_event(event_ticker: str, max_pages: int = 10) -> list[dict]:
    """All markets of one event (works for settled events, unlike the
    series+status filter, which returned zero rows in runner probes)."""
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"event_ticker": event_ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = _get("/markets", params)
        if not r.ok:
            break
        js = r.json or {}
        out.extend(js.get("markets") or [])
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


def get_candlesticks(tickers: list[str], start_ts_ms: int, end_ts_ms: int,
                     interval: int = 60) -> list[dict]:
    """Batch candlesticks. Returns list of {market_ticker, candlesticks:[...]}.

    Live-API verified 2026-09-20: the endpoint requires `market_tickers` +
    `period_interval` (not `tickers`/`interval` — those 400), and nests rows
    under the `markets` key (not `candlesticks`). Both old mistakes silently
    returned zero rows; fixed and pinned by unit test.
    """
    out: list[dict] = []
    B = 20  # conservative batch size
    for i in range(0, len(tickers), B):
        chunk = tickers[i:i + B]
        r = _get("/markets/candlesticks", {
            "market_tickers": ",".join(chunk),
            "start_ts": int(start_ts_ms // 1000),
            "end_ts": int(end_ts_ms // 1000),
            "period_interval": interval,
        })
        if r.ok:
            out.extend((r.json or {}).get("markets") or [])
    return out


def get_orderbook(ticker: str) -> http.HttpResult:
    return _get(f"/markets/{ticker}/orderbook")


def get_orderbooks(tickers: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        r = _get("/markets/orderbooks", {"tickers": ",".join(chunk)})
        if r.ok:
            out.extend((r.json or {}).get("orderbooks") or [])
    return out


def get_trades(ticker: str | None = None, limit: int = 1000,
               max_pages: int = 5) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"limit": min(limit, 1000)}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        r = _get("/markets/trades", params)
        if not r.ok:
            break
        js = r.json or {}
        out.extend(js.get("trades") or [])
        cursor = js.get("cursor")
        if not cursor:
            break
    return out


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def classify_market(m: dict) -> str:
    """Best-effort classification of a Kalshi market into a market type.

    Based on the market/series title and subtitle text ONLY (never guessed
    numbers). Unknown classes stay 'unknown' — the registry records them.
    """
    series = (m.get("series_ticker") or "").upper()
    title = f"{m.get('title') or ''} {(m.get('subtitle') or '')}".lower()
    if series == "KXNBAGAME":
        return "winner"
    if series == "KXNBASPREAD":
        return "spread"
    if series == "KXNBATOTAL":
        return "total"
    if series == "KXNBA1H":
        return "1h"
    if series == "KXNBAQ1":
        return "q1"
    if series == "KXNBAPTS":
        return "prop:points"
    if series == "KXNBAREB":
        return "prop:rebounds"
    if series == "KXNBAAST":
        return "prop:assists"
    if series == "KXNBAPRA":
        return "prop:pra"
    if series == "KXNBASTL":
        return "prop:steals"
    if series == "KXNBABLK":
        return "prop:blocks"
    if series == "KXNBAMVP":
        return "award:mvp"
    # generic text fallbacks for newly-discovered series
    if "spread" in series:
        return "spread"
    if "total" in series and "point" not in title:
        return "total"
    if "1h" in series or "first half" in title:
        return "1h"
    if "q1" in series or "first quarter" in title:
        return "q1"
    if "game" in series:
        return "winner"
    for k, tag in (("point", "prop:points"), ("pts", "prop:points"),
                   ("reb", "prop:rebounds"), ("ast", "prop:assists"),
                   ("three", "prop:threes"), ("stl", "prop:steals"),
                   ("blk", "prop:blocks"), ("pra", "prop:pra")):
        if k in series:
            return tag
    return "unknown"


def _cents(m: dict, *keys) -> int | None:
    """First present quote as integer cents.

    Live-API verified 2026-09-20: current markets expose dollar strings
    (yes_bid_dollars "0.5400", volume_fp "9922.32") and NO cent integers.
    Legacy cent fields are kept as fallback; unknown stays None (never 0 —
    0 cents would be a fabricated price).
    """
    for k in keys:
        v = m.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        # *_dollars are fractions of $1 (x100 -> cents); legacy fields are
        # already cents. (*_fp volume fields are NOT dollars — see _count.)
        return int(round(f * 100.0)) if k.endswith("_dollars") else int(f)
    return None


def _count(m: dict, *keys) -> int | None:
    """First present volume/OI field as integer contracts (no rescaling)."""
    for k in keys:
        v = m.get(k)
        if v is None or v == "":
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return None


def parse_market(m: dict, captured_utc: str) -> dict:
    st: dict = {}
    for blob_key in ("strike", "custom_strike"):
        blob = m.get(blob_key)
        if isinstance(blob, dict):
            st.update(blob)
    for scalar_key in ("strike_type", "floor_strike", "cap_strike"):
        if m.get(scalar_key) is not None:
            st[scalar_key] = m.get(scalar_key)
    result = m.get("result") or None
    return {
        "ticker": m.get("ticker"),
        "series_ticker": m.get("series_ticker"),
        "event_ticker": m.get("event_ticker"),
        "title": m.get("title"),
        # live shape: yes_sub_title ("San Antonio") / no_sub_title; legacy
        # market_subtitle/subtitle kept as fallback (unit-test pinned).
        "subtitle": (m.get("yes_sub_title") or m.get("market_subtitle")
                     or m.get("subtitle")),
        "market_type": classify_market(m),
        "strike_values": None if not st else
        __import__("json").dumps(st, default=str),
        "status": m.get("status"),
        "close_time": _iso(m.get("close_time")),
        "expected_expiration_time": _iso(m.get("expected_expiration_time")),
        "yes_bid": _cents(m, "yes_bid_dollars", "yes_bid"),
        "yes_ask": _cents(m, "yes_ask_dollars", "yes_ask"),
        "last_price": _cents(m, "last_price_dollars", "last_price"),
        "volume": _cents(m, "volume_fp", "volume"),
        "open_interest": _cents(m, "open_interest_fp", "open_interest"),
        "result": result,
        "settled_time": _iso(m.get("settled_time")),
        "captured_utc": captured_utc,
    }


def _iso(ts) -> str | None:
    if not ts:
        return None
    try:
        v = float(ts)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return str(ts)
