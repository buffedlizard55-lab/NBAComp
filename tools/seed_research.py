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
    {
        "question": "Which transports and endpoints ACTUALLY work from GitHub Actions runners?",
        "sources_searched": "Live probes from ubuntu-latest runners (committed to data/diagnostics.txt): site.api.espn.com, site.web.api.espn.com, sports.core.api.espn.com, stats.nba.com, data.nba.net, cdn.nba.com, basketball-reference.com, Kalshi trade-api v2; transport variants: python-urllib, curl, Node fetch",
        "data_discovered": ("ESPN site.api: Akamai 403 for runner TLS fingerprints (python AND curl) but 200 via Node fetch; "
                            "site.web.api.espn.com: 200 via plain HTTP for scoreboard (any historical date), injuries (860KB), teams, summary (full box scores). "
                            "stats.nba.com: connection tarpit/timeout from runners in ALL transports (urllib, curl, Node); data.nba.net dead (0 bytes); cdn.nba.com liveData 403. "
                            "basketball-reference: 200. Kalshi: 200 keyless; settled history via /events?status=settled pagination (NOT /markets?status=settled which returns 0); "
                            "NBA series confirmed: KXNBAGAME/KXNBASPREAD/KXNBATOTAL/KXNBA1H/KXNBAPTS/KXNBAREB/KXNBAAST/KXNBAPRA/KXNBASTL/KXNBABLK/KXNBAMVP."),
        "hypothesis": "A fully keyless, free core pipeline is possible using site.web ESPN + BBR + Kalshi.",
        "test_performed": "Iterated 5 committed probe runs from Actions; each result recorded in-repo (data/diagnostics.txt history).",
        "result": "Pipeline re-architected to verified endpoints; stats.nba.com replaced by ESPN box scores (same modeled fields) + BBR independent verification.",
        "verification": "Every claim above comes from committed probe output with HTTP status + byte counts (see diagnostics history in git).",
        "decision": "Freeze this endpoint matrix in code + tests; re-probe nightly so drift is detected and recorded.",
        "next_steps": "Backfill two seasons of ESPN results, BBR verification, Kalshi settled events + candlesticks, and box scores; then run first real backtests.",
    },
    {
        "question": "probe5 (Actions run): what do settled Kalshi NBA events actually expose?",
        "sources_searched": "Kalshi trade-api v2 /events?series_ticker=KXNBAGAME&status=settled (cursor pagination), via Actions runner (data/diagnostics.txt probe5 2026-09-20T19:54Z)",
        "data_discovered": ("200 settled events/page with cursor; 1448+ settled KXNBAGAME events over 8 pages (2024-25 and 2025-26 seasons). "
                            "Settled event rows carry title ('Game 5: New York at San Antonio') and sub_title ('NYK at SAS (Jun 13)') but NO "
                            "ticker or close_time keys; the identity is the event_ticker field (KXNBAGAME-26JUN13NYKSAS format). "
                            "Per-event markets and 400-day candle history were NOT reached (probe crashed on the wrong key) — still unverified."),
        "hypothesis": "Historical game-winner prices (candlesticks) exist deep enough to honestly backtest strategies.",
        "test_performed": "Runner probe walked 8 pages of settled events; market/candle probe pending next run (probe fixed to use event_ticker).",
        "result": "UNVERIFIED for market-level history; event-level history CONFIRMED. Collectors re-written around event_ticker identity.",
        "verification": "data/diagnostics.txt probe5 block (committed by Actions run for f154a0b).",
        "decision": "kalshi_backfill uses event_ticker + subtitle '(Mon DD)' date resolution; dates never guessed beyond plausible season years.",
        "next_steps": "Next Actions run: dump one settled event's markets (result/strike fields) + 400d candles; then decide backtest depth vs forward-test.",
    },
    {
        "question": "Adversarial review pass 2 (2026-09-20): what breaks under hostile inspection?",
        "sources_searched": "Local code review: engine.py, backtest.py, paper.py, audit.py, db.py, collect.py, sources/*, workflows",
        "data_discovered": ("CRITICAL: Kelly sizing was passed the model's FAIR decimal odds (1/p) as payout — Kelly f* is identically zero, so NO bet "
                            "was ever placed anywhere (backtest+paper). Fixed to paid odds (100/price_cents). CRITICAL: winner settlement was "
                            "side-blind — away bets (NO side of the home market) settled from the raw Kalshi result as if YES; fixed + regression "
                            "tests. HIGH: forward engine re-bet the same game/strategy every collection run (bet ids embed the decision "
                            "timestamp) — fixed with a one-position-per-strategy/game/market/selection rule. Also: partially-open hourly candles "
                            "excluded from decisions (look-ahead); bet rows made append-only via SQLite trigger; ESPN divergence signals ignore "
                            "snapshots older than 24h; NBA-012 cross-market divergence re-checked; workflow bref verification was missing May 2025."),
        "hypothesis": "The pipeline would have produced plausible-looking output despite producing zero bets.",
        "test_performed": "38 automated tests incl. new regressions: candle-closure guard, side-aware settlement, kelly paid-odds, dedup, append-only trigger, probe5 subtitle mapping.",
        "result": "4 significant defects found and fixed before any data run; all tests green.",
        "verification": "pytest 38 passed locally on arena/01a0c02a-nbacomp at commit c1b972c+.",
        "decision": "Backtest/paper P&L is now trustworthy-by-construction at the sizing/settlement level; remaining risk is data availability, not code.",
        "next_steps": "Pass 3: line-by-line requirements review against the full competition spec.",
    },
    {
        "question": "Pass 3 — line-by-line requirements review against the original prompt?",
        "sources_searched": "Local code review against the 66-section competition prompt; gap analysis; categories not yet covered: starting lineups (#7), rotation/minutes (#8), rebounds-only (#15), assists-only (#16), shot-profile numeric (#13), live betting (#27), overtime (#28), referee (#23 — explicitly excluded), coaching (#24).",
        "data_discovered": ("Coverage gaps closed: started lineup edge (NBA-019 LineupSpotLarry, gated on starting-lineup feed), "
                            "defensive matchup (NBA-015 DefRtgLena — defense-vs-offense under-totals), "
                            "foul-rate / free-throw matchups (NBA-016 FoulToneFern), "
                            "pace mismatch generalization (NBA-017 PaceMatchQuincy), "
                            "overtime observer (NBA-018 OvertoneOlive — counts OT games since Kalshi has no OT market currently). "
                            "Forward paper engine now runs every registered strategy via the SAME evaluator set as the backtester "
                            "(NBA-001/003/004/005/006/007 + NBA-002/010/011 totals + NBA-008/009/014 props, when priced). "
                            "Risk metrics expanded: per-strategy volatility, longest win/loss streaks, largest single win/loss, "
                            "loss rate, win/loss push/void counts. Site shows profit-by-month and profit-by-category breakdowns "
                            "on the leaderboard; per-strategy profit-by-market/team/month breakdowns on the strategy page. "
                            "Audit extended: exposure-cap-violation, impossible-probability, bet-still-pending-after-24h, "
                            "unknown-strategy detection, stale source flags."),
        "hypothesis": None,
        "test_performed": "49-test pytest suite now covers: extended performance metrics, profit-by groupings, exposure-cap audit, impossible-probability audit, pending-24h audit, empty-DB audit, total signal placement + settlement, prop signal no-market guard, kalshi_cents helper, unique-username invariant, unknown-strategy audit, paper total placement.",
        "result": "All 49 tests pass. Site builds without errors against empty seed DB and against a populated DB.",
        "verification": "tests/test_core.py + tests/test_collect_and_backtest.py — 49 passed in 0.56s.",
        "decision": "Continue collecting data via GitHub Actions; every pipeline run will inflate real results on top of this scaffold.",
        "next_steps": "Pass 4 (live): first real ESPN scoreboard + Kalshi backfill + box-scores + paper-betting pass executed in Actions.",
    },
]


def main():
    with db.get_db() as con:
        added = 0
        for e in ENTRIES:
            existing = con.execute("SELECT id FROM research_log WHERE question=?",
                                   (e["question"],)).fetchone()
            if existing:
                continue
            db.insert(con, "research_log", {
                "ts_utc": util.utcnow_iso(), **{k: e.get(k) for k in (
                    "question", "sources_searched", "data_discovered", "hypothesis",
                    "test_performed", "result", "verification", "decision", "next_steps")}})
            added += 1
        print(f"research entries added: {added}")


if __name__ == "__main__":
    main()
