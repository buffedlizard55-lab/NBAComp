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
4. **Backtest** where verified price history exists (Kalshi candlesticks for NBA winner
   markets), with Kalshi fee schedule and 1-tick slippage. **Forward-test** where it
   doesn't (props, totals lines pre-2026-27) — labeled assumptions never passed off as
   observed prices. Because **no free historical price series exists at all**, the
   decision rules are additionally validated **outcome-only** against verified final
   results (`signal_backtest.py`), and those results are published as *signal quality,
   not P&L* — never mixed with backtest or forward records.
5. **Paper-trade**: 22 strategies, $1,000 each, 25%-Kelly capped at 3% per bet, settled from
   verified results. Backtest / forward records are always kept separate.
6. **Publish**: static site rebuilt each run — dashboard, leaderboard, per-strategy pages,
   upcoming bets, open positions, full trade history, source registry, research log,
   methodology. The dashboard also publishes the pipeline's own open anomalies: an empty
   database is presented as a failure, never as a clean run.

## Current state (verified 2026-09-21, after the fix run)

Read this before reading any result on the site. The competition has **no settled
results yet** — 9 forward bets are open (all NBA-002, all `pending`, the 2026-27
season has not tipped off), zero are settled, so no P&L, ROI or edge is claimed
anywhere.

What the database actually holds after the auto-run of commit `8be85b1`
(2026-09-21T03:15:27Z) and the pipeline run of 2026-09-21T04:34Z that
placed the competition's first bets, read out of `data/nbacomp.db`:

| table | rows | note |
|-------|------|------|
| `games` | 2,961 | 2,788 finals (2024-10 → 2026-04) + 173 scheduled 2026-27 (incl. the 2026-10-20 openers), one canonical row per game; 0 cross-verified so far |
| `kalshi_markets` | 59 | 6 `KXNBAGAME` (the 2026-10-20 openers) + 53 `KXNBAMVP`, all live/active |
| `kalshi_candles` | 412 | hourly OHLCV for those 6 openers, accumulating since 2026-09-18 |
| `kalshi_orderbooks` | 48 | real quotes, e.g. `KXNBAGAME-26OCT20OKCSAS-SAS` bid 53 / ask 54 |
| `odds_snapshots` | 240 | all **forward**: ESPN keeps no odds for past dates, so these are the first lines ever captured |
| `injuries` | 0 | ESPN injury board is empty in the offseason |
| `team_gamelogs` / `player_gamelogs` | 182 / 3,158 | box-score walk in progress (~2,697 ESPN finals still lack team box scores — the new BallDon'tLie deep-history collectors target exactly this gap) |
| `quarter_scores` | 0 | KXNBA1H settlement fallback; starts filling as in-game scores are captured |
| `bets` | 9 | all **open** (NBA-002 UNDER on 2026-27 preseason/opening-week lines, `PRICED-ASSUMPTION` at −110); none settled — the season has not tipped off |
| `verifications` | 0 | cross-verification runs once ESPN and BRef rows overlap |

Three facts this establishes rather than assumes:

1. **Settled Kalshi NBA markets expose no price history at all.** 480 constructed tickers
   were probed for candlesticks and 4 for the trade tape: 0 candles, 0 trades (HTTP 200).
   Price history can therefore only accumulate forward from 2026-09-18.
2. **Basketball-Reference alone yields a complete two-season schedule with finals and
   tipoff times** (2,643/2,643 rows have `tipoff_utc`) — enough for schedule- and
   result-based modelling, not enough to price a bet.
3. **The 2026-27 season has not tipped off**, so the only live NBA markets are the three
   opening-night games and season-long futures.
4. **Team vocabulary had to be unified before anything could be joined.** ESPN abbreviates
   six franchises differently from BRef and from Kalshi's event tickers
   (`NY/GS/SA/UTAH/WSH/NO` vs `NYK/GSW/SAS/UTA/WAS/NOP`), which stored 497 duplicate games
   and made **zero** Kalshi markets joinable to a game. ESPN's NBA scoreboard also lists
   preseason exhibitions against non-NBA clubs (`STARS`, `STRIPES`, `WORLD`, `GUANGZHOU`,
   `HAPOEL`, `LON`, `MEL`), which had been stored as NBA games. Both are fixed, repaired in
   place, and now raised as audit findings if they recur.
5. **No free historical NBA price series exists.** probe7 (`data/diagnostics.txt`, run
   `35554330261`) found past ESPN scoreboards return games with **no odds at all** (20260115:
   9 events / 0 odds; 20260613: 1/0; 20250115: 11/0; 20241022: 2/0), BRef publishes no odds,
   and ESPN's summary endpoint rejects BRef boxscore IDs (HTTP 400). With settled Kalshi
   markets exposing neither candles nor tape, **price-taking backtests are impossible** and
   every strategy has to be forward-tested. BRef's schedule + finals + tipoffs support
   modelling and settlement, not pricing.

Before that run, `games`, `kalshi_markets`, `kalshi_candles`, `bets` and `anomalies` were
all **0** while every workflow reported success. Three defects caused it, all fixed and all
pinned by `tests/test_pipeline_fixes.py` (research-log entries 9 and 10):

1. `kalshi_snapshot` crashed on live market rows that omit `series_ticker`, so **no Kalshi
   price was ever stored**;
2. the two-season history backfill was gated behind a manual `workflow_dispatch` input the
   6-hourly cron never sets, so `games` stayed at 0 and every backtest logged
   *"no games in window"*;
3. the daily Basketball-Reference task requested the current month, which does not exist in
   the offseason, logging a 404 "failure" every run.

## The competition

- Window: **2026-09-20 → 2027-09-19** · 22 strategies · primary objective: **total return**
- Risk stats (drawdown, win rate, volatility) tracked and shown, not used for ranking
- Losing strategies are never hidden; every strategy page carries an auto-generated
  "why it worked / failed" analysis that is sample-size aware

## Strategy families (22 registered; most v1.0.0, three revised)

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
  BallDon'tLie, Basketball-Reference, Kalshi public market data).
  Registration-required or paid sources are excluded and documented.
- Free trials / freemium tiers are **not** treated as free.
- Nothing is invented: no odds, no stats, no fills, no liquidity, no results. If something
  can't be verified it is marked **unverified** and worked around.
- Historical price availability is probed at runtime and recorded — never assumed.

## Repo layout

```
nbacomp/            core package (db, sources, engine, backtest, paper, audit, sitegen)
tools/              pipeline entrypoints (collect, run_pipeline, seed_research)
tests/              pytest suite (odds math, settlement, pushes, look-ahead guards, site)
.github/workflows/  collect-and-build (cron), tests
data/nbacomp.db     collected + derived state (SQLite, committed each run)
index.html …        generated site (GitHub Pages serves main:/)
```

## Run locally

The sandbox/dev image has no pytest and a PEP-668-managed system Python, so use a venv:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests          # offline suite, no network needed (131 tests)
.venv/bin/python -m nbacomp.collect daily  # needs open internet (runs automatically in CI)
.venv/bin/python tools/run_pipeline.py     # strategies + engines + audit + site build
.venv/bin/python tools/db_report.py        # row counts + collection health (CI gate)
```

Collection tasks (`python -m nbacomp.collect <task>`):

| task | what it does | budget |
|------|--------------|--------|
| `daily` | everything below, in order, each task isolated so one crash cannot discard the rest | 1 run |
| `espn-backfill` | walks the ESPN scoreboard **backwards** (schedule + results — ESPN keeps no odds for past dates), cursor in `meta`, floor `20231001` | 113 days/run |
| `espn-forward` | upcoming schedule + tipoffs and the first pre-game lines | 42 days/run |
| `boxscores-backfill` | box scores for FINAL games not yet logged, cursor in `meta` | 40 games/run |
| `bdlt-season-games` (in `daily`) | BallDon'tLie season game lists: deep-history scores + independent final-score cross-check (`score-mismatch-balldontlie` on conflict), resumable per season | ~150 requests/run budget |
| `bdlt-boxscores` (in `daily`) | BallDon'tLie per-game box scores for finals missing team rows (OREB=NULL rows make pace features unavailable, never 0), cursor in `meta` | ~150 requests/run budget |
| `kalshi-discovery` | probes candidate NBA series tickers, records which exist | 1 page each |
| `kalshi-snapshot` | OPEN markets + orderbooks (forward prices) | live series |
| `kalshi-candles` | candlesticks for stored markets in a window | window |
| `kalshi-settled-history` | probes whether settled markets expose candles/tape | 60 games |
| `bref-month` | current-month BRef schedule + verification (skipped in the offseason) | 2 pages |
| `repair` | idempotent self-heal: canonical team abbreviations, de-duplicated games, non-NBA rows purged | 1 pass |

Every fetch is logged to `collection_log` with its HTTP status, and every crash is recorded
with its traceback; `tools/db_report.py` prints row counts and fails the CI job when a
collector crashed during that run.
