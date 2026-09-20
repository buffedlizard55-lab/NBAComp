"""Seed the research log with the actual research performed during the build
(2026-09-20). Each entry records what was searched, what was verified, and what
was decided — no invented findings."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import db, util  # noqa: E402

ENTRIES = [
    {
        "question": "Which free, keyless public sources can power an NBA betting research pipeline?",
        "sources_searched": "GitHub API (sibling repos: NBAInjuryReport, PriceKalshiHistorical, KalshiPaperSim), docs.kalshi.com, ESPN site API docs via public collectors, stats.nba.com community documentation, paid-archive listings (Scottfree) reviewed and excluded",
        "data_discovered": ("ESPN site API (schedule/scores/current odds, injuries); stats.nba.com (game logs, advanced stats, hustle); "
                            "Kalshi trade-api v2 public market data (markets/orderbooks/candlesticks/trades, keyless); "
                            "data.nba.net fallback. Paid historical odds archives exist (e.g. Scottfree 24,284 NBA rows) — excluded by free-pipeline policy and documented as a limitation."),
        "hypothesis": None,
        "test_performed": "Sandbox network policy test (only GitHub/PyPI reachable); decision: all live collection runs in GitHub Actions; sibling-project runner audits (NBAInjuryReport AUDIT.md) used to pre-verify ESPN reachability patterns.",
        "result": "Pipeline architecture: Actions-based collectors + SQLite + static Pages site.",
        "verification": "Kalshi public endpoints confirmed via docs.kalshi.com quick start + PriceKalshiHistorical collector code; ESPN injury endpoint confirmed via NBAInjuryReport runner audit 2026-09-19 (75 rows/27 team blocks).",
        "decision": "Adopt ESPN + stats.nba.com + Kalshi as core; treat everything else as future research.",
        "next_steps": "Verify Kalshi NBA series list at first runtime discovery; probe candlestick availability for settled markets.",
    },
    {
        "question": "MasterSite review: what can sibling projects contribute?",
        "sources_searched": "https://buffedlizard55-lab.github.io/MasterSite/ (live), GitHub API for member repos",
        "data_discovered": ("44 verified sites. Directly reusable: NBAInjuryReport (ESPN injuries endpoint audits, official NBA injury-report PDF "
                            "discovery — currently 404 for 2026-27; ESPN 403 fingerprint episodes documented), PriceKalshiHistorical "
                            "(Kalshi collector: markets/orderbooks/candles endpoints, rate-limit patterns, SQLite+parquet storage), "
                            "KalshiPaperSim (paper-trading ledger discipline), Commodities (Actions collector committing fills), "
                            "SFWeather (no-invention audit culture). Not reusable: SF local guides, elections, drugs, GEMSDOE."),
        "hypothesis": None,
        "test_performed": "Repo contents read via GitHub API (READMEs, collectors, workflows, audits).",
        "result": "Collector design + source-verification intel adopted; MasterSite entries recorded in sources registry.",
        "verification": "READMEs and audit files read directly from repos (2026-09-20).",
        "decision": "Reuse patterns, not data; keep our collection independent and verified per-run.",
        "next_steps": "Re-check MasterSite audit ledger for new irregularities each pass.",
    },
    {
        "question": "Can historical NBA betting prices be obtained for free and honestly?",
        "sources_searched": "GitHub (sports-odds-datasets by ParlayAPI — samples only: Super Bowl LX, one MLB day, prop closes; not NBA seasons), Scottfree (paid), SBR archive pages (not verifiable from sandbox), Kalshi candlesticks for settled NBA markets",
        "data_discovered": ("No free full-archive of NBA sportsbook closing lines was verifiable. Kalshi public candlesticks may provide real "
                            "price history for the NBA game-winner markets from their launch onward — availability probed at runtime, not assumed."),
        "hypothesis": "Strategies on game-winner markets can be honestly backtested where Kalshi candle history exists.",
        "test_performed": "Designed backfill job: fetch settled KXNBAGAME markets + candlesticks; record availability per window in collection log.",
        "result": "Pending first Actions run (sandbox cannot reach Kalshi directly — documented).",
        "verification": "Endpoint publicity verified via docs.kalshi.com; data availability NOT yet verified — flagged, not asserted.",
        "decision": "Backtest only against verified price data; where prices are missing, strategies are forward-test-first with explicit labels.",
        "next_steps": "Run kalshi-discovery + candles backfill in CI; record what actually exists.",
    },
    {
        "question": "Which strategy families are testable NOW vs forward-only?",
        "sources_searched": "Internal design review against collected-source capabilities; NBA betting research literature priors (B2B cost ~1.5-3 pts; totals SD ~23; Elo/MOV conversions)",
        "data_discovered": "Winner-market strategies (rest, Elo, line-move, injury, travel, home/away, cross-market) are backtestable where Kalshi candles exist; totals strategies need a line source (Kalshi strikes or ESPN lines — labeled assumptions where absent); prop strategies (rebounds/assists/blowout) need prop series discovery and are forward-first.",
        "hypothesis": "14 strategies registered as v1.0.0 with explicit hypotheses, entry rules, sizing, failure modes, and look-ahead controls.",
        "test_performed": "Each strategy encoded in code with the same evaluator used in backtest and forward engines.",
        "result": "Registry live; empty results until real data lands (by design).",
        "verification": "Unit tests pin pricing/settlement/no-lookahead behavior.",
        "decision": "Competition launches with 14 strategies; lineage tracking enabled for future versions.",
        "next_steps": "First backtest after candle backfill; first forward bets when 2026-27 prices exist.",
    },
]


def main():
    with db.get_db() as con:
        n = con.execute("SELECT COUNT(*) c FROM research_log").fetchone()["c"]
        if n:
            print(f"research log already has {n} entries; skipping seed")
            return
        for e in ENTRIES:
            db.insert(con, "research_log", {
                "ts_utc": util.utcnow_iso(), **{k: e.get(k) for k in (
                    "question", "sources_searched", "data_discovered", "hypothesis",
                    "test_performed", "result", "verification", "decision", "next_steps")}})
        print(f"seeded {len(ENTRIES)} research entries")


if __name__ == "__main__":
    main()
