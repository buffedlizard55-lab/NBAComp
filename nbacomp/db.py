"""SQLite storage layer. WAL mode; append-only where correctness matters.

Design rules:
- `bets` rows are never updated in place. Corrections are appended to audit_log
  and the row is superseded (superseded_by), preserving the original decision
  state (competition requirement #41/#47).
- Every ingested fact stores its source and capture timestamp.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from . import util

DB_PATH = os.environ.get("NBACOMP_DB", os.path.join("data", "nbacomp.db"))

# Decision-state columns of `bets` that may NEVER change after insert. The
# settlement columns (result, settlement_utc, settlement_source, pnl_usd, roi,
# closing_price-via-settle-path) are the only permitted mutations.
_BETS_CORE_IMMUTABLE = [
    "bet_id", "run_id", "kind", "strategy_id", "strategy_version", "username",
    "decision_utc", "game_id", "game_label", "tipoff_utc", "market", "selection",
    "side", "price", "price_format", "source", "source_url", "source_ts",
    "model_prob", "market_prob", "edge", "stake_usd", "to_win_usd", "ev_usd",
    "execution_status", "fill_price", "fee_usd", "contracts", "closing_price",
    "verification", "notes",
]
# Added 2026-09-21 for prop-bet settlement (strike + market identity + player
# must be frozen at decision time; settlement needs them).
_BETS_V2_IMMUTABLE = ["strike", "market_ticker", "prop_player"]


def _bets_trigger_sql(extra_cols=()):
    cols = _BETS_CORE_IMMUTABLE + list(extra_cols)
    conds = "\n    OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in cols)
    return (
        "CREATE TRIGGER IF NOT EXISTS trg_bets_append_only BEFORE UPDATE ON bets\n"
        "BEGIN\n"
        f"  SELECT CASE WHEN\n    {conds}\n"
        "  THEN RAISE(ABORT, 'bets are append-only: only result/settlement_utc/"
        "settlement_source/pnl_usd/roi may change')\n"
        "  END;\n"
        "END;")


def _bets_delete_trigger_sql():
    """A placed bet is evidence: rows can never be deleted, only flagged.

    2026-09-21 adversarial pass: the UPDATE guard alone left `DELETE FROM bets`
    legal, so a losing bet could have been erased without a trace. Deletion now
    aborts at the database level; defects are recorded in `bet_flags` instead.
    """
    return (
        "CREATE TRIGGER IF NOT EXISTS trg_bets_no_delete BEFORE DELETE ON bets\n"
        "BEGIN\n"
        "  SELECT RAISE(ABORT, 'bets are append-only: rows cannot be deleted, "
        "use bet_flags to record a defect');\n"
        "END;")


SCHEMA = f"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_utc TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS games (
  game_id TEXT PRIMARY KEY,             -- namespaced: espn:401584793 / kalshi:KXNBAGAME-...
  source TEXT NOT NULL,
  season TEXT NOT NULL,                 -- e.g. 2025-26
  game_date_et TEXT NOT NULL,
  tipoff_utc TEXT,
  home_team TEXT NOT NULL,
  away_team TEXT NOT NULL,
  home_score INTEGER,
  away_score INTEGER,
  status TEXT NOT NULL,                 -- scheduled|in|final|postponed|canceled
  season_type TEXT,                     -- preseason|regular|postseason|unknown (NULL = unrecorded)
  neutral_site INTEGER DEFAULT 0,
  source_updated_utc TEXT,
  captured_utc TEXT NOT NULL,
  verified INTEGER DEFAULT 0,           -- 1 when cross-checked vs secondary source
  verified_against TEXT
);
CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date_et);
CREATE INDEX IF NOT EXISTS idx_games_season ON games(season, status);

CREATE TABLE IF NOT EXISTS odds_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id TEXT NOT NULL,
  market TEXT NOT NULL,                 -- ml|spread|total|team_total|prop:...|kalshi:SERIES
  selection TEXT NOT NULL,              -- e.g. home/away/over 225.5/LAL -4.5
  line REAL,                            -- numeric line if applicable (spread/total)
  price REAL NOT NULL,                  -- american odds or kalshi cents
  price_format TEXT NOT NULL,           -- american|kalshi_cents|decimal
  source TEXT NOT NULL,
  source_url TEXT,
  source_updated_utc TEXT,
  captured_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_odds_game ON odds_snapshots(game_id, market, captured_utc);

CREATE TABLE IF NOT EXISTS kalshi_markets (
  ticker TEXT PRIMARY KEY,
  series_ticker TEXT NOT NULL,
  event_ticker TEXT NOT NULL,
  title TEXT,
  subtitle TEXT,
  market_type TEXT,                     -- winner|spread|total|1h|prop:*
  strike_values TEXT,                   -- JSON
  status TEXT,
  close_time TEXT,
  expected_expiration_time TEXT,
  yes_bid INTEGER, yes_ask INTEGER, last_price INTEGER,
  volume INTEGER, open_interest INTEGER,
  result TEXT,
  settled_time TEXT,
  captured_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kmarkets_series ON kalshi_markets(series_ticker, status);

CREATE TABLE IF NOT EXISTS kalshi_candles (
  ticker TEXT NOT NULL,
  interval INTEGER NOT NULL,            -- minutes
  ts_utc TEXT NOT NULL,
  open INTEGER, high INTEGER, low INTEGER, close INTEGER,
  volume INTEGER,
  captured_utc TEXT NOT NULL,
  PRIMARY KEY (ticker, interval, ts_utc)
);

CREATE TABLE IF NOT EXISTS kalshi_orderbooks (
  ticker TEXT NOT NULL,
  captured_utc TEXT NOT NULL,
  yes_bid INTEGER, yes_ask INTEGER,
  bids TEXT, asks TEXT,                 -- JSON [[price,size],...] (yes side)
  PRIMARY KEY (ticker, captured_utc)
);

CREATE TABLE IF NOT EXISTS injuries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player TEXT NOT NULL,
  team TEXT,
  player_id TEXT,
  status TEXT NOT NULL,                 -- Out|Doubtful|Questionable|Probable|GTD|...
  reason TEXT,
  game_date TEXT,
  source TEXT NOT NULL,
  source_url TEXT,
  published_utc TEXT,
  captured_utc TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_inj_player ON injuries(player, captured_utc);

CREATE TABLE IF NOT EXISTS team_gamelogs (
  season TEXT NOT NULL,
  game_id TEXT NOT NULL,
  game_date_et TEXT NOT NULL,
  team TEXT NOT NULL, opp TEXT NOT NULL,
  is_home INTEGER NOT NULL,
  pts INTEGER, opp_pts INTEGER,
  wl TEXT,
  minutes INTEGER,
  fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
  oreb INTEGER, dreb INTEGER, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER,
  tov INTEGER, pf INTEGER,
  plus_minus REAL,
  source TEXT NOT NULL, captured_utc TEXT NOT NULL,
  PRIMARY KEY (season, game_id, team)
);

CREATE TABLE IF NOT EXISTS player_gamelogs (
  season TEXT NOT NULL,
  game_id TEXT NOT NULL,
  game_date_et TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player TEXT NOT NULL,
  team TEXT NOT NULL,
  status TEXT,                          -- started / bench / dnp
  minutes REAL, pts INTEGER, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER,
  tov INTEGER, fg3m INTEGER, fgm INTEGER, fga INTEGER, ftm INTEGER, fta INTEGER,
  plus_minus REAL,
  source TEXT NOT NULL, captured_utc TEXT NOT NULL,
  PRIMARY KEY (season, game_id, player_id)
);

CREATE TABLE IF NOT EXISTS team_season_stats (
  season TEXT NOT NULL,
  team TEXT NOT NULL,
  measure TEXT NOT NULL,                -- Base|Advanced|FourFactors|Hustle...
  stats_json TEXT NOT NULL,             -- full row from source
  source TEXT NOT NULL, captured_utc TEXT NOT NULL,
  PRIMARY KEY (season, team, measure)
);

CREATE TABLE IF NOT EXISTS player_season_stats (
  season TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player TEXT NOT NULL,
  team TEXT NOT NULL,
  games INTEGER,                        -- games played (source-reported)
  minutes REAL, pts REAL, reb REAL, ast REAL, stl REAL, blk REAL, tov REAL,
  fg_pct REAL, fg3_pct REAL, ft_pct REAL,
  points_per_game REAL,                 -- explicit per-game fields where the
  rebounds_per_game REAL,               -- source reports them directly
  assists_per_game REAL,                -- (balldontlie /players = per game)
  per_game_basis TEXT,                  -- 'per_game'|'season_totals' (honest label)
  stats_json TEXT NOT NULL,             -- full row from source
  source TEXT NOT NULL, captured_utc TEXT NOT NULL,
  PRIMARY KEY (season, player_id, team)
);

-- Game identity aliases: when a duplicate game row is merged (ESPN id
-- supersedes a BRef id, or a repair collapses duplicates), bets keep their
-- ORIGINAL game_id — the bets table is append-only and its trigger aborts
-- game_id rewrites. The alias table is the only bridge between the old id
-- and the canonical row. (2026-09-21: the repair's `UPDATE bets SET game_id`
-- would RAISE once any bets existed.)
CREATE TABLE IF NOT EXISTS game_aliases (
  old_game_id TEXT PRIMARY KEY,
  new_game_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_galias_new ON game_aliases(new_game_id);

-- Per-quarter team scores captured from in-game scoreboard snapshots
-- (enables 1H/quarter settlement and OT detection once in-game captures exist).
CREATE TABLE IF NOT EXISTS quarter_scores (
  game_id TEXT NOT NULL,
  quarter INTEGER NOT NULL,             -- 1..5+
  home_score INTEGER NOT NULL,
  away_score INTEGER NOT NULL,
  captured_utc TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (game_id, quarter)
);

-- Signal-validation backtests: outcome-only validation of each strategy's
-- decision rule on verified historical results. NO market prices are used
-- or implied — this is evidence about the predictive signal, not P&L.
CREATE TABLE IF NOT EXISTS signal_backtests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  season TEXT NOT NULL,
  game_id TEXT NOT NULL,
  game_date_et TEXT NOT NULL,
  decision_utc TEXT NOT NULL,
  market TEXT NOT NULL,                 -- winner|total (rule market, not a price)
  selection TEXT NOT NULL,              -- side the rule picked
  model_prob REAL,                      -- rule's own probability where it has one
  outcome INTEGER,                      -- 1 if the picked side won / hit
  metric_value REAL,                    -- rule-specific (margin, total, ...)
  trigger TEXT,
  UNIQUE (run_id, strategy_id, game_id, market, selection)
);
CREATE INDEX IF NOT EXISTS idx_sigbt_strat ON signal_backtests(strategy_id, season);

CREATE TABLE IF NOT EXISTS signal_backtest_summary (
  run_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  season TEXT NOT NULL,                 -- 'ALL' = pooled
  n_signals INTEGER NOT NULL,
  n_hits INTEGER,
  hit_rate REAL,
  baseline_rate REAL,                  -- league-base-rate the rule beats
  brier REAL,                          -- winner rules only
  mae_total REAL,                      -- total rules only
  baseline_mae_total REAL,             -- 'always league-average total' MAE
  avg_model_prob REAL,
  max_streak_hits INTEGER,
  max_streak_misses INTEGER,
  generated_utc TEXT NOT NULL,
  PRIMARY KEY (run_id, strategy_id, season)
);

CREATE TABLE IF NOT EXISTS strategies (
  strategy_id TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  name TEXT NOT NULL,
  username TEXT NOT NULL,
  category TEXT NOT NULL,
  thesis TEXT NOT NULL,
  description TEXT NOT NULL,
  market_types TEXT NOT NULL,           -- JSON list
  data_sources TEXT NOT NULL,           -- JSON list of source ids
  entry_rules TEXT NOT NULL,
  exit_rules TEXT NOT NULL,
  sizing_rules TEXT NOT NULL,
  historical_window TEXT,
  expected_edge TEXT,
  failure_modes TEXT,                   -- JSON list
  data_limitations TEXT,
  lookahead_controls TEXT,
  lineage TEXT,                         -- parent strategy id if a fork
  status TEXT NOT NULL DEFAULT 'active',
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bets (
  bet_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,                 -- backtest:<id> | forward:<ts> | live
  kind TEXT NOT NULL,                   -- backtest|forward
  strategy_id TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  username TEXT NOT NULL,
  decision_utc TEXT NOT NULL,
  game_id TEXT NOT NULL,
  game_label TEXT,
  tipoff_utc TEXT,
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  side TEXT,                            -- yes/no/over/under/home/away
  price REAL NOT NULL,
  price_format TEXT NOT NULL,
  source TEXT NOT NULL,
  source_url TEXT,
  source_ts TEXT,
  model_prob REAL,
  market_prob REAL,
  edge REAL,
  stake_usd REAL NOT NULL,
  to_win_usd REAL,
  ev_usd REAL,
  execution_status TEXT NOT NULL,       -- simulated_fill|no_fill|void
  fill_price REAL,
  fee_usd REAL,
  contracts INTEGER,
  closing_price REAL,                   -- last observed price at tipoff (where available)
  result TEXT,                          -- win|loss|push|void|pending
  settlement_utc TEXT,
  settlement_source TEXT,
  pnl_usd REAL,
  roi REAL,
  verification TEXT NOT NULL DEFAULT 'unverified',
  notes TEXT,
  superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_bets_strat ON bets(strategy_id, kind, run_id);

-- Historical sportsbook odds archive (SBR season pages). Only rows that passed
-- the parser's structural + numeric cross-checks are stored; `reject_reason` is
-- never set on stored rows, and rejected games are counted in
-- collection_log/anomalies instead of being silently dropped.
CREATE TABLE IF NOT EXISTS hist_odds (
  season TEXT NOT NULL,
  game_date_et TEXT NOT NULL,
  away TEXT NOT NULL,
  home TEXT NOT NULL,
  away_q TEXT, home_q TEXT,              -- JSON quarter scores
  away_final INTEGER NOT NULL,
  home_final INTEGER NOT NULL,
  open_total REAL, close_total REAL,
  open_home_spread REAL, close_home_spread REAL,
  ml_away INTEGER, ml_home INTEGER,
  source TEXT NOT NULL DEFAULT 'sbr',
  source_url TEXT NOT NULL,
  source_row_hash TEXT,                  -- detects later edits at the source
  spread_printed_row TEXT,               -- which row (away|home) printed the spread
  spread_sign_from_ml INTEGER,           -- 1 when the moneyline made home the favourite
  cross_checked INTEGER NOT NULL DEFAULT 0,
  cross_check_detail TEXT,
  captured_utc TEXT NOT NULL,
  PRIMARY KEY (season, game_date_et, away, home)
);
CREATE INDEX IF NOT EXISTS idx_hist_odds_season ON hist_odds(season, game_date_et);

-- Price-based historical simulation results over the validated SBR archive.
-- Separate from signal_backtests (outcome-only) and from bets (forward paper
-- trading): these are the only results in the repository that carry real
-- historical market prices.
CREATE TABLE IF NOT EXISTS hist_backtests (
  run_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,             -- or MARKET for the home-team baseline
  season TEXT NOT NULL,                  -- or ALL
  bets INTEGER NOT NULL,
  wins INTEGER NOT NULL,
  win_rate REAL,
  pnl REAL NOT NULL,
  staked REAL NOT NULL,
  roi REAL,
  max_dd REAL,
  avg_edge REAL,
  avg_price REAL,
  games_available INTEGER NOT NULL,
  generated_utc TEXT NOT NULL,
  PRIMARY KEY (run_id, strategy_id, season)
);

-- Bet quarantine flags. A bet placed by a version of the engine that is now
-- known to be defective (stale model state, assumed price never observed,
-- strategy parked by its own evidence) cannot be deleted or edited — the bets
-- table is append-only — so the defect is recorded here instead. Flagged bets
-- stay visible in every ledger, are excluded from live exposure and from the
-- competition's ranking P&L, and their own settlement P&L is published
-- separately as a quarantined figure.
CREATE TABLE IF NOT EXISTS bet_flags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bet_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  flag TEXT NOT NULL,                   -- stale-state-at-decision|price-not-observed|strategy-parked|...
  severity TEXT NOT NULL,               -- critical|warn|info
  detail_json TEXT NOT NULL,
  flagged_utc TEXT NOT NULL,
  run_id TEXT,
  UNIQUE(bet_id, flag)
);
CREATE INDEX IF NOT EXISTS idx_bet_flags_bet ON bet_flags(bet_id);

CREATE TRIGGER IF NOT EXISTS trg_bet_flags_no_update BEFORE UPDATE ON bet_flags
BEGIN
  SELECT RAISE(ABORT, 'bet_flags is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_bet_flags_no_delete BEFORE DELETE ON bet_flags
BEGIN
  SELECT RAISE(ABORT, 'bet_flags is append-only');
END;
CREATE INDEX IF NOT EXISTS idx_bets_game ON bets(game_id);

-- Bet records are append-only. The ONLY permitted mutation is writing the
-- settlement columns (never decision/price/state columns). Any other UPDATE
-- aborts at the database level instead of relying on code discipline.
{_bets_trigger_sql()}

-- A placed bet is evidence; it can be flagged but never deleted.
{_bets_delete_trigger_sql()}

CREATE TABLE IF NOT EXISTS bankroll_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL,
  as_of_utc TEXT NOT NULL,
  starting REAL NOT NULL, current REAL NOT NULL,
  available REAL NOT NULL, exposure REAL NOT NULL,
  reason TEXT NOT NULL,
  UNIQUE (strategy_id, as_of_utc, reason)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anomalies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  detected_utc TEXT NOT NULL,
  severity TEXT NOT NULL,               -- info|warn|critical
  check_name TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  checked_utc TEXT NOT NULL,
  claim TEXT NOT NULL,
  primary_source TEXT NOT NULL, primary_value TEXT NOT NULL,
  secondary_source TEXT, secondary_value TEXT,
  status TEXT NOT NULL,                 -- match|mismatch|unverified
  discrepancy TEXT
);

CREATE TABLE IF NOT EXISTS research_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  question TEXT NOT NULL,
  sources_searched TEXT NOT NULL,
  data_discovered TEXT,
  hypothesis TEXT,
  test_performed TEXT,
  result TEXT,
  verification TEXT,
  decision TEXT,
  next_steps TEXT
);

CREATE TABLE IF NOT EXISTS collection_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  task TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,                 -- ok|fail|partial|skipped
  detail TEXT,
  rows INTEGER
);

CREATE TABLE IF NOT EXISTS source_status (
  source_id TEXT PRIMARY KEY,
  checked_utc TEXT NOT NULL,
  ok INTEGER NOT NULL,
  http_status INTEGER,
  detail TEXT,
  sample_hash TEXT
);
"""


def connect(db_path: str = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("PRAGMA foreign_keys=ON")
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Idempotent schema upgrades for databases created before a change.

    The injuries natural-key index is created here (not in SCHEMA) because an
    existing database may already hold duplicate listings: creating a UNIQUE
    index on them aborts the whole schema script and would break every other
    CREATE TABLE on a pre-existing database.
    """
    # bets v2: decision-state columns for prop settlement (2026-09-21)
    # + closing_price (added to SCHEMA after production DBs existed, so it
    # needs a migration too — settlement writes it and the site reads it)
    gcols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
    if "season_type" not in gcols:
        # 2026-09-21: ESPN's scoreboard reports season type (1 preseason,
        # 2 regular, 3 postseason). Without it, preseason exhibitions and
        # playoff games are indistinguishable from regular-season games in
        # every rolling feature. Existing rows keep NULL = unrecorded (never
        # guessed retroactively).
        con.execute("ALTER TABLE games ADD COLUMN season_type TEXT")
    hcols = {r[1] for r in con.execute("PRAGMA table_info(hist_odds)")}
    for col, ddl in (("spread_printed_row", "TEXT"),
                     ("spread_sign_from_ml", "INTEGER")):
        if col not in hcols:
            # 2026-09-21: recorded when the archive's spread sign could not be
            # trusted and the sign was taken from the moneyline instead.
            con.execute(f"ALTER TABLE hist_odds ADD COLUMN {col} {ddl}")
    scols = {r[1] for r in con.execute("PRAGMA table_info(strategies)")}
    if "version_history" not in scols:
        # 2026-09-21: version history is part of the audit trail — a strategy
        # that changes what it trades gets a new version and the previous
        # version's rule text is preserved here, never rewritten.
        con.execute("ALTER TABLE strategies ADD COLUMN version_history TEXT")
    cols = {r[1] for r in con.execute("PRAGMA table_info(bets)")}
    for col, decl in (("strike", "REAL"), ("market_ticker", "TEXT"),
                      ("prop_player", "TEXT"), ("closing_price", "REAL")):
        if col not in cols:
            con.execute(f"ALTER TABLE bets ADD COLUMN {col} {decl}")
    # recreate the append-only trigger so pre-v2 databases also protect the
    # new decision-state columns (idempotent)
    con.execute("DROP TRIGGER IF EXISTS trg_bets_append_only")
    con.execute(_bets_trigger_sql(_BETS_V2_IMMUTABLE))
    con.execute(_bets_delete_trigger_sql())
    try:
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_inj_natural "
            "ON injuries(player, team, status, COALESCE(published_utc, ''), "
            "COALESCE(note, ''))")
    except sqlite3.IntegrityError:
        # Duplicate listings exist: keep the earliest capture per natural key.
        con.execute(
            "DELETE FROM injuries WHERE id NOT IN ("
            "  SELECT MIN(id) FROM injuries GROUP BY player, team, status, "
            "  COALESCE(published_utc, ''), COALESCE(note, ''))")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_inj_natural "
            "ON injuries(player, team, status, COALESCE(published_utc, ''), "
            "COALESCE(note, ''))")
    con.commit()


@contextmanager
def get_db(db_path: str = None) -> Iterator[sqlite3.Connection]:
    con = connect(db_path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def insert(con: sqlite3.Connection, table: str, row: dict[str, Any], replace: bool = False) -> None:
    cols = list(row.keys())
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    con.execute(sql, [row[c] for c in cols])


def upsert(con: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = list(row.keys())
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
           f"ON CONFLICT DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols if c not in ('season', 'game_id', 'team'))}")
    con.execute(sql, [row[c] for c in cols])


def log_audit(con: sqlite3.Connection, actor: str, action: str, entity: str, detail: dict) -> None:
    insert(con, "audit_log", {
        "ts_utc": util.utcnow_iso(), "actor": actor, "action": action,
        "entity": entity, "detail_json": json.dumps(detail, default=str)})


def log_anomaly(con: sqlite3.Connection, severity: str, check: str, detail: dict) -> None:
    insert(con, "anomalies", {
        "detected_utc": util.utcnow_iso(), "severity": severity, "check_name": check,
        "detail_json": json.dumps(detail, default=str)})


def log_collection(con: sqlite3.Connection, task: str, source: str, status: str,
                   detail: str = "", rows: int = 0) -> None:
    insert(con, "collection_log", {
        "ts_utc": util.utcnow_iso(), "task": task, "source": source,
        "status": status, "detail": detail[:2000], "rows": rows})
