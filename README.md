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
   observed prices.
5. **Paper-trade**: 14 strategies, $1,000 each, 25%-Kelly capped at 3% per bet, settled from
   verified results. Backtest / forward records are always kept separate.
6. **Publish**: static site rebuilt each run — dashboard, leaderboard, per-strategy pages,
   upcoming bets, open positions, full trade history, source registry, research log,
   methodology.

## The competition

- Window: **2026-09-20 → 2027-09-19** · 14 strategies · primary objective: **total return**
- Risk stats (drawdown, win rate, volatility) tracked and shown, not used for ranking
- Losing strategies are never hidden; every strategy page carries an auto-generated
  "why it worked / failed" analysis that is sample-size aware

## Strategy families (v1.0.0)

| ID | Username | Category |
|----|----------|----------|
| NBA-001 | RestEdgeRaven | Rest & scheduling (B2B fade) |
| NBA-002 | PacePulsePete | Pace & totals |
| NBA-003 | EloOracle | Team ratings / moneyline |
| NBA-004 | LineMoveTracker | Market movement |
| NBA-005 | InjuryIQIvan | Injuries (forward-first) |
| NBA-006 | HomeCourtHana | Home/away splits |
| NBA-007 | RoadWarriorRex | Travel & schedule spots |
| NBA-008 | GlassGuru | Rebound props (forward-first) |
| NBA-009 | DimeDoc | Assist props (forward-first) |
| NBA-010 | RegimeRanger | Three-point regression |
| NBA-011 | ProfileSage | Shot profile / OREB totals |
| NBA-012 | MarketMirrorMia | Cross-market divergence |
| NBA-013 | QuarterQuest | Quarter/half markets (awaiting market discovery) |
| NBA-014 | BlowoutBlair | Game-script / garbage-time props (forward-first) |

## Data policy (non-negotiables)

- Core pipeline = **keyless, free, public** sources only (ESPN, NBA.com/stats, Kalshi
  public market data). Registration-required or paid sources are excluded and documented.
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

```bash
python -m pytest tests          # offline test-suite (no network needed)
python -m nbacomp.collect daily # needs open internet (runs automatically in CI)
python tools/run_pipeline.py    # strategies + engines + audit + site build
```
