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
4. **Backtest on real prices, then on outcomes — as two separate tracks.** The *priced*
   track (`hist_backtest.py`) replays 4,043 validated games from the free SBR archive
   (2013-14..2022-23, October-December of each season, moneylines only) at the archive's
   own prices, with a MARKET baseline (home side on every game) for comparison; model
   probabilities are shrunk and edges capped before a bet is allowed. The *signal* track
   (`signal_backtest.py`) scores each rule against verified final results strictly
   chronologically (4,173 finals) and is published as signal quality, **not** P&L — the
   two are never mixed. Totals, spreads and props have no free per-side price history, so
   those rules are forward-tested only; assumption prices are always labeled as such.
5. **Paper-trade**: 24 strategies, $1,000 each, 25%-Kelly capped at 3% per bet, settled from
   verified results. Backtest / forward records are always kept separate.
6. **Publish**: static site rebuilt each run — dashboard, leaderboard, per-strategy pages,
   upcoming bets, open positions, full trade history, source registry, research log,
   methodology. The dashboard also publishes the pipeline's own open anomalies: an empty
   database is presented as a failure, never as a clean run.

## Current state (verified 2026-09-22, read from `data/nbacomp.db`)

Read this before reading any result. Three things are true at once:

1. **The competition has no settled bets.** 10 forward bets are open (all NBA-002
   totals, all `pending`, priced at an assumed −110 because no observed price existed
   at decision time), zero settled, so no forward P&L, ROI or edge is claimed anywhere.
   Those 10 bets carry `price-not-observed`, `stale-state-at-decision` and
   `strategy-parked` quarantine flags: they are excluded from exposure and ranking and
   published on `positions.html#quarantined` rather than deleted.
2. **The priced historical backtest is done and it is negative.** 4,043 real games,
   six moneyline rules, all six lose money; the MARKET baseline loses −5.3% over the
   same games. Six strategies are therefore tiered `failed` by their own price
   evidence. (Full table in `PROJECT_REPORT.md`.)
3. **Everything else is forward-looking and labeled as such.**

| table | rows | note |
|-------|------|------|
| `games` | 4,346 | 4,173 finals (2023-10 → 2026-04) + 173 scheduled 2026-27; 5 cross-verified ESPN↔BRef |
| `hist_odds` | 4,043 | SBR archive, validated rows only (2013-14..2022-23); rejected rows are logged, never stored |
| `hist_backtests` | 63 | priced replay output: 6 rules × 10 seasons + `ALL` + the MARKET baseline |
| `signal_backtests` | 11,384 | outcome-only validation rows (no prices) |
| `team_gamelogs` / `player_gamelogs` | 2,940 / 40,366 | box-score walk (ESPN primary, BRef verification) |
| `odds_snapshots` | 672 | all **forward**: ESPN keeps no odds for past dates |
| `kalshi_markets` / `kalshi_candles` / `kalshi_orderbooks` | 59 / 530 / 102 | live NBA series + captured books; settled NBA markets expose no candles (probed, recorded) |
| `bets` / `bet_flags` | 10 / 33 | the 10 quarantined forward bets and why |
| `strategies` | 24 | 9 `failed`, 4 `weak`, 11 `forward_only` |
| `anomalies` / `collection_log` / `audit_log` | 1,684 / 1,975 / 2,797 | append-only; the dashboard quotes the latest audit pass *and* the cumulative log separately |

The last pipeline pass reported **0 critical / 3 warn / 8 info** anomalies of its own
(the three warnings are the intentional quarantine disclosures), while the cumulative
log holds every past finding — including one open critical *kind*
(`bet-quarantined-stale-state-at-decision`, the standing disclosure that those 10 bets
were priced wrong at decision time).

## The competition

- Window: **2026-09-20 → 2027-09-19** · 24 strategies · primary objective: **total return**
- Risk stats (drawdown, win rate, volatility) tracked and shown, not used for ranking
- Losing strategies are never hidden; every strategy page carries an auto-generated
  "why it worked / failed" analysis that is sample-size aware

## Strategy families (24 registered; most v1.0.0, five revised)

IDs, usernames and categories below are generated from `nbacomp/strategies.py` — the same
registry that populates the database and the site, so this table cannot drift from them.

| ID | Username | Category | Version |
|----|----------|----------|---------|
| NBA-001 | RestEdgeRaven | Rest & Scheduling | 1.0.0 |
| NBA-002 | PacePulsePete | Pace & Totals | 1.0.0 |
| NBA-003 | EloOracle | Team Ratings / Moneyline | 1.0.0 |
| NBA-004 | LineMoveTracker | Market Movement | 1.1.0 (labeled directional, fixed 1% stake) |
| NBA-005 | InjuryIQIvan | Injuries | 1.0.0 |
| NBA-006 | HomeCourtHana | Home/Away | 1.0.0 |
| NBA-007 | RoadWarriorRex | Travel & Schedule Spots | 1.0.0 |
| NBA-008 | GlassGuru | Player Props (Rebounds) | 1.0.0 |
| NBA-009 | DimeDoc | Player Props (Assists) | 1.0.0 |
| NBA-010 | RegimeRanger | Three-Point Regression | 1.0.0 |
| NBA-011 | ProfileSage | Shot Profile / Offensive Rebounding | 1.0.0 |
| NBA-012 | MarketMirrorMia | Cross-Market Divergence | 1.0.0 |
| NBA-013 | QuarterQuest | Quarter / Half Markets | 1.1.0 (live KXNBA1H vs Elo half-probability) |
| NBA-014 | BlowoutBlair | Game Script / Garbage Time | 1.0.0 |
| NBA-015 | DefRtgLena | Defensive Matchup | 1.0.0 |
| NBA-016 | FoulToneFern | Foul Rate / Free Throws | 1.0.0 |
| NBA-017 | PaceMatchQuincy | Pace Mismatch | 1.0.0 |
| NBA-018 | OvertoneOlive | Overtone Watcher | 1.0.0 |
| NBA-019 | LineupSpotLarry | Starting Lineup | 1.0.0 |
| NBA-020 | BlowoutBounce | Bounce Back | 1.0.0 |
| NBA-021 | StreakSkeptic | Streak Fade (counter-hypothesis) | 1.0.0 |
| NBA-022 | RestRigidity | Rest & Scheduling (Totals) | 1.0.0 |

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
  games), and the six moneyline rules simulated on it **all lose money**; totals,
  spreads and props have no free per-side price history, so they are forward-tested
  and never assigned a simulated profit.
- A rule's tier is decided by its own evidence: price-based ROI when a priced
  simulation exists (with the MARKET baseline quoted next to it), otherwise the
  outcome-only hit rate against the base rate. Losing rules stay on the site, named
  and explained.

## Repo layout

```
nbacomp/            core package (db, sources, engine, backtest, hist_backtest,
                    signal_backtest, paper, validation, audit, sitegen)
tools/              pipeline entrypoints (run_pipeline, seed_research, db_report,
                    hist_backtest, probe_*, run_scope, verify_deployed)
tests/              pytest suite (odds math, settlement, pushes, look-ahead guards,
                    engine math, SBR parser, run scope, deployed-site check, site)
.github/workflows/  collect-and-build (cron), tests
data/nbacomp.db     collected + derived state (SQLite, committed each run)
index.html …        generated site (GitHub Pages serves main:/)
```

## Run locally

The sandbox/dev image has no pytest and a PEP-668-managed system Python, so use a venv:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests            # offline suite, no network needed (200 tests)
.venv/bin/python -m nbacomp.collect daily    # needs open internet (runs automatically in CI)
.venv/bin/python tools/run_pipeline.py       # strategies + engines + audit + site build
.venv/bin/python tools/db_report.py          # row counts + collection health (CI gate)
.venv/bin/python tools/hist_backtest.py      # priced replay over the SBR archive -> hist_backtests
.venv/bin/python tools/run_scope.py --event schedule --message "[auto] x"   # who may skip?
.venv/bin/python tools/verify_deployed.py    # live Pages site vs committed bytes (CI; needs egress)
```

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
