"""Pass-4 integrity tests: reporting blind spots, misattributed anomalies, marks.

Every defect fixed here was found by reading the committed database, not by
reading the code, and each test names the number that proved it:

1. ``tools/db_report.py`` hardcoded a 23-table list. The schema has 28 real
   tables, so `quarter_scores` and `player_season_stats` -- both genuinely
   empty -- never appeared in the report's "empty tables" line, which exists
   precisely to catch that. Its docstring claimed "every table".

2. 1,277 of the 1,455 accumulated `warn` anomalies were `sbr-row-changed`, all
   written in one run (2026-09-21T23:27:17..23Z) when the SBR parser went
   v1 -> v2. `source_row_hash` hashes the parsed row, so our own parser change
   was indistinguishable from an edit at the archive, and the anomaly text
   asserted the archive had changed. It had not.

3. The competition spec requires a current price for every open position. No
   code anywhere computed one (grep for mark-to-market returned nothing), and
   the positions page claimed "current marks come from the latest collected
   orderbook snapshots" while rendering no mark column at all.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from nbacomp import collect, db, marks, sitegen, util  # noqa: E402
from nbacomp.sources import sbr  # noqa: E402

DB_PATH = os.path.join(REPO, "data", "nbacomp.db")

#: bound before any monkeypatching, so a test can redirect db_report at a
#: scratch database without recursing into the patched symbol
_orig_connect = db.connect


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "p4.db"))
    yield c
    c.close()


# --------------------------------------------------------------------------
# 1. db_report must report every table that exists, not a hardcoded subset
# --------------------------------------------------------------------------

def test_db_report_table_list_matches_schema():
    """The report's table list is derived from sqlite_master, never hardcoded."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import importlib

    import db_report
    importlib.reload(db_report)
    c = db.connect(":memory:")
    reported = db_report.all_tables(c)
    actual = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert set(reported) == actual
    assert len(reported) == len(actual)
    # the five tables the hardcoded list silently dropped
    for t in ("quarter_scores", "player_season_stats", "signal_backtest_summary",
              "meta", "game_aliases"):
        assert t in reported, t
    c.close()


def test_db_report_flags_previously_hidden_empty_tables(capsys, monkeypatch, tmp_path):
    """An empty table can no longer hide by being absent from the list."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import importlib

    import db_report
    importlib.reload(db_report)
    path = str(tmp_path / "report.db")
    c = db.connect(path)
    # quarter_scores and player_season_stats are empty in the committed DB too
    assert c.execute("SELECT COUNT(*) FROM quarter_scores").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM player_season_stats").fetchone()[0] == 0
    c.close()
    monkeypatch.setattr(db_report.db, "connect", lambda *a, **k: _orig_connect(path))
    rc = db_report.main([])
    out = capsys.readouterr().out
    assert rc == 0, "a fresh database has no collector crashes, so the job must pass"
    assert "quarter_scores" in out
    assert "player_season_stats" in out
    line = next(l for l in out.splitlines() if l.startswith("empty tables:"))
    assert "quarter_scores" in line and "player_season_stats" in line


def test_db_report_survives_a_table_added_later():
    """A future table is picked up automatically — the drift cannot recur."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import importlib

    import db_report
    importlib.reload(db_report)
    c = db.connect(":memory:")
    c.execute("CREATE TABLE some_future_table (id INTEGER PRIMARY KEY)")
    assert "some_future_table" in db_report.all_tables(c)
    c.close()


# --------------------------------------------------------------------------
# 2. a parser upgrade is OUR edit; only the archive editing is a source change
# --------------------------------------------------------------------------

def _hist_row(con, *, hash_, parser):
    db.insert(con, "hist_odds", {
        "season": "2022-23", "game_date_et": "2022-12-31", "away": "PHI", "home": "OKC",
        "away_q": "[]", "home_q": "[]", "away_final": 110, "home_final": 120,
        "open_total": 220.0, "close_total": 221.0, "open_home_spread": -3.0,
        "close_home_spread": -3.5, "ml_away": 140, "ml_home": -165,
        "source": "sbr", "source_url": "https://example.test/x",
        "source_row_hash": hash_, "parser_version": parser,
        "captured_utc": "2026-09-21T00:00:00Z"}, replace=True)


def _names(con):
    return [r[0] for r in con.execute(
        "SELECT check_name FROM anomalies ORDER BY id")]


def test_parser_upgrade_is_not_reported_as_a_source_edit(con, monkeypatch):
    """v1 -> v2 with an unchanged archive must NOT raise `sbr-row-changed`."""
    _hist_row(con, hash_="OLDHASH", parser="1")
    monkeypatch.setattr(sbr, "SBR_PARSER_VERSION", "2")

    calls = {"logged": []}
    real = db.log_anomaly

    def spy(c, sev, check, detail):
        calls["logged"].append((sev, check, detail))
        return real(c, sev, check, detail)

    monkeypatch.setattr(db, "log_anomaly", spy)
    # Only the hash-comparison branch is under test; stub the fetch/parse so no
    # network is touched and the parsed row differs from the stored hash.
    monkeypatch.setattr(collect, "save_source_status", lambda *a, **k: None)

    class R:
        ok_body = True
        body = b"<html></html>"
        status = 200
        error = None

    monkeypatch.setattr(sbr, "fetch_season", lambda season: R())
    parsed = {"season": "2022-23", "game_date_et": "2022-12-31", "away": "PHI",
              "home": "OKC", "away_q": [], "home_q": [], "away_final": 110,
              "home_final": 120, "open_total": 220.0, "close_total": 221.0,
              "open_home_spread": -3.0, "close_home_spread": -3.5,
              "ml_away": 140, "ml_home": -165,
              "source_url": "https://example.test/x"}
    monkeypatch.setattr(sbr, "parse_season", lambda html, season: ([parsed], []))

    collect.collect_sbr_season(con, "2022-23")

    checks = [c for _, c, _ in calls["logged"]]
    assert "sbr-row-changed" not in checks
    assert "sbr-row-reparsed" in checks
    reparsed = [d for _, c, d in calls["logged"] if c == "sbr-row-reparsed"][0]
    assert reparsed["from_parser"] == "1" and reparsed["to_parser"] == "2"
    assert "archive is not known to have edited it" in reparsed["detail"]
    # the row was re-derived and now carries the parser that produced it
    row = con.execute("SELECT source_row_hash, parser_version FROM hist_odds").fetchone()
    assert row["parser_version"] == "2"
    assert row["source_row_hash"] != "OLDHASH"


def test_same_parser_hash_change_still_raises_source_edit(con, monkeypatch):
    """The check that catches real archive edits must still fire."""
    _hist_row(con, hash_="OLDHASH", parser="2")
    monkeypatch.setattr(sbr, "SBR_PARSER_VERSION", "2")
    logged = []
    real = db.log_anomaly
    monkeypatch.setattr(db, "log_anomaly",
                        lambda c, s, k, d: (logged.append(k), real(c, s, k, d))[1])
    monkeypatch.setattr(collect, "save_source_status", lambda *a, **k: None)

    class R:
        ok_body = True
        body = b"<html></html>"
        status = 200
        error = None

    monkeypatch.setattr(sbr, "fetch_season", lambda season: R())
    parsed = {"season": "2022-23", "game_date_et": "2022-12-31", "away": "PHI",
              "home": "OKC", "away_q": [], "home_q": [], "away_final": 111,  # edited
              "home_final": 120, "open_total": 220.0, "close_total": 221.0,
              "open_home_spread": -3.0, "close_home_spread": -3.5,
              "ml_away": 140, "ml_home": -165,
              "source_url": "https://example.test/x"}
    monkeypatch.setattr(sbr, "parse_season", lambda html, season: ([parsed], []))

    collect.collect_sbr_season(con, "2022-23")
    assert "sbr-row-changed" in logged
    assert "sbr-row-reparsed" not in logged
    d = [r[0] for r in con.execute(
        "SELECT detail_json FROM anomalies WHERE check_name='sbr-row-changed'")]
    assert "SAME parser" in json.loads(d[0])["detail"]


def test_reclassification_runs_once_and_never_deletes(con):
    """The 1,277 historical rows stay; a correction is appended exactly once."""
    for i in range(5):
        db.log_anomaly(con, "warn", "sbr-row-changed", {"game": f"G{i}"})
    before = con.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]

    out = collect.reclassify_sbr_parser_warnings(con)
    assert out["status"] == "recorded" and out["reclassified"] == 5
    after = con.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
    assert after == before + 1, "the append-only log must grow, never shrink"
    assert con.execute("SELECT COUNT(*) FROM anomalies "
                       "WHERE check_name='sbr-row-changed'").fetchone()[0] == 5

    again = collect.reclassify_sbr_parser_warnings(con)
    assert again["status"] == "already-done"
    assert con.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == after

    rec = con.execute("SELECT detail_json FROM anomalies "
                      "WHERE check_name='sbr-row-changed-reclassified'").fetchone()
    d = json.loads(rec[0])
    assert d["n_records"] == 5
    assert "must not be read as source edits" in d["detail"]


def test_committed_db_reclassification_numbers_match_the_log():
    """The correction record, once written, must quote the real counts."""
    if not os.path.exists(DB_PATH):
        pytest.skip("committed database not present")
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    n = c.execute("SELECT COUNT(*) c FROM anomalies "
                  "WHERE check_name='sbr-row-changed'").fetchone()["c"]
    rec = c.execute("SELECT value FROM meta "
                    "WHERE key='sbr_row_changed_reclassified'").fetchone()
    if rec is None:
        pytest.skip("reclassification has not run against the committed DB yet")
    assert json.loads(rec["value"])["n_records"] == n
    c.close()


# --------------------------------------------------------------------------
# 3. mark-to-market: observed prices only, honest UNAVAILABLE otherwise
# --------------------------------------------------------------------------

def _bet(con, **kw):
    row = {"bet_id": kw.pop("bet_id", "fo-test"), "run_id": "forward:test",
           "kind": "forward", "strategy_id": "NBA-002", "strategy_version": "1.1.0",
           "username": "PacePulsePete", "decision_utc": "2026-09-22T17:00:00Z",
           "game_id": "espn:1", "game_label": "PHI @ NYK 2026-10-20",
           "tipoff_utc": "2026-10-20T23:00:00Z", "market": "total",
           "selection": "under 231.5", "side": "under", "price": -110.0,
           "price_format": "american", "source": "line", "stake_usd": 22.81,
           "to_win_usd": 20.73, "execution_status": "simulated_fill",
           "fill_price": -110.0, "contracts": 0, "result": "pending",
           "verification": "unverified"}
    row.update(kw)
    db.insert(con, "bets", row, replace=True)
    return row


def _snap(con, market, line, ts, *, price=0.0, fmt="line", sel=None):
    db.insert(con, "odds_snapshots", {
        "game_id": "espn:1", "market": market,
        "selection": sel or (f"line {line:g}" if market == "total" else f"home {line:+g}"),
        "line": line, "price": price, "price_format": fmt,
        "source": "espn:DraftKings", "captured_utc": ts})


def test_line_only_market_never_yields_a_price(con):
    """ESPN publishes lines with price=0.0; that 0.0 must not become a mark."""
    _bet(con)  # decision_utc 2026-09-22T17:00:00Z
    _snap(con, "total", 231.5, "2026-09-22T10:00:00Z")
    _snap(con, "total", 232.5, "2026-09-22T18:00:00Z")   # moved AFTER the decision
    m = marks.mark_bet(con, con.execute("SELECT * FROM bets").fetchone())
    assert m["mark_status"] == "line-only"
    assert m["current_price"] is None, "a line is not a price"
    assert m["unrealized_usd"] is None
    assert m["line_at_decision"] == 231.5
    assert m["current_line"] == 232.5
    assert m["line_move"] == 1.0
    assert "no side PRICE has ever been observed" in m["mark_reason"]


def test_a_snapshot_exactly_at_decision_time_counts_as_available(con):
    """Boundary: `<= decision` is right; a bet can use a price it saw at t."""
    _bet(con, bet_id="fo-edge", decision_utc="2026-09-22T17:00:00Z")
    _snap(con, "total", 231.5, "2026-09-22T17:00:00Z")
    m = marks.mark_bet(con, con.execute("SELECT * FROM bets").fetchone())
    assert m["line_at_decision"] == 231.5


def test_no_captured_data_at_all_is_unavailable_not_zero(con):
    _bet(con, bet_id="fo-none")
    m = marks.mark_bet(con, con.execute("SELECT * FROM bets").fetchone())
    assert m["mark_status"] == "unavailable"
    assert m["current_price"] is None and m["current_line"] is None
    assert m["line_move"] is None and m["unrealized_usd"] is None
    assert "UNAVAILABLE, not estimated" in m["mark_reason"]


def test_kalshi_ticker_marks_the_position_and_computes_unrealized(con):
    _bet(con, bet_id="fo-k", market="kalshi:winner", selection="NYK",
         price=54.0, price_format="kalshi_cents", stake_usd=54.0,
         to_win_usd=46.0, contracts=100, market_ticker="KXNBAGAME-X-NYK")
    db.insert(con, "kalshi_candles", {"ticker": "KXNBAGAME-X-NYK", "interval": 60,
                                      "ts_utc": "2026-09-22T16:00:00Z", "open": 50,
                                      "high": 60, "low": 48, "close": 62,
                                      "volume": 10, "captured_utc": "2026-09-22T17:00:00Z"})
    m = marks.mark_bet(con, con.execute("SELECT * FROM bets").fetchone())
    assert m["mark_status"] == "marked"
    assert m["current_price"] == 62.0
    assert m["current_price_source"] == "kalshi:candle_close"
    # 100 contracts at 62c = $62.00 of value on a $54.00 stake
    assert m["unrealized_usd"] == pytest.approx(8.0)


def test_orderbook_ask_is_preferred_over_a_candle(con):
    _bet(con, bet_id="fo-k2", market="kalshi:winner", selection="NYK",
         price=54.0, price_format="kalshi_cents", stake_usd=54.0,
         to_win_usd=46.0, contracts=100, market_ticker="KXNBAGAME-Y-NYK")
    db.insert(con, "kalshi_candles", {"ticker": "KXNBAGAME-Y-NYK", "interval": 60,
                                      "ts_utc": "2026-09-22T15:00:00Z", "open": 50,
                                      "high": 60, "low": 48, "close": 62,
                                      "volume": 10, "captured_utc": "2026-09-22T16:00:00Z"})
    db.insert(con, "kalshi_orderbooks", {"ticker": "KXNBAGAME-Y-NYK",
                                         "captured_utc": "2026-09-22T17:30:00Z",
                                         "yes_bid": 57, "yes_ask": 59,
                                         "bids": "[]", "asks": "[]"})
    m = marks.mark_bet(con, con.execute("SELECT * FROM bets").fetchone())
    assert m["current_price"] == 59.0
    assert m["current_price_source"] == "kalshi:orderbook_ask"


def test_line_at_decision_is_as_of_the_decision_not_now(con):
    """Look-ahead guard: the 'then' line may not come from after the decision."""
    _bet(con, bet_id="fo-lag", decision_utc="2026-09-22T12:00:00Z")
    _snap(con, "total", 230.5, "2026-09-22T11:00:00Z")   # before decision
    _snap(con, "total", 236.5, "2026-09-22T20:00:00Z")   # after decision
    m = marks.mark_bet(con, con.execute("SELECT * FROM bets").fetchone())
    assert m["line_at_decision"] == 230.5, "must not read a later snapshot"
    assert m["current_line"] == 236.5
    assert m["line_move"] == 6.0


def test_american_unrealized_is_zero_at_the_entry_price(con):
    """A position marked at its own entry price is worth what it cost.

    The first version of this function returned -52.38 here (it dropped the
    returned stake), so this assertion is the regression guard for that.
    """
    _bet(con, bet_id="fo-us", price=-110.0, price_format="american",
         stake_usd=100.0, to_win_usd=90.91)
    b = dict(con.execute("SELECT * FROM bets").fetchone())
    at_entry = marks._unrealized(b, -110.0)
    assert at_entry == pytest.approx(0.0, abs=0.02), "entry mark must be ~0"
    # the market moves toward our side -> the mark turns positive
    assert marks._unrealized(b, -160.0) > at_entry
    # the market moves against us -> negative
    assert marks._unrealized(b, +130.0) < at_entry


def test_mark_summary_counts_are_publishable(con):
    _bet(con, bet_id="fo-a")                       # has a captured line
    _bet(con, bet_id="fo-b", game_id="espn:2")     # nothing captured at all
    _snap(con, "total", 231.5, "2026-09-22T17:00:00Z")
    s = marks.mark_summary(con)
    assert s["n_open"] == 2
    assert s["by_status"] == {"line-only": 1, "unavailable": 1}
    assert s["n_with_price_mark"] == 0
    assert s["unrealized_usd"] is None


def test_settled_bets_are_not_marked(con):
    """Only open positions get a mark; a settled bet's P&L is already final."""
    _bet(con, bet_id="fo-settled", result="win", pnl_usd=20.73)
    assert marks.mark_open_positions(con) == []
    assert marks.mark_summary(con)["n_open"] == 0


def test_positions_page_publishes_marks_and_the_reason(con, tmp_path):
    _bet(con, bet_id="fo-site")  # decision 2026-09-22T17:00:00Z
    _snap(con, "total", 231.5, "2026-09-22T10:00:00Z")
    _snap(con, "total", 233.5, "2026-09-22T18:00:00Z")
    sitegen.build_all(con, out_dir=str(tmp_path))
    html = (tmp_path / "positions.html").read_text()
    assert "Mark to market" in html
    assert "Line move" in html and "Unrealized" in html
    assert "line only" in html
    assert "UNAVAILABLE for all 1 positions" in html
    # the old false claim about orderbook marks is gone
    assert "current marks come from the latest collected orderbook snapshots" not in html
    assert "+2" in html, "the real +2.0 line move should be rendered"


def test_every_strategy_card_has_a_version_history_section(con, tmp_path):
    """Five v1.0.0 strategies used to render no version-history section at all."""
    sitegen.build_all(con, out_dir=str(tmp_path))
    html = (tmp_path / "strategies.html").read_text()
    import re
    # `<div class="strategy-card" id="...">` — the bare `id="NBA-xxx"` pattern
    # also matches inside `data-sid="NBA-xxx"` and would double every match
    parts = re.split(r'<div class="strategy-card" id="(NBA-\d+)"', html)
    ids = [parts[i] for i in range(1, len(parts), 2)]
    assert len(ids) == 27, ids
    missing = [parts[i] for i in range(1, len(parts) - 1, 2)
               if "Version history" not in parts[i + 1][:9000]]
    assert missing == []
    assert html.count("Version history") == 27


def test_content_pages_are_searchable(con, tmp_path):
    """The spec asks for a searchable, filterable site; these pages had none.

    The positions page only offers search when there is something to search, so
    an open position is seeded first — an empty book correctly renders no box.
    """
    _bet(con, bet_id="fo-search")
    sitegen.build_all(con, out_dir=str(tmp_path))
    for name in ("strategies.html", "sources.html", "research.html",
                 "positions.html", "history.html", "leaderboard.html"):
        html = (tmp_path / name).read_text()
        assert 'type="search"' in html, f"{name} has no search input"


def test_positions_page_offers_no_search_when_the_book_is_empty(tmp_path):
    c = db.connect(str(tmp_path / "empty.db"))
    sitegen.build_all(c, out_dir=str(tmp_path))
    html = (tmp_path / "positions.html").read_text()
    assert 'type="search"' not in html
    assert "No open positions" in html
    c.close()


# --------------------------------------------------------------------------
# 5. workflow: untrusted text must never reach the shell through ${{ }}
# --------------------------------------------------------------------------

WORKFLOW = os.path.join(REPO, ".github", "workflows", "collect-and-build.yml")

#: Contexts whose content comes from outside the repository's own control, or
#: from data the pipeline itself wrote. Interpolating one of these into a `run:`
#: script with ${{ }} lets it execute as shell.
UNTRUSTED_CONTEXTS = ("github.event.head_commit.message", "github.event.issue",
                      "github.event.comment", "github.event.pull_request",
                      "github.head_ref", "github.event.pages")


def _run_blocks(text: str) -> list[str]:
    """Crude but dependency-free extraction of every `run: |` block."""
    out, cur, indent = [], None, None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("run: |") or stripped.startswith("run: >"):
            indent = len(line) - len(stripped)
            cur = []
            continue
        if cur is not None:
            if not line.strip():
                cur.append(line)
                continue
            if len(line) - len(stripped) > indent:
                cur.append(line)
                continue
            out.append("\n".join(cur))
            cur = None
    if cur is not None:
        out.append("\n".join(cur))
    return out


def test_no_untrusted_context_is_interpolated_into_a_shell_script():
    """${{ }} is substituted before bash runs, so it must not carry free text.

    This is the defect that failed run 35765860793: `--message "${{
    github.event.head_commit.message }}"` put a commit body containing backticks
    straight into the shell. bash tried to execute `sbr-row-changed` as a
    command, argparse then received the message split across arguments, and the
    step exited 2. The same step holds a GITHUB_TOKEN with contents:write, so
    the failure mode available to a crafted message is code execution.
    """
    text = open(WORKFLOW).read()
    blocks = _run_blocks(text)
    assert blocks, "no run blocks found -- the extractor broke, not the workflow"
    offenders = []
    for b in blocks:
        for ctx in UNTRUSTED_CONTEXTS:
            if "${{ " + ctx in b or "${{" + ctx in b:
                offenders.append(ctx)
    assert offenders == [], f"untrusted context(s) interpolated into a run script: {offenders}"


def test_scope_step_passes_the_commit_message_through_env():
    """The safe pattern: the context lands in `env:`, the script reads $VAR."""
    text = open(WORKFLOW).read()
    assert "HEAD_COMMIT_MESSAGE: ${{ github.event.head_commit.message }}" in text
    assert '--message "$HEAD_COMMIT_MESSAGE"' in text
    assert 'env:\n          EVENT_NAME: ${{ github.event_name }}' in text


def test_run_scope_accepts_a_hostile_commit_message(tmp_path):
    """The exact shape that broke CI must parse cleanly, not exit 2."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import importlib

    import run_scope
    importlib.reload(run_scope)
    hostile = ("Integrity pass\n\n1. 1,277 `sbr-row-changed` warnings (88%) claimed\n"
               '   "every table" but hardcoded 23 of the schema\'s 28.\n'
               "   $(whoami) and \"quotes\" and `backticks` and $100 -110\n")
    out = tmp_path / "gh_output"
    out.write_text("")
    rc = run_scope.main(["--event", "push", "--message", hostile, "--out", str(out)])
    assert rc == 0
    written = out.read_text()
    assert "skip=false" in written and "probe=false" in written
    # the hostile text must not change the decision
    assert run_scope.decide("push", hostile) == {"skip": False, "probe": False}
    assert run_scope.decide("push", "[auto] collect") == {"skip": True, "probe": False}
    assert run_scope.decide("schedule", "[auto] collect") == {"skip": False, "probe": False}


def test_strategies_page_filters_are_populated_from_the_registry(tmp_path):
    """Facets come from the live registry and the evidence-based tiers."""
    if not os.path.exists(DB_PATH):
        pytest.skip("committed database not present")
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    out = tmp_path / "site"
    out.mkdir()
    sitegen.build_all(c, out_dir=str(out))
    html = (out / "strategies.html").read_text()
    for facet in ("sf_tier", "sf_cat", "sf_mkt"):
        assert f'id="{facet}"' in html
    assert "all verification tiers" in html
    # every card carries the attributes the filters read
    import re
    cards = re.findall(r'<div class="strategy-card" id="(NBA-\d+)"', html)
    assert len(cards) == 27
    assert html.count("data-tier=") == 27
    assert html.count("data-category=") == 27
    # tiers are derived from evidence in the committed DB, so `failed` is real
    tiers = set(re.findall(r'data-tier="([a-z_]+)"', html))
    assert "failed" in tiers, tiers
    c.close()
