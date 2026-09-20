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

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_bets_game ON bets(game_id);

-- Bet records are append-only. The ONLY permitted mutation is writing the
-- settlement columns (never decision/price/state columns). Any other UPDATE
-- aborts at the database level instead of relying on code discipline.
CREATE TRIGGER IF NOT EXISTS trg_bets_append_only BEFORE UPDATE ON bets
BEGIN
  SELECT CASE WHEN
    NEW.bet_id IS NOT OLD.bet_id
    OR NEW.run_id IS NOT OLD.run_id
    OR NEW.kind IS NOT OLD.kind
    OR NEW.strategy_id IS NOT OLD.strategy_id
    OR NEW.strategy_version IS NOT OLD.strategy_version
    OR NEW.username IS NOT OLD.username
    OR NEW.decision_utc IS NOT OLD.decision_utc
    OR NEW.game_id IS NOT OLD.game_id
    OR NEW.game_label IS NOT OLD.game_label
    OR NEW.tipoff_utc IS NOT OLD.tipoff_utc
    OR NEW.market IS NOT OLD.market
    OR NEW.selection IS NOT OLD.selection
    OR NEW.side IS NOT OLD.side
    OR NEW.price IS NOT OLD.price
    OR NEW.price_format IS NOT OLD.price_format
    OR NEW.source IS NOT OLD.source
    OR NEW.source_url IS NOT OLD.source_url
    OR NEW.source_ts IS NOT OLD.source_ts
    OR NEW.model_prob IS NOT OLD.model_prob
    OR NEW.market_prob IS NOT OLD.market_prob
    OR NEW.edge IS NOT OLD.edge
    OR NEW.stake_usd IS NOT OLD.stake_usd
    OR NEW.to_win_usd IS NOT OLD.to_win_usd
    OR NEW.ev_usd IS NOT OLD.ev_usd
    OR NEW.execution_status IS NOT OLD.execution_status
    OR NEW.fill_price IS NOT OLD.fill_price
    OR NEW.fee_usd IS NOT OLD.fee_usd
    OR NEW.contracts IS NOT OLD.contracts
    OR NEW.verification IS NOT OLD.verification
    OR NEW.notes IS NOT OLD.notes
    THEN RAISE(ABORT, 'bets are append-only: only result/settlement_utc/settlement_source/pnl_usd/roi may change')
  END;
END;

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
    return con


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
