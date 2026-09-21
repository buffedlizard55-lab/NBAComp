# NBAComp Project Report

## What was built

A complete NBA autonomous sports-betting strategy research laboratory and
year-long paper-trading competition. The project researches the NBA betting
ecosystem without manual intervention, collects and cross-verifies real public
data, encodes discovered strategies as data-defined hypotheses, runs them in
an honest no-look-ahead backtester and a forward paper-trading engine, and
publishes the entire pipeline as a static GitHub Pages site.

> ### STATUS CORRECTION — verified 2026-09-21
>
> Earlier revisions of this report described a pipeline that was collecting data and a
> competition that was about to populate. **That was not true.** At commit `53465b8`
> (the state this correction pass started from) the committed database contained
> `games=0`, `odds_snapshots=0`, `kalshi_markets=0`, `kalshi_candles=0`,
> `kalshi_orderbooks=0`, `injuries=0`, `team_gamelogs=0`, `player_gamelogs=0`, `bets=0`,
> `verifications=0` — read directly out of `data/nbacomp.db`. The live dashboard showed
> "Games in database 0 (0 cross-verified)" and 19 strategies at $1,000 with 0 bets.
>
> Three defects caused the empty pipeline and are fixed in this pass (details in
> *Known bugs*, research-log entry 9, and `tests/test_pipeline_fixes.py`):
>
> 1. `kalshi_snapshot` crashed on live market rows that omit `series_ticker`
>    (`IntegrityError: NOT NULL constraint failed: kalshi_markets.series_ticker`), so the
>    task died and **no Kalshi price was ever stored**;
> 2. the two-season history backfill was gated behind a manual `workflow_dispatch` input
>    that the 6-hourly cron never sets, and the daily job only fetched `today-2..today+8`,
>    so `games` never left 0 and every backtest logged *"no games in window"*;
> 3. the daily Basketball-Reference task requested the current month, which has no page in
>    the offseason, logging a 404 "failure" every run and masking real failures.
>
> A process defect made all three survivable: the test suite was green (72 tests) while
> covering none of those paths, the audit had no empty-database check, and the workflow
> exited 0. **Current honest result count: 0 backtested strategies, 0 forward-tested
> strategies, 0 paper bets, $0 P&L.** Nothing in this report claims otherwise.

> ### Verification of the fix — Actions run `35553630400`, commit `f2133d9` (2026-09-21T02:18:37Z)
>
> The fixes were pushed and executed on a real runner, and its committed
> `data/db_report.txt` / `data/nbacomp.db` were queried directly:
>
> | table | before (`53465b8`) | after (`f2133d9`) |
> |-------|--------------------|-------------------|
> | `games` | 0 | **2,643** (2024-10-22 → 2026-06-13, all `final`, all with `tipoff_utc`) |
> | `kalshi_markets` | 0 | **59** (6 `KXNBAGAME` openers + 53 `KXNBAMVP`, all active) |
> | `kalshi_candles` | 0 | **406** (hourly, 2026-09-18 → 2026-09-21, 6 tickers) |
> | `kalshi_orderbooks` | 0 | **6** (e.g. `KXNBAGAME-26OCT20OKCSAS-SAS` bid 53 / ask 54) |
> | `collection_log` | 697 | 810 |
> | `anomalies` | 0 | 6 (3 × `kalshi-multi-tickers-per-event` info, `no-bets-despite-data`, `persistent-source-failure`) |
> | `odds_snapshots` / `injuries` / `team_gamelogs` / `bets` | 0 | still 0 (see below) |
>
> `collector crashes recorded (since 2026-09-21T02:16:31Z): 0`; `backtest` logged `ok` with
> `bets=0` instead of `empty`; `bref-backfill` logged `skipped — offseason` instead of a 404
> failure.
>
> Two facts are now established rather than assumed:
>
> - **Settled Kalshi NBA markets expose no price history.** `kalshi-settled-history` probed
>   60 games → 480 constructed market tickers for candlesticks and 4 tickers for the trade
>   tape: `candles=0`, `trades=0`, tape queries HTTP 200 with 0 rows, recorded as
>   `availability=unavailable` in `meta`. Historical Kalshi prices do not exist; history can
>   only accumulate forward.
> - **Basketball-Reference alone supplies a full two-season schedule with finals and tipoff
>   times** (2,643/2,643 rows have `tipoff_utc`), which is enough to model schedules and
>   settle results but not enough to price a bet.
>
> Still honestly zero: no odds history yet (the ESPN backfill cursor is at `20260901` and
> walks back ~113 days per run to `20231001`), no injuries (the ESPN board is empty in the
> offseason), no box scores (cursor `20241028`), and **no bets of any kind** — so still
> **0 backtested strategies, 0 forward-tested strategies, $0 P&L**.

## Strategies discovered (19, all v1.0.0)

| ID | Username | Category | Win condition |
|----|----------|----------|--------------|
| NBA-001 | RestEdgeRaven | Rest & Scheduling (B2B fade) | Back-to-back away vs 2+ day-rested home |
| NBA-002 | PacePulsePete | Pace & Totals | Rolling pace/efficiency total vs line |
| NBA-003 | EloOracle | Team Ratings / Moneyline | MOV-adjusted Elo vs Kalshi winner market |
| NBA-004 | LineMoveTracker | Market Movement | Follow 48h→2h move ≥8¢ |
| NBA-005 | InjuryIQIvan | Injuries | Top-2-min Out/Doubtful → opponent winner |
| NBA-006 | HomeCourtHana | Home/Away | Home rolling home net vs away rolling road net |
| NBA-007 | RoadWarriorRex | Travel & Schedule Spots | 5+-road-trip finale + 2+ tz shift |
| NBA-008 | GlassGuru | Player Props (Rebounds) | Rolling5 REB vs prop line |
| NBA-009 | DimeDoc | Player Props (Assists) | Rolling5 AST vs prop line |
| NBA-010 | RegimeRanger | Three-Point Regression | Rolling 10 3P% vs season 3P% (totals bias) |
| NBA-011 | ProfileSage | Shot Profile / OREB | Combined OREB rate matchup vs pace total |
| NBA-012 | MarketMirrorMia | Cross-Market Divergence | Devigged ESPN ML vs Kalshi ≥4pp |
| NBA-013 | QuarterQuest | Quarter / Half Markets | Awaiting KXNBA1H/KXNBAQ1 discovery |
| NBA-014 | BlowoutBlair | Game Script / Garbage Time | Elo margin ≥13 → starter-points under |
| NBA-015 | DefRtgLena | Defensive Matchup | Top DRtg vs top ORtg → under total |
| NBA-016 | FoulToneFern | Foul Rate / Free Throws | FTA-vs-opp FTA mismatch → totals bias |
| NBA-017 | PaceMatchQuincy | Pace Mismatch | abs(pace mismatch) ≥5 → model total |
| NBA-018 | OvertoneOlive | Overtone Watcher | OT-game counter (no executable market yet) |
| NBA-019 | LineupSpotLarry | Starting Lineup | Bench scorer promoted → 1H edge |

All entries have unique usernames, version numbers (semver),
explicit entry rules, exit rules, sizing rules, expected edge,
failure modes, data limitations, and look-ahead controls.

## Strategies backtested (chronologically, no look-ahead)

Implementations in `nbacomp/backtest.py`. The same evaluator set runs in the
forward paper engine so a backtested rule is exactly the rule traded in the
competition.

- **Result to date: 0 backtest bets.** Every `backtest` row in
  `collection_log` reads `status='empty'`, `detail='no games in window'`
  (8 rows at `53465b8`) because `games` was empty. The engine is implemented
  and unit-tested, but it has never had verified prices to run on, so no
  backtest number is reported anywhere.
- **Backtest universe**: Kalshi NBA winner markets where hourly candlestick
  history exists; settled events are walked via the `event_ticker` encoding
  (KXNBAGAME-YYMMDDTEAM1TEAM2 → date + team pair). Whether settled markets
  still expose candlesticks is probed every run by
  `collect.kalshi_settled_history` and recorded in
  `meta:kalshi_settled_availability:KXNBAGAME` — it is not assumed.
- **Prices**: last fully-closed hourly candle strictly before decision, +1
  tick slippage; depth unobservable, labeled.
- **Totals/spread**: priced at standard −110 by default; every such bet row
  is labeled `PRICED-ASSUMPTION` (free historical sportsbook closing lines
  do not exist; this is documented, not hidden).
- **Cross-validation**: ESPN finals cross-checked vs Basketball-Reference
  monthly results; mismatches logged into `verifications` with anomaly status.
- **Settlement**: Kalshi's own `result` field is ground truth for Kalshi
  markets; for non-Kalshi markets the verified final score decides; conflicts
  raise anomalies rather than silent resolution.
- **Look-ahead guards**: enforced in code (PriceBook, append-only DB trigger,
  no bet inside 1h of tipoff) and tested (38 regression tests cover candle
  closure, side awareness, no-lookahead, dedup, append-only).

## Strategies forward-tested (live paper competition)

The paper engine in `nbacomp/paper.py` mirrors backtest evaluators and
re-evaluates them at every scheduled collection (cron every 6h, also
workflow_dispatch), capturing:

- Live Kalshi orderbook ask for the home-YES side (NO side derived as 100−ask).
- ESPN moneyline snapshots for divergence detection (NBA-012).
- Recent ESPN or Kalshi-strike totals/spread lines for NBA-002/010/011.
- Prop market discovery for NBA-008/009/014 (auto-activates when discovered).
- Injury adjustments from the ESPN injury board for NBA-005.
- Closing-line capture at settlement (where candles exist).

Every bet is timestamped at decision time and at settlement time;
exposure is capped at 25% of bankroll per strategy.

**Result to date: 0 forward bets.** The engine needs (a) upcoming games with
tipoffs and (b) a mapped Kalshi winner market with a live ask; at `53465b8`
neither existed (0 games, 0 markets, and the 2026-27 season has not tipped
off). The leaderboard therefore shows 19 strategies at their $1,000 starting
bankroll with 0 bets and status `awaiting-opportunity`.

## Current paper-trading strategies

All 19 strategies are registered and represented on the leaderboard, each
with a $1,000 virtual wallet, sorted by total forward P&L (the primary
competition objective). **Every one of them currently has 0 bets and $0 P&L**
(`data/leaderboard.json` at `53465b8`: 19 entries, all `pnl: 0.0`,
`bets: 0`, `status: "awaiting-opportunity"`).

Two separate reasons, both real:

1. The 2026-27 season has not tipped off (preseason late October 2026), so no
   game markets are open and no game bet is possible yet. Season-long futures
   (KXNBAMVP, championship) are the only live NBA series right now and no
   strategy trades them yet.
2. Before this pass, the collectors were not storing anything even when data
   existed — see the status correction above.

The sandbox this report was written in cannot reach ESPN/Kalshi/BRef at all
(`curl` to `site.api.espn.com`, `stats.nba.com`, `basketball-reference.com`
and `api.elections.kalshi.com` all return `000`; only `pypi.org` and
`api.github.com` answer), so all collection executes in GitHub Actions.

## Data sources

The core pipeline uses only **keyless, free, public** sources:

- **ESPN site API (site.web host)** — schedule, scores, current odds,
  injuries, box scores (replaces blocked stats.nba.com from CI runners;
  documented in `sources_registry.py`).
- **NBA.com/stats (stats.nba.com)** — registered but unreachable from
  CI runners (Akamai 403 / connection tarpitting); collectors exist for
  when network conditions allow.
- **Basketball-Reference** — independent score verification (BREF monthly
  results pages; HTML scraping).
- **Kalshi trade-api v2** — public (no-key) market-data endpoint for NBA
  series (KXNBAGAME, KXNBASPREAD, KXNBATOTAL, KXNBA1H, KXNBAPTS, KXNBAREB,
  KXNBAAST, KXNBAPRA, KXNBASTL, KXNBABLK, KXNBAMVP — confirmed at runtime).
- **Sister sources** — sibling collector patterns (NBAInjuryReport,
  PriceKalshiHistorical, KalshiPaperSim) reviewed and re-used; raw data
  never imported from them.

Free trials and freemium tiers are treated as **not free**. The
`rejected:paid-odds-archives` registry entry documents the Scottfree-style
paid archives and the policy decision to exclude them.

## Source limitations

- **No free historical price series of any kind — established by probe, not
  assumed (2026-09-21).** Three independent channels were tested on a real
  runner (`data/diagnostics.txt`, probe7 / probe6):
  * Kalshi settled NBA markets expose **no candlesticks and no trade tape**
    (480 constructed tickers → 0 candles; 4 tape queries HTTP 200 → 0 trades);
  * ESPN scoreboards for past dates return games with **no odds block at all**
    (20260115: 9 events / 0 with odds; 20260613: 1/0; 20250115: 11/0;
    20241022: 2/0);
  * Basketball-Reference publishes no odds, and ESPN's `summary` endpoint
    rejects BRef boxscore ids (`202410220BOS` → HTTP 400).

  Consequence, stated plainly: **price-taking backtests cannot be run
  honestly for any past NBA game.** Every strategy is therefore forward-tested,
  and any bet whose line was not observed is labelled `PRICED-ASSUMPTION`.
- **Historical sportsbook closing lines (totals/spread/props)**: no free
  archive exists; paid archives are excluded by policy. Consequence:
  pre-2025-26 historical totals/spread backtests are not attempted; -110
  is used for any model-vs-line bet, labeled `PRICED-ASSUMPTION`.
- **Historical injury reports**: no free dated archive exists; injury
  strategies (NBA-005) are forward-first.
- **Historical order-book depth**: Kalshi does not publish historical
  books; backtest entries use last closed hourly trade price +1 tick,
  labeled as an execution assumption.
- **Referee assignments**: no reliable free historical feed — referee
  strategies are explicitly NOT claimed (per spec).
- **NBA.com/stats advanced tracking**: blocked from CI; replaced by ESPN
  box-score data + BBR verification.
- **Pre-2024-25 totals lines**: no historical archive so pre-2025
  totals backtests are not attempted.
- **OT-only markets**: Kalshi does not sell OT-only markets on NBA; the
  OT observer (NBA-018) is an auditor, not a trader.

## Kalshi findings

- Public market-data endpoints reachable from CI (200 OK on
  `/series/{ticker}`, `/markets`, `/markets/{ticker}/orderbook`,
  `/markets/candlesticks`, `/events`). No authentication required for
  the read endpoints we use.
- Confirmed NBA series existence: KXNBAGAME, KXNBASPREAD, KXNBATOTAL,
  KXNBA1H, KXNBAPTS, KXNBAREB, KXNBAAST, KXNBAPRA, KXNBASTL, KXNBABLK,
  KXNBAMVP. Candidate tickers that did NOT exist: KXNBAPOINT,
  KXNBATHREES, KXNBATO, KXNBADD.
- Settlement rules: per Kalshi docs, NBA markets settle at game end; OT
  is included for game-winner markets. Fee formula
  `0.07 · C · P · (1−P)` applied as documented.
- Probe 5 discovered that settled events carry `title` and `sub_title`
  but no `ticker`/`close_time` keys on the event row; the
  `event_ticker` field (KXNBAGAME-26JUN13NYKSAS) carries both the date
  and team pair.
- **Settled markets are NOT retrievable from the public API — corrected
  2026-09-21.** An earlier revision of this report claimed settled history
  could be walked via `/events?series=X&status=settled` and that each event's
  markets carry the recorded result. **That claim was false.** The events
  endpoint does return settled event rows (200/page, 1,448+ observed over 8
  pages, `data/diagnostics.txt` probe5), but for every settled event the
  per-event market queries came back empty: `/markets?event_ticker=…`,
  `/events/{ticker}`, series+`status=settled` filters and direct ticker GETs
  all returned 0 rows or 404. Evidence: `collection_log` rows
  `kalshi-backfill kalshi:KXNBAGAME … events=3 skipped=397 markets_total=0`
  for all 8 live series, plus the summary row *"settled markets not exposed by
  public API"*. Consequence: **no historical Kalshi price series exists for
  settled NBA markets**, so backtests cannot be priced from them.
- What is still untested (now probed on every run by
  `collect.kalshi_settled_history`, answer recorded in
  `meta:kalshi_settled_availability:KXNBAGAME`): whether settled markets still
  expose **candlesticks** or the **trade tape**. Tickers for those queries are
  constructed from verified game rows — event `{SERIES}-{YY}{MON}{DD}{TEAM1}{TEAM2}`
  and market `{event}-{AWAY|HOME|YES|NO}` (live KXNBAGAME markets are per-team:
  `KXNBAGAME-26OCT20OKCSAS-SAS`, fixture-verified) — and only tickers that
  actually return rows are ever stored.
- Fee schedule `ceil(0.07 · C · P · (1−P))` is implemented in
  `util.kalshi_fees_dollars` and pinned by a formula-level unit test.

## MasterSite findings

Reviewed the sibling-site directory `https://buffedlizard55-lab.github.io/MasterSite/`
(44 verified sites). Adoptions:

- **NBAInjuryReport** — ESPN injury endpoint audit + 403 fingerprint
  episode; methodology and source-verification format reused.
- **PriceKalshiHistorical** — Kalshi collector (markets / orderbooks /
  candles) pattern and rate-limit discipline; SQLite + parquet storage.
- **KalshiPaperSim** — paper-trading ledger discipline (audit trails,
  append-only history).
- **Commodities** — Actions collector committing fills on a schedule.
- **SFWeather** — no-invention audit culture.

Not reused: SF local guides, elections, drugs, GEMSDOE — either
out-of-scope or rely on paid feeds.

## Website status

Built and committed in repo root; GitHub Pages serves `main:/`. Pages:

- `index.html` — Dashboard (competition P&L, leaderboard, upcoming,
  recently settled, backtest snapshot).
- `leaderboard.html` — Full forward leaderboard with risk metrics
  (volatility, longest streaks, largest W/L) + profit-by-month and
  profit-by-category tables + backtest section.
- `strategies.html` — 19 strategy cards with thesis, rules, sources,
  market types, sizing, look-ahead controls, failure modes, data
  limitations, performance block (both kinds), forward breakdowns
  (market / team / month), auto-analysis, and recent bets.
- `upcoming.html` — Every intended bet pre-tipoff with model prob,
  edge, stake, trigger, source ts.
- `positions.html` — Open positions with entry price, exposure, fees.
- `history.html` — Complete bet history with text + filter search
  (kind / market / result / strategy); `data/history.json` for client
  filtering, includes closing_price where available.
- `sources.html` — Source registry + known-unavailable data +
  documented failure-handling protocol.
- `research.html` — Research log entries (R-001..R-008) + anomaly
  register (latest 50).
- `methodology.html` — Decision-time integrity, backtesting, forward
  testing, closing-line capture, **live betting scope gap**, **OT
  handling**, execution realism, P&L/sizing, data verification,
  strategy versioning.

The site is intentionally **clean, fast, mobile-friendly, search-
and filterable, and renders no fabricated data** — every empty
section is plainly labeled.

## GitHub status (verified 2026-09-21 with `gh`)

- Repository: `buffedlizard55-lab/NBAComp`
- Session branch: `arena/01a0c1af-nbacomp`, cut from `main` at `53465b8c757b66b368deee268d3deb136b766089`
  (`[auto] collect + pipeline + site build 2026-09-21T00:37:13Z`).
- `main` HEAD at the time of this correction: `53465b8…` — i.e. `main` and this
  session's starting point are the same commit.
- Workflows present: `collect-and-build` (cron `23 */6 * * *`, plus push and
  `workflow_dispatch`) and `tests` (push + PR). Recent runs on 2026-09-21 are
  green for `tests`; the `collect-and-build` runs were "successful" while the
  database they committed was empty — which is exactly what the new
  `tools/db_report.py` gate now prevents.
- Open PRs at the time of writing: **#4** from another session branch
  (`arena/01a0c12e-nbacomp`, "Live-shape fixes…"), still open; **#3**, **#2**,
  **#1** merged. This pass's work goes out on `arena/01a0c1af-nbacomp`.

## Deployment status (verified 2026-09-21)

- GitHub Pages **is enabled and built**: `gh api repos/buffedlizard55-lab/NBAComp/pages`
  returns `status: "built"`, `source: {branch: main, path: /}`,
  `html_url: https://buffedlizard55-lab.github.io/NBAComp/`, `https_enforced: true`.
- The deployed dashboard was fetched and read in this pass; its cards show
  `Games in database 0 (0 cross-verified)`, `Kalshi NBA series live 0`, forward
  P&L `+$0.00`, `Strategies with live bets 0/19`, and a 19-row leaderboard of
  $1,000 / 0-bet strategies. So the site is genuinely live, and genuinely empty.
- `curl` to the Pages host from this sandbox returns `000` (network egress is
  limited to `pypi.org` / `api.github.com` here); the page content above was
  read through the workspace's page-fetch tool, not curl.
- The site is regenerated from the database on every `collect-and-build` run and
  committed to `main`, so deployment does not depend on a manual step.

## Known bugs

### Fixed in this pass (2026-09-21)

| Defect | Evidence | Fix |
|--------|----------|-----|
| `kalshi_snapshot` crashed on any live market row without `series_ticker`, killing the whole task so no Kalshi price was ever stored | `collection_log` `daily-kalshi-snapshot` `crash` ×2 with `IntegrityError: NOT NULL constraint failed: kalshi_markets.series_ticker` | `collect.normalize_market_row()`: series from the query actually sent, event from the ticker prefix; identity-less rows are skipped with an anomaly; a zero-store run raises `kalshi-snapshot-stored-nothing` |
| Two-season history backfill only ran on manual `workflow_dispatch` with `backfill=true`; the cron never sets it, and `daily` fetched only `today-2..today+8`, so `games` stayed at 0 | `games=0` in the committed DB; 8× `backtest … 'no games in window'` | `espn_backfill_resumable()` (20 days/run, cursor in `meta`, floor `20231001`), `boxscores_backfill_resumable()` (40 games/run), plus a BRef two-season bootstrap in the workflow that runs only while `games` is empty |
| BRef daily task requested the current month, which has no page in the offseason → a 404 logged as `fail` every run | `bref-backfill` / `bref-verify` `'2026-september: HTTP 404'` | `bref_current_month()` logs `skipped` in Jul/Aug/Sep and uses the season-end-year mapping |
| Audit reported an empty database as clean; workflow exited 0 and published a green site | `anomalies=0` while every data table was 0 | audit raises `empty-games-table`, `empty-kalshi-markets-table`, `collector-crash` (24 h window), `persistent-source-failure`, `no-bets-despite-data`; dashboard shows a pipeline-health banner; `tools/db_report.py` prints row counts and fails the job on a crash |
| Settled-Kalshi claim was wrong in both this report and the code comments | `kalshi-backfill … markets_total=0` for all 8 series | `kalshi_settled_history()` probes candles/tape with constructed tickers and records the true answer in `meta` |
| Report/README drift (14 vs 19 strategies, 49/54 vs actual test count, stale branch/PR/deployment claims) | `strategies` table has 19 rows; `pytest` reports 92 | README + this report regenerated from the registry and from a real test run |

### Fixed after the first verified runs (2026-09-21, runs `35553997534` / `35554765928`)

| Defect | Evidence | Fix |
|--------|----------|-----|
| `team_gamelogs.pts` always NULL — `parse_team_boxscore` filled only `score` while the collector writes `pts` | 46 rows with `pts=NULL` in run `35553997534` | `pts` mirrors the observed score; incomplete rows are dropped and re-collected rather than patched from another table |
| The same game stored twice — ESPN says `NY/GS/SA/UTAH/WSH/NO`, BRef and Kalshi tickers say `NYK/GSW/SAS/UTA/WAS/NOP` | 17 `duplicate-game` anomalies; 582 games with ESPN-only spellings; **zero** Kalshi markets joinable to a game | `engine.canon_team()` applied in all three ESPN collectors; `repair_team_vocab()` re-labelled 637 game rows + 366 gamelog rows and merged 497 duplicate games |
| Non-NBA preseason opponents stored as NBA games | rows for `STARS`, `STRIPES`, `WORLD`, `GUANGZHOU`, `HAPOEL`, `LON`, `MEL` | `engine.is_nba_team()` filter in the collectors (skips + logs), 10 rows purged, audit raises `non-nba-team-in-games` |
| Daily ESPN window reached only 8 days ahead, so the 2026-10-20 openers (the only games with live Kalshi markets) had no row | 0 scheduled games before run `35554765928` | `espn_forward_window()` (42 days) → 174 scheduled games and the first 24 odds rows |
| Box-score collectors spent their budget on `bref:` rows whose id is not an ESPN event id | 41 `boxscore espn fail` rows | only `espn:`-namespaced ids are fetched |

Repair verified by executing it against a copy of the committed 3,393-row database:
**2,886 games, 0 duplicates, exactly the 30 NBA abbreviations, 0 non-NBA gamelog rows.**

| Deleting the 46 `pts=NULL` rows left `team_gamelogs` at **0** — the backfill cursor had already passed those days, so nothing was re-collected | run `3572932`: `team_gamelogs=0` | the backfill restarts at the first final-game date whenever a repair drops rows, and otherwise stays finished |

### Still open

- The `audit.run_checks` `injury-listing-late` query joins on team
  abbrev without considering game-direction; a Player X injury with
  Team A might trigger a flag against the WRONG game if Team A has
  two games against different opponents in the same week. Documented
  as info-level (not critical).
- `_parse_prop_subtitle` substring-match on player names can in rare
  cases match a name that contains another player's name as a prefix
  (e.g. "Ja" matching "Ja Morant" before "James Harden"). Mitigated
  by sorting candidates longest-first.
- The PR #2 first run showed an `action_required` status (Node.js 20
  deprecation warning). Subsequent runs all `success`. Tracking the
  Node 20 action if it persists.

## Known limitations

See **Source limitations** above. Additional structural limits:

- The competition runs one year; bankroll is virtual ($1,000 starting).
- Strategies do not place OT-only bets because Kalshi does not sell
  such markets.
- Live / in-game betting is explicitly out of scope (Kalshi closes
  winner markets at tipoff; the forward engine enforces a 1-hour
  pre-tip guard).
- Referee strategies are not implemented (per spec: "only if the
  assignment and historical statistics can be reliably verified").

## Post-fix data-collection additions (2026-09-21)

| addition | why | verified effect |
|----------|-----|-----------------|
| `espn_forward_window()` (42 days ahead) | the daily job looked only 8 days ahead, so the 2026-10-20 openers — the only games with live Kalshi markets — had no row for the forward engine to join | pending next run |
| `_merge_espn_game_row()` | the ESPN walk duplicated games the BRef bootstrap already held (5 `duplicate-game` warnings) and BRef-only rows can never be joined to box scores or odds | pending next run |
| `parse_team_boxscore` fills `pts` | run `35553997534` stored 46 `team_gamelogs` rows with `pts=NULL`, which would have starved every pace/efficiency feature | pending next run |
| `espn-backfill` budget 20 → 113 days/run | the walk needs to reach 2023-10 to cover two full seasons | cursor moved `20260901` → `20260511` in run `35553997534` |
| box-score collectors take `espn:`-namespaced ids only | 41 requests were spent on `bref:` rows whose id is not an ESPN event id | 46 team / 664 player rows landed once the ids matched |

## Recommended next research areas

1. **Closing-line-value (CLV) signal**: for every forward bet record the
   difference between entry price and last observed price before tipoff;
   track whether positive-CLV bets have higher forward hit rate (the
   "best predictor of future performance is closing-line value" finding).
2. **Player-prop pricing model**: extend the prop strategies with a
   hierarchical Bayesian model using minutes × pace × opponent-def-allowed;
   the Kalshi KXNBAPTS/KXNBAREB/KXNBAAST series are already discovered.
3. **Quarter / half markets**: re-probe daily for KXNBA1H / KXNBAQ1
   series activation; activate NBA-013 QuarterQuest and NBA-019
   LineupSpotLarry automatically when a series is found.
4. **Lineup source**: investigate NBA.com lineups RSS or
   `data.nba.net/data/1.5/lineups` (keyless) — if reachable, activate
   NBA-019 (LineupSpotLarry) and add Q1/1H signals.
5. **Multi-day Kalshi probes**: hourly cron probing for live orderbook
   depth at decision time, then size against observed depth rather
   than observed ask (currently we use the ask).
6. **NBA.com/stats activation**: a fallback DNS (e.g. via Tor or a
   proxy) that can reach stats.nba.com from CI would unlock the
   comprehensive player-tracking data the spec calls out (#3).

## Test summary

Run in this pass, in a fresh venv (`python3 -m venv .venv && .venv/bin/pip
install pytest`), on `arena/01a0c1af-nbacomp`:

```
.venv/bin/python -m pytest tests   ->  104 passed

tests/test_collect_and_backtest.py   9 tests
tests/test_core.py                  45 tests
tests/test_live_shapes.py           18 tests
tests/test_pipeline_fixes.py        30 tests   (new this pass)
```

The new file pins every defect above: snapshot stores markets that omit
`series_ticker` and raises an anomaly when nothing is storable; the ESPN
backfill walks backwards, persists its cursor, and refuses to advance past a
failed day; the box-score backfill respects its request budget, resumes, and
never refetches a logged day; the BRef task skips the offseason and uses the
season-end year; settled-history stores only tickers that returned candles and
records `unavailable` when the API returns nothing; the audit flags an empty
pipeline; the dashboard publishes its own anomalies.

Note on why the earlier passes missed all of this: pytest is not installed in
this sandbox and the system Python is PEP-668 managed, so a bare `python -m
pytest` fails with "No module named pytest". Earlier claims of a green suite
came from a venv; the suite itself simply had no coverage of the collector
paths that were failing.

## Honesty statement

No data was invented: no prices, results, fills, liquidity, or URLs were
fabricated, and where data is unavailable the system surfaces `"unverified"`,
`"pending"`, or explicit gap markers rather than a plausible-looking
placeholder.

That standard was not fully met in *prose*, and this revision corrects it.
Earlier revisions described a system that was collecting and about to produce
results when the committed database was empty, repeated a false claim about
settled Kalshi markets being retrievable, and carried stale branch, PR, test
count and deployment details. Each of those statements has been replaced above
with what was actually read from `data/nbacomp.db`, `data/diagnostics.txt`,
`gh`, and the live Pages site on 2026-09-21, and each fix is pinned by a test
in `tests/test_pipeline_fixes.py`.

What remains genuinely unverified is labelled as such: whether Kalshi exposes
candlesticks or a trade tape for settled NBA markets (probed every run, answer
recorded in `meta`), and every strategy's actual edge — no strategy has a
single settled bet, so no performance claim of any kind is made.
