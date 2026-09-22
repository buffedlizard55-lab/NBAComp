# NBAComp — Autonomous NBA Betting Strategy Research & Paper-Trading Competition

**Live site:** https://buffedlizard55-lab.github.io/NBAComp/

An autonomous research laboratory: it researches the NBA betting ecosystem, collects and
cross-verifies real public data, converts public claims into testable hypotheses, backtests
them against verified historical prices where they exist, forward-tests them where they
don't, and runs a season-long paper-trading competition between system-generated strategies
— every simulated bet timestamped, priced from captured market data, and auditable.

> **Simulated paper trading only. No real money. Not betting advice.**

## How it works (autonomous loop)

```
RESEARCH → DISCOVER → VERIFY → MODEL → BACKTEST → FORWARD TEST → PAPER TRADE → MEASURE → ANALYZE → REPEAT
```

1. **Collect** (GitHub Actions, every 6h): ESPN schedule/scores/current odds, ESPN injury
   board, Kalshi public market data (markets, order books, candlesticks, official results),
   NBA.com/stats team & player game logs and advanced stats. Every fetch is logged with
   HTTP status; failures are recorded, never papered over.
2. **Verify**: final scores cross-checked ESPN ↔ NBA.com; discrepancies raise anomalies.
3. **Model**: chronological Elo (MOV-adjusted, season carryover), rolling pace/efficiency
   totals, rest/B2B/road-trip/time-zone features — built strictly from games before the
   decision date (look-ahead-proof by construction, tested).
4. **Backtest on real prices, on observed lines, and on outcomes — three tracks that
   are never mixed.** The *priced* track (`hist_backtest.py`) replays validated games
   from the free SBR archive (2013-14..2022-23, October-December of each season,
   moneylines only) at the archive's own prices, with a MARKET baseline (home side on
   every game) for comparison; model probabilities are shrunk and edges capped before a
   bet is allowed. The *line* track (`line_backtest.py`) uses the same archive's
   **observed** opening/closing spreads and totals — lines, not prices — and publishes
   cover rates and line accuracy, never P&L. The *signal* track (`signal_backtest.py`)
   scores each rule against verified final results strictly chronologically and is
   published as signal quality, **not** P&L. Totals, spreads and props have no free
   per-side price history, so no simulated profit is ever assigned to them; assumption
   prices in the live engine are always labeled as such.
5. **Paper-trade**: every registered strategy (count generated below), $1,000 each,
   25%-Kelly capped at 3% per bet, settled from verified results. Backtest / forward
   records are always kept separate.
6. **Publish**: static site rebuilt each run — dashboard, leaderboard, per-strategy pages,
   upcoming bets, open positions, full trade history, source registry, research log,
   methodology. The dashboard also publishes the pipeline's own open anomalies: an empty
   database is presented as a failure, never as a clean run.

## Current state (verified 2026-09-22, read from `data/nbacomp.db`)

Read this before reading any result. Four things are true at once:

1. **The competition has no settled bets.** Every paper bet is `pending` (the 2026-27
   season has not tipped off), so no forward P&L, ROI or edge is claimed anywhere.
2. **The priced historical backtest is done and it is negative.** 4,043 real games,
   six moneyline rules, all six lose money; the MARKET baseline loses −5.3% over the
   same games. Six strategies are therefore tiered `failed` by their own price
   evidence. (Full table in `PROJECT_REPORT.md`.)
3. **The spread rule is refuted at the market's own lines.** NBA-026's rule (Elo margin
   vs the archive's closing spread) covered **49.31%** of **1,888** decided firings,
   against the **52.38%** break-even a standard −110 price requires and a **49.53%**
   home-ATS baseline on 3,973 games — so it is parked by its own line evidence. No P&L
   is claimed for it in either direction: a cover rate is a frequency, not a profit.
4. **Everything else is forward-looking and labeled as such.**

Row counts are deliberately **not** quoted here. They move on every collection run, and
three past revisions of this file copied numbers that were already stale. Read
[`data/db_report.txt`](data/db_report.txt) instead — it is regenerated and committed by
every pipeline run, and it fails the CI job when a collector crashed.

<!-- facts:headline -->
- strategies registered: **27** — `nbacomp/strategies.py` is the only source of this number, and the table below is generated from it
- offline test suite: **242 tests** collected by pytest in 16 files (237 test functions; parametrized cases expand)
- live database row counts are **not quoted here**: read `data/db_report.txt`, which every pipeline run regenerates
<!-- /facts:headline -->

Quarantine state, stated precisely because it changed in this pass:

- **10 NBA-002 totals bets stay quarantined.** They were priced off rolling box-score
  state 650+ days old (the 2026-09-21 stale-state defect) and their strategy is parked
  by its own measured evidence. Critical flags exclude them from exposure and ranking;
  they are published on `positions.html#quarantined`, never deleted.
- **4 NBA-026 spread bets are live positions again.** Their `stale-state-at-decision`
  flags were misapplied — `eval_spread` reads only Elo and the observed spread, never a
  box score — so each carries an appended `stale-state-at-decision-retracted` row. The
  original flag row is still there (`bet_flags` is append-only at the database level);
  only its power to exclude the bet is withdrawn. NBA-026 itself is now parked, so those
  bets carry `strategy-parked` (warn) and place no more.

## The competition

- Window: **2026-09-20 → 2027-09-19** · primary objective: **total return**
- Risk stats (drawdown, win rate, volatility) tracked and shown, not used for ranking
- Losing strategies are never hidden; every strategy page carries an auto-generated
  "why it worked / failed" analysis that is sample-size aware

## Strategy families

IDs, usernames, categories and versions below are **generated** from
`nbacomp/strategies.py` — the same registry that populates the database and the site —
by `tools/doc_facts.py`, and `--check` runs in CI, so this table cannot drift from the
code. (It had drifted: an earlier revision of this file claimed 24 strategies and
listed 22.)

<!-- facts:strategy-table -->
| ID | Username | Category | Version |
|----|----------|----------|---------|
| NBA-001 | RestEdgeRaven | Rest & Scheduling | 1.1.0 |
| NBA-002 | PacePulsePete | Pace & Totals | 1.1.0 |
| NBA-003 | EloOracle | Team Ratings / Moneyline | 1.1.0 |
| NBA-004 | LineMoveTracker | Market Movement | 1.2.0 |
| NBA-005 | InjuryIQIvan | Injuries | 1.1.0 |
| NBA-006 | HomeCourtHana | Home/Away | 1.1.0 |
| NBA-007 | RoadWarriorRex | Travel & Schedule Spots | 1.1.0 |
| NBA-008 | GlassGuru | Player Props (Rebounds) | 1.1.0 |
| NBA-009 | DimeDoc | Player Props (Assists) | 1.1.0 |
| NBA-010 | RegimeRanger | Three-Point Regression | 1.1.0 |
| NBA-011 | ProfileSage | Shot Profile / Offensive Rebounding | 1.1.0 |
| NBA-012 | MarketMirrorMia | Cross-Market Divergence | 1.1.0 |
| NBA-013 | QuarterQuest | Quarter / Half Markets | 1.2.0 |
| NBA-014 | BlowoutBlair | Game Script / Garbage Time | 1.1.0 |
| NBA-015 | DefRtgLena | Defensive Matchup | 1.1.0 |
| NBA-016 | FoulToneFern | Foul Rate / Free Throws | 1.1.0 |
| NBA-017 | PaceMatchQuincy | Pace Mismatch | 1.1.0 |
| NBA-018 | OvertoneOlive | Overtone Watcher | 1.1.0 |
| NBA-019 | LineupSpotLarry | Starting Lineup | 1.1.0 |
| NBA-020 | BlowoutBounce | Game Script / Motivation | 1.1.0 |
| NBA-021 | StreakSkeptic | Streak Persistence / Market Overreaction | 1.1.0 |
| NBA-022 | RestRigidity | Rest & Scheduling (Totals) | 1.1.0 |
| NBA-023 | PlayoffGrindGus | Season Phase / Totals | 1.0.0 |
| NBA-024 | MomentumWitness | Streaks (counter-hypothesis to NBA-021) | 1.0.0 |
| NBA-025 | LongshotLarry | Academic anomaly / Favorite-longshot bias | 1.0.0 |
| NBA-026 | SpreadSageSam | Spreads / Against the spread | 1.0.0 |
| NBA-027 | TeamTotalTess | Team totals | 1.0.0 |
<!-- /facts:strategy-table -->

Each carries a hypothesis, entry/exit rules, sizing rule, expected edge, failure modes, data
limitations and look-ahead controls; the full text is on the site's strategy pages.

## Data policy (non-negotiables)

- Core pipeline = **keyless, free, public** sources only (ESPN, NBA.com/stats,
  Basketball-Reference, Kalshi public market data). BallDon'tLie was
  evaluated and excluded (keyless API retired; registered key now required).
  Registration-required or paid sources are excluded and documented.
- Free trials / freemium tiers are **not** treated as free.
- Nothing is invented: no odds, no stats, no fills, no liquidity, no results. If something
  can't be verified it is marked **unverified** and worked around.
- Historical price availability is probed at runtime and recorded — never assumed.
  Concretely: the SBR archive is real, free and in the database (4,043 validated
  games), and the six moneyline rules simulated on it **all lose money**. Totals,
  spreads and props have no free per-side price history, so they are never assigned a
  simulated profit; the archive's observed spreads and totals *are* used, in the
  line-based track, to measure cover rates and line accuracy instead.
- A rule's tier is decided by its own evidence: price-based ROI when a priced
  simulation exists (with the MARKET baseline quoted next to it), then the cover rate
  against the market's own observed lines where that market has line history, then the
  outcome-only hit rate against the base rate. Losing rules stay on the site, named
  and explained.
- A quarantine flag must describe a defect the decision actually had. Flags are scoped
  to the state each rule reads (`strategies.STATE_INPUTS`), and a misapplied flag is
  retracted by an appended row — never by editing or deleting the original.
- Documentation figures that can be read from the repository are generated, not typed:
  `tools/doc_facts.py --check` fails CI when README/report blocks disagree with the
  registry and the test suite.

## Repo layout

```
nbacomp/            core package (db, sources, engine, backtest, hist_backtest,
                    line_backtest, signal_backtest, paper, validation, audit,
                    sitegen, strategies)
tools/              pipeline entrypoints (run_pipeline, seed_research, db_report,
                    hist_backtest, line_backtest, probe_*, run_scope,
                    verify_deployed, doc_facts)
tests/              pytest suite (odds math, settlement, pushes, look-ahead guards,
                    engine math, SBR parser, run scope, quarantine scoping,
                    line-based validation, deployed-site check, site)
.github/workflows/  collect-and-build (cron), tests
data/nbacomp.db     collected + derived state (SQLite, committed each run)
data/db_report.txt  row counts + collection health, regenerated every run
data/live_verify.json  last byte-level check of the deployed Pages site
index.html …        generated site (GitHub Pages serves main:/)
```

## Run locally

The sandbox/dev image has no pytest and a PEP-668-managed system Python, so use a venv:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests            # offline suite, no network needed
.venv/bin/python -m nbacomp.collect daily    # needs open internet (runs automatically in CI)
.venv/bin/python tools/run_pipeline.py       # strategies + engines + audit + site build
.venv/bin/python tools/db_report.py          # row counts + collection health (CI gate)
.venv/bin/python tools/hist_backtest.py      # priced replay over the SBR archive -> hist_backtests
.venv/bin/python tools/line_backtest.py      # observed-line replay -> line_backtests (no P&L)
.venv/bin/python tools/doc_facts.py --check  # README/report figures vs the code (CI gate)
.venv/bin/python tools/run_scope.py --event schedule --message "[auto] x"   # who may skip?
.venv/bin/python tools/verify_deployed.py    # live Pages site vs committed bytes (CI; needs egress)
```

<!-- facts:suite -->
`python -m pytest tests` collects **242 tests** across 16 files (offline, no network needed). 237 of those are test functions; the difference is parametrized cases:

- `tests/test_adversarial.py` — 24 tests
- `tests/test_audit_summary.py` — 3 tests
- `tests/test_collect_and_backtest.py` — 9 tests
- `tests/test_core.py` — 45 tests
- `tests/test_doc_facts.py` — 6 tests
- `tests/test_hist_backtest.py` — 4 tests
- `tests/test_integrity_v2.py` — 13 tests
- `tests/test_line_backtest.py` — 16 tests
- `tests/test_live_shapes.py` — 18 tests
- `tests/test_new_markets.py` — 7 tests
- `tests/test_pipeline_fixes.py` — 33 tests
- `tests/test_quarantine_scope.py` — 13 tests
- `tests/test_run_scope.py` — 11 tests
- `tests/test_sbr_odds.py` — 7 tests
- `tests/test_verify_deployed.py` — 7 tests
- `tests/test_wave2.py` — 26 tests
<!-- /facts:suite -->

Collection tasks (`python -m nbacomp.collect <task>`):

| task | what it does | budget |
|------|--------------|--------|
| `daily` | everything below, in order, each task isolated so one crash cannot discard the rest | 1 run |
| `espn-backfill` | walks the ESPN scoreboard **backwards** (schedule + results — ESPN keeps no odds for past dates), cursor in `meta`, floor `20231001` | 113 days/run |
| `espn-forward` | upcoming schedule + tipoffs and the first pre-game lines | 42 days/run |
| `boxscores-backfill` | box scores for FINAL games not yet logged, cursor in `meta` | 40 games/run |
| `bdlt-*` (in `daily`) | **DISABLED** — BallDon'tLie retired its keyless API (HTTP 404 on all endpoints, CI-verified 2026-09-21) and now requires a registered API key, which the keyless-only policy excludes; collectors short-circuit and log `skipped`. Zero rows from this source ever entered the DB (verified 2026-09-21) | — |
| `kalshi-discovery` | probes candidate NBA series tickers, records which exist | 1 page each |
| `kalshi-snapshot` | OPEN markets + orderbooks (forward prices) | live series |
| `kalshi-candles` | candlesticks for stored markets in a window | window |
| `kalshi-settled-history` | probes whether settled markets expose candles/tape | 60 games |
| `bref-month` | current-month BRef schedule + verification (skipped in the offseason) | 2 pages |
| `repair` | idempotent self-heal: canonical team abbreviations, de-duplicated games, non-NBA rows purged | 1 pass |

Every fetch is logged to `collection_log` with its HTTP status, and every crash is recorded
with its traceback; `tools/db_report.py` prints row counts and fails the CI job when a
collector crashed during that run.
