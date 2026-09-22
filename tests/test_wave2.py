"""Wave-2 (2026-09-21) tests: bet-integrity (append-only, aliases, immutable
decision state), new strategies (NBA-013 1H, NBA-022 rest, per-strategy
dedup), new settlement paths (1H push/void, prop win/loss/push/void,
mismatch anomaly), pace guard, new audit checks, and the outcome-only
signal-validation backtest."""
import sqlite3

import nbacomp.audit as audit
import nbacomp.db as db
import nbacomp.engine as engine
import nbacomp.model as model
import nbacomp.paper as paper
import nbacomp.signal_backtest as sb
import nbacomp.strategies as S
import pytest
from nbacomp import util


@pytest.fixture
def con(tmp_path):
    c = db.connect(str(tmp_path / "wave2.db"))
    yield c
    c.close()


def _game(c, gid, season="2025-26", tipoff="2026-01-05T00:00:00Z",
          status="final", home="BOS", away="LAL", hs=110, as_=100):
    db.insert(c, "games", {
        "game_id": gid, "source": "espn", "season": season,
        "game_date_et": tipoff[:10], "tipoff_utc": tipoff,
        "home_team": home, "away_team": away, "status": status,
        "home_score": hs, "away_score": as_,
        "captured_utc": "2026-01-05T00:01:00Z",
        "source_updated_utc": "2026-01-05T00:00:30Z", "verified": 0,
    })
    return c.execute("SELECT * FROM games WHERE game_id=?", (gid,)).fetchone()


def _box_row(i, team, date, pts, opp_pts, oreb=10, fga=85, fg3a=38, fg3m=14):
    return {
        "team": team, "game_date_et": date, "pts": pts, "opp_pts": opp_pts,
        "oreb": oreb, "dreb": None, "reb": 44, "ast": 26, "tov": 13,
        "fga": fga, "fg3a": fg3a, "fg3m": fg3m, "is_home": team == "BOS",
    }


def _ctx_total(c, g, h_roll, a_roll, line, h_rest=None, a_rest=None,
               rolling_history=None):
    return {
        "game": g, "decision": "2026-01-04T20:00:00Z",
        "elo": model.EloModel(), "home": g["home_team"], "away": g["away_team"],
        "h_roll": h_roll, "a_roll": a_roll,
        "h_rest": h_rest or {"rest_days": 3.0},
        "a_rest": a_rest or {"rest_days": 3.0},
        "rolling_history": rolling_history or {},
        "total_line": line, "total": None,
    }


# ---------------------------------------------------------------- integrity
def test_alias_resolution(con):
    _game(con, "espn:a")
    _game(con, "espn:b", hs=120, as_=110)
    db.insert(con, "game_aliases", {"old_game_id": "espn:a", "new_game_id": "espn:b",
                                    "reason": "same game re-keyed",
                                    "created_utc": "2026-01-05T00:02:00Z"})
    assert engine.resolve_game_id(con, "espn:a") == "espn:b"
    assert engine.resolve_game_id(con, "espn:b") == "espn:b"
    assert engine.resolve_game_id(con, "espn:c") == "espn:c"


def test_injury_dedupe_semantics(con):
    base = dict(player="P. Player", team="BOS", status="out",
                source="espn:injuries", note="hamstring", source_url="u1")
    db.insert(con, "injuries", dict(base,
                                    published_utc="2026-01-01T10:00:00Z",
                                    captured_utc="2026-01-01T10:05:00Z"))
    # different published ts -> distinct natural key -> distinct row
    db.insert(con, "injuries", dict(base,
                                    published_utc="2026-01-02T10:00:00Z",
                                    captured_utc="2026-01-02T10:05:00Z"))
    # exact natural-key duplicate -> ignored (append-only)
    db.insert(con, "injuries", dict(base,
                                    published_utc="2026-01-01T10:00:00Z",
                                    captured_utc="2026-01-01T10:05:00Z"))
    n = con.execute("SELECT COUNT(*) c FROM injuries WHERE player='P. Player'").fetchone()["c"]
    assert n == 2


def test_bets_v2_immutable_columns(con):
    _game(con, "espn:a", status="scheduled", hs=None, as_=None)
    db.insert(con, "bets", {
        "bet_id": "b-imm", "run_id": "forward", "strategy_id": "NBA-002",
        "strategy_version": "1.0.0", "username": "PacePunter",
        "kind": "forward", "game_id": "espn:a", "market": "total",
        "selection": "over 220.5", "side": "over", "price": -110,
        "price_format": "american", "stake_usd": 25.0,
        "execution_status": "simulated_fill",
        "decision_utc": "2026-01-04T20:00:00Z", "result": "pending",
        "strike": 220.5, "market_ticker": "T1", "prop_player": "X",
        "verification": "test", "source": "test"})
    con.execute("UPDATE bets SET result='win' WHERE bet_id='b-imm'")  # allowed
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE bets SET strike=230.5 WHERE bet_id='b-imm'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE bets SET market_ticker='X' WHERE bet_id='b-imm'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE bets SET prop_player='Y' WHERE bet_id='b-imm'")


def _promote_to_price_verified(con, strategy_id="NBA-002"):
    """See tests/test_core.py: makes a strategy tier `price_verified`."""
    _game(con, "espn:tier", status="final", hs=110, as_=105)
    db.insert(con, "bets", {
        "bet_id": f"bt-tier-{strategy_id}", "run_id": "bt-tier", "kind": "backtest",
        "strategy_id": strategy_id, "strategy_version": "1.0.0", "username": "x",
        "decision_utc": "2025-12-31T20:00:00Z", "game_id": "espn:tier",
        "game_label": "t", "tipoff_utc": "2026-01-01T00:00:00Z", "market": "ml",
        "selection": "over 220.5", "side": "over", "price": -110,
        "price_format": "american", "source": "t", "stake_usd": 10.0,
        "execution_status": "simulated_fill", "result": "win", "verification": "t"})


def test_total_bet_append_only_dedup(con):
    g = _game(con, "espn:d", status="scheduled", hs=None, as_=None)
    _promote_to_price_verified(con)
    db.insert(con, "odds_snapshots", {
        "game_id": "espn:d", "captured_utc": "2026-01-04T20:00:00Z",
        "source": "espn:consensus", "market": "total", "selection": "line 220.5",
        "line": 220.5, "price": 0, "price_format": "line", "source_url": "x"})
    sig = S.Signal(strategy_id="NBA-002", game_id="espn:d", market="total",
                   selection="over 220.5", side="over", price=-110,
                   price_format="american", source="espn:line",
                   model_prob=0.6, market_prob=0.524, trigger="t",
                   game_label="T", tipoff_utc="2026-01-05T00:00:00Z")
    ctx = {"game": g, "decision": "2026-01-04T20:00:00Z",
           "home": "BOS", "away": "LAL"}
    assert paper._place_total_bet(con, ctx, sig, info=None) == 1
    assert paper._place_total_bet(con, ctx, sig, info=None) == 0  # dedup, no rewrite
    assert con.execute("SELECT COUNT(*) c FROM bets WHERE market='total'").fetchone()["c"] == 1


# ------------------------------------------------------- new strategy math
def _rolls(h_pts=118, a_pts=102, h_opp=106, a_opp=118, h3m=15, a3m=11):
    roll = model.RollingTeamState()
    for i in range(10):
        d = f"2025-12-{i+1:02d}"
        roll.add_game(_box_row(i, "BOS", d, h_pts, h_opp, fg3m=h3m))
        roll.add_game(_box_row(i, "LAL", d, a_pts, a_opp, fg3m=a3m))
    return roll.team_rolling("BOS"), roll.team_rolling("LAL")


def test_total_signals_pace_fires(con):
    g = _game(con, "espn:m", status="scheduled", hs=None, as_=None)
    h, a = _rolls()  # high-scoring BOS vs low-scoring LAL
    exp = model.expected_total(h, a)
    line = round(exp - 10.0, 1)  # line clearly below model -> OVER
    ctx = _ctx_total(con, g, h, a, line)
    sigs = paper._total_signals(con, ctx)
    ids = [x.strategy_id for x in sigs]
    assert "NBA-002" in ids


def test_total_signals_rest_advantage(con):
    """NBA-022: 3 rest days vs 1 adds +4 to the model total for the OVER."""
    g = _game(con, "espn:r", status="scheduled", hs=None, as_=None)
    # balanced teams (no NBA-002 pace fire) but rested home vs 1-day away
    roll = model.RollingTeamState()
    for i in range(10):
        d = f"2025-12-{i+1:02d}"
        roll.add_game(_box_row(i, "BOS", d, 110, 106, fg3m=13))
        roll.add_game(_box_row(i, "LAL", d, 108, 112, fg3m=13))
    h, a = roll.team_rolling("BOS"), roll.team_rolling("LAL")
    exp = model.expected_total(h, a)
    line = round(exp - 6.0, 1)  # NBA-002/011 stay silent (diff 6 < 8), rest fires
    ctx = _ctx_total(con, g, h, a, line,
                     h_rest={"rest_days": 3.0}, a_rest={"rest_days": 1.0})
    sigs = paper._total_signals(con, ctx)
    rest = [s for s in sigs if s.strategy_id == "NBA-022"]
    assert len(rest) == 1
    assert rest[0].side == "over" and "rest" in rest[0].trigger


def test_dedup_per_strategy_no_self_hedge():
    """Per-strategy dedup: two signals from the same strategy in the same
    game+market (e.g. NBA-010 hot-home under AND cold-away over) collapse
    to the single stronger-edge signal; other strategies are untouched."""
    def mk(sid, side, prob, mkt=0.524, gid="espn:dd"):
        return S.Signal(strategy_id=sid, game_id=gid, market="total",
                        selection=f"{side} 220.5", side=side, price=-110,
                        price_format="american", source="t",
                        model_prob=prob, market_prob=mkt, trigger="t",
                        game_label="T", tipoff_utc="2026-01-05T00:00:00Z")
    under = mk("NBA-010", "under", 0.56)   # edge +0.036
    over = mk("NBA-010", "over", 0.60)     # edge +0.076 (stronger)
    pace = mk("NBA-002", "over", 0.62)     # different strategy: kept
    out = paper._dedup_per_strategy([under, over, pace])
    kept = {(x.strategy_id, x.side) for x in out}
    assert kept == {("NBA-010", "over"), ("NBA-002", "over")}
    # different game = no dedup
    out2 = paper._dedup_per_strategy(
        [mk("NBA-010", "under", 0.56), mk("NBA-010", "under", 0.58, gid="espn:ee")])
    assert len(out2) == 2


def test_half_hour_signal_requires_live_ask(con):
    g = _game(con, "espn:x", status="scheduled", hs=None, as_=None)
    info = engine.KalshiMarketInfo(
        ticker="KXNBA1H261105101", event_ticker="KXNBAGAME261105",
        series_ticker="KXNBA1H", market_type="1h", game_id="espn:x",
        home="BOS", away="LAL", team="BOS")
    elo = model.EloModel()
    for _ in range(4):
        elo.update("BOS", "LAL", 122, 102)
    ctx = {"game": g, "decision": "2026-01-04T20:00:00Z", "home": "BOS",
           "away": "LAL", "half": info, "elo": elo,
           "book": engine.PriceBook(con)}
    # no orderbook observation -> no bet (cannot price)
    assert paper._half_hour_signals(con, ctx) == 0
    # with a live ask of 72 and a strong BOS margin, YES gets the 4pp edge
    db.insert(con, "kalshi_orderbooks", {
        "ticker": "KXNBA1H261105101", "captured_utc": "2026-01-04T20:00:00Z",
        "yes_bid": 68, "yes_ask": 72, "bids": "[]", "asks": "[[72,100]]"})
    ctx["book"] = engine.PriceBook(con)
    n = paper._half_hour_signals(con, ctx)
    assert n == 1
    row = con.execute("SELECT * FROM bets WHERE market='kalshi:1h'").fetchone()
    assert row["market_ticker"] == "KXNBA1H261105101"
    assert row["selection"] == "BOS leads at half"
    assert row["side"] == "yes"
    # The traded probability is the CALIBRATED one (model blended toward the
    # market by the tier's shrink weight, 2026-09-21). NBA-013 is forward-only
    # -> shrink 0.60, so 0.4 * model + 0.6 * 0.72 sits below the raw model
    # probability but still clearly above the market ask.
    assert 0.72 < row["model_prob"] < 0.80
    assert row["model_prob"] < 0.78  # strictly below the raw model prob
    assert "policy: raw p=" in row["notes"]  # calibration is on the record
    assert row["strike"] is None  # 1H market has no numeric strike


# ------------------------------------------------------------- settlement
def _mk1h_bet(con, tipoff="2026-01-05T00:00:00Z", side="yes",
              sel="BOS leads at half"):
    return db.insert(con, "bets", {
        "bet_id": "b-1h", "run_id": "forward", "strategy_id": "NBA-013",
        "strategy_version": "1.1.0", "username": "HalfHour",
        "kind": "forward", "game_id": "espn:a", "market": "kalshi:1h",
        "selection": sel, "side": side, "price": 72,
        "price_format": "kalshi_cents", "stake_usd": 24.0,
        "contracts": 33, "fill_price": 72, "execution_status": "simulated_fill",
        "decision_utc": "2026-01-04T20:00:00Z", "model_prob": 0.84,
        "market_prob": 0.72, "edge": 0.12, "game_label": "T",
        "tipoff_utc": tipoff, "result": "pending", "verification": "test",
        "source": "test", "market_ticker": "KXNBA1H261105101"})


def test_settle_1h_from_quarter_scores(con):
    _game(con, "espn:a", hs=110, as_=100)
    _mk1h_bet(con, side="yes", sel="BOS leads at half")  # BOS is home
    db.insert(con, "quarter_scores", {
        "game_id": "espn:a", "quarter": 1, "home_score": 40, "away_score": 50,
        "source": "espn", "captured_utc": "2026-01-05T00:12:00Z"})
    db.insert(con, "quarter_scores", {
        "game_id": "espn:a", "quarter": 2, "home_score": 75, "away_score": 60,
        "source": "espn", "captured_utc": "2026-01-05T00:32:00Z"})
    assert paper.settle_finished(con) == 1
    r = con.execute("SELECT * FROM bets WHERE bet_id='b-1h'").fetchone()
    assert r["result"] == "win"
    assert "Q1+Q2" in r["settlement_source"]
    assert paper._bankroll(con, "NBA-013") == pytest.approx(
        1000 + engine.kalshi_bet_pnl(33, 72, "win"))


def test_settle_1h_tied_half_is_push(con):
    _game(con, "espn:a", hs=110, as_=100)
    _mk1h_bet(con, side="yes", sel="BOS leads at half")
    db.insert(con, "quarter_scores", {
        "game_id": "espn:a", "quarter": 2, "home_score": 62, "away_score": 62,
        "source": "espn", "captured_utc": "2026-01-05T00:32:00Z"})
    assert paper.settle_finished(con) == 1
    r = con.execute("SELECT * FROM bets WHERE bet_id='b-1h'").fetchone()
    assert r["result"] == "push" and r["pnl_usd"] == 0.0
    assert "tied half" in r["settlement_source"]


def test_settle_1h_kalshi_result_beats_scores(con):
    """The Kalshi recorded result is the settlement source of record;
    captured scores are the fallback."""
    _game(con, "espn:a", hs=110, as_=100)
    _mk1h_bet(con, side="yes", sel="BOS leads at half")
    db.insert(con, "kalshi_markets", {
        "series_ticker": "KXNBA1H", "event_ticker": "KXNBAGAME261105",
        "ticker": "KXNBA1H261105101",
        "title": "Boston Celtics lead at half", "yes_ask": 72,
        "captured_utc": "2026-01-05T02:00:00Z", "result": "yes"})
    # (no quarter_scores rows at all)
    assert paper.settle_finished(con) == 1
    r = con.execute("SELECT * FROM bets WHERE bet_id='b-1h'").fetchone()
    assert r["result"] == "win" and "kalshi result" in r["settlement_source"]


def test_settle_1h_void_when_stale(con):
    _game(con, "espn:a", tipoff="2025-12-01T00:00:00Z", hs=110, as_=100)
    _mk1h_bet(con, tipoff="2025-12-01T00:00:00Z")  # >48h old; no data at all
    assert paper.settle_finished(con) == 1
    r = con.execute("SELECT * FROM bets WHERE bet_id='b-1h'").fetchone()
    assert r["result"] == "void" and r["pnl_usd"] == 0.0
    assert "void" in r["settlement_source"]
    assert con.execute(
        "SELECT COUNT(*) c FROM anomalies WHERE check_name='unresolvable-1h-settlement'"
    ).fetchone()["c"] >= 1
    assert paper._bankroll(con, "NBA-013") == 1000.0  # stake returned


def test_settle_1h_pending_when_recent(con):
    # Relative to now: a hard-coded "recent" timestamp silently becomes stale
    # 48h later and this test then asserted the opposite of its own name
    # (found 2026-09-22T00:00Z, exactly 48h after the fixed date it used).
    recent = util.to_iso(util.utcnow() - util.timedelta(hours=2))
    _game(con, "espn:a", tipoff=recent, hs=110, as_=100)
    _mk1h_bet(con, tipoff=recent)
    assert paper.settle_finished(con) == 0
    assert con.execute(
        "SELECT result FROM bets WHERE bet_id='b-1h'").fetchone()["result"] == "pending"


def test_settle_1h_uses_alias(con):
    _game(con, "espn:a", hs=110, as_=100)
    _game(con, "espn:a2", hs=110, as_=100)
    db.insert(con, "game_aliases", {"old_game_id": "espn:a", "new_game_id": "espn:a2",
                                    "reason": "test",
                                    "created_utc": "2026-01-05T00:02:00Z"})
    db.insert(con, "quarter_scores", {
        "game_id": "espn:a2", "quarter": 2, "home_score": 80, "away_score": 50,
        "source": "espn", "captured_utc": "2026-01-05T00:32:00Z"})
    _mk1h_bet(con, side="yes", sel="BOS leads at half")  # bet.game_id = espn:a
    assert paper.settle_finished(con) == 1
    assert con.execute("SELECT result FROM bets WHERE bet_id='b-1h'").fetchone()["result"] == "win"


def _mk_prop_bet(con, market, strike, player="Test Guy", side="over"):
    return db.insert(con, "bets", {
        "bet_id": "b-prop", "run_id": "forward", "strategy_id": "NBA-015",
        "strategy_version": "1.0.0", "username": "PropHunter",
        "kind": "forward", "game_id": "espn:a", "market": market,
        "selection": f"Test Guy over {strike}", "side": side,
        "price": 60, "price_format": "kalshi_cents", "stake_usd": 30.0,
        "contracts": 50, "fill_price": 60, "execution_status": "simulated_fill",
        "decision_utc": "2026-01-04T20:00:00Z", "model_prob": 0.6,
        "market_prob": 0.6, "edge": 0.0, "game_label": "T",
        "tipoff_utc": "2026-01-05T00:00:00Z", "result": "pending",
        "verification": "test", "source": "test", "strike": strike,
        "market_ticker": "KXNBAPTS261105TST", "prop_player": player})


def _box(con, pts):
    db.insert(con, "player_gamelogs", {
        "game_id": "espn:a", "season": "2025-26", "team": "BOS",
        "player_id": "00000010", "player": "Test Guy", "pts": pts, "reb": 8,
        "ast": 5, "stl": 1, "blk": 0, "minutes": 34, "source": "espn",
        "game_date_et": "2026-01-04",
        "captured_utc": "2026-01-05T03:00:00Z"})


def test_settle_prop_win_loss_push(con):
    _game(con, "espn:a")
    _box(con, 30)
    cases = [("b-p1", 25.5, "over", "win"),      # 30 > 25.5
             ("b-p2", 30.0, "over", "push"),     # exact line
             ("b-p3", 32.5, "over", "loss"),     # 30 < 32.5
             ("b-p4", 32.5, "under", "win")]     # under side wins
    for bid, strike, side, want in cases:
        db.insert(con, "bets", {**{
            "run_id": "forward", "strategy_id": "NBA-015",
            "strategy_version": "1.0.0", "username": "PropHunter",
            "kind": "forward", "game_id": "espn:a",
            "market": "kalshi:prop:points",
            "selection": f"Test Guy over {strike}", "side": side,
            "price": 60, "price_format": "kalshi_cents", "stake_usd": 30.0,
            "contracts": 50, "fill_price": 60,
            "execution_status": "simulated_fill",
            "decision_utc": "2026-01-04T20:00:00Z", "model_prob": 0.6,
            "market_prob": 0.6, "edge": 0.0, "game_label": "T",
            "tipoff_utc": "2026-01-05T00:00:00Z", "result": "pending",
            "verification": "test", "source": "test", "strike": strike,
            "market_ticker": "KXNBAPTS261105TST", "prop_player": "Test Guy",
        }, "bet_id": bid})
    assert paper.settle_finished(con) == 4
    got = {r["bet_id"]: r["result"]
           for r in con.execute("SELECT bet_id, result FROM bets "
                                "WHERE bet_id LIKE 'b-p%'")}
    assert {b: r for b, r in got.items()} ==         {"b-p1": "win", "b-p2": "push", "b-p3": "loss", "b-p4": "win"}
    assert "box score" in con.execute(
        "SELECT settlement_source FROM bets WHERE bet_id='b-p1'").fetchone()[0]
    assert "exact line" in con.execute(
        "SELECT settlement_source FROM bets WHERE bet_id='b-p2'").fetchone()[0]


def test_settle_prop_kalshi_mismatch_records_anomaly(con):
    _game(con, "espn:a")
    _box(con, 30)
    db.insert(con, "kalshi_markets", {
        "series_ticker": "KXNBAPTS", "event_ticker": "KXNBAGAME261105",
        "ticker": "KXNBAPTS261105TST",
        "title": "Test Guy points over 20.5", "yes_ask": 70,
        "captured_utc": "2026-01-05T02:00:00Z", "result": "no"})  # kalshi: under...
    _mk_prop_bet(con, "kalshi:prop:points", 20.5, side="over")  # box: over
    assert paper.settle_finished(con) == 1
    r = con.execute("SELECT * FROM bets WHERE bet_id='b-prop'").fetchone()
    assert r["result"] == "win"  # verified box score is the truth
    assert con.execute(
        "SELECT COUNT(*) c FROM anomalies WHERE check_name='prop-settlement-mismatch'"
    ).fetchone()["c"] == 1


def test_settle_prop_void_when_no_boxscore(con):
    _game(con, "espn:a", tipoff="2025-12-01T00:00:00Z")
    _mk_prop_bet(con, "kalshi:prop:points", 25.5)  # stale (>48h), no box row
    assert paper.settle_finished(con) == 1
    r = con.execute("SELECT * FROM bets WHERE bet_id='b-prop'").fetchone()
    assert r["result"] == "void" and r["pnl_usd"] == 0.0
    assert con.execute(
        "SELECT COUNT(*) c FROM anomalies WHERE check_name='prop-unresolvable'"
    ).fetchone()["c"] == 1


def test_settle_winner_via_alias(con):
    _game(con, "espn:a", hs=115, as_=100)  # home BOS wins
    _game(con, "espn:a2", hs=115, as_=100)
    db.insert(con, "game_aliases", {"old_game_id": "espn:a", "new_game_id": "espn:a2",
                                    "reason": "test",
                                    "created_utc": "2026-01-05T00:02:00Z"})
    db.insert(con, "bets", {
        "bet_id": "b-win", "run_id": "forward", "strategy_id": "NBA-001",
        "strategy_version": "1.0.0", "username": "EloEdge",
        "kind": "forward", "game_id": "espn:a", "market": "kalshi:winner",
        "selection": "Boston Celtics", "side": "home", "price": 60,
        "price_format": "kalshi_cents", "stake_usd": 30.0, "contracts": 50,
        "fill_price": 60, "execution_status": "simulated_fill",
        "decision_utc": "2026-01-04T20:00:00Z", "model_prob": 0.6,
        "market_prob": 0.6, "edge": 0.0, "game_label": "T",
        "tipoff_utc": "2026-01-05T00:00:00Z", "result": "pending",
        "verification": "test", "source": "test"})
    assert paper.settle_finished(con) == 1
    r = con.execute("SELECT * FROM bets WHERE bet_id='b-win'").fetchone()
    assert r["result"] == "win"


# ------------------------------------------------------------------ model
def test_pace_unavailable_when_oreb_missing():
    roll = model.RollingTeamState()
    for i in range(8):
        d = f"2025-11-{i+1:02d}"
        roll.add_game(_box_row(i, "BOS", d, 110, 100, oreb=None))
    r = roll.team_rolling("BOS")
    assert r["pace"] is None and r["pace_available"] is False
    assert r["pts"] is not None  # other features still computed


def test_pace_available_when_oreb_present():
    roll = model.RollingTeamState()
    for i in range(8):
        d = f"2025-11-{i+1:02d}"
        roll.add_game(_box_row(i, "BOS", d, 110, 100, oreb=11))
    r = roll.team_rolling("BOS")
    assert r["pace_available"] is True and r["pace"] is not None
    assert r["dreb"] is None  # never fabricated from a missing source


# ------------------------------------------------------------------ audit
def test_audit_future_dated_gamelogs(con):
    db.insert(con, "player_gamelogs", {
        "game_id": "espn:a", "season": "2025-26", "team": "BOS",
        "player_id": "00000001", "player": "Future Man", "pts": 10,
        "reb": 3, "ast": 2, "stl": 0, "blk": 0, "minutes": 20,
        "source": "espn", "game_date_et": "2999-01-01",
        "captured_utc": "2026-09-21T00:00:00Z"})
    audit.run_checks(con)
    r = con.execute("SELECT severity FROM anomalies "
                    "WHERE check_name='future-dated-gamelogs'").fetchone()
    assert r is not None and r["severity"] == "critical"


def test_audit_impossible_stat(con):
    db.insert(con, "player_gamelogs", {
        "game_id": "espn:a", "season": "2025-26", "team": "BOS",
        "player_id": "00000002", "player": "Big Man", "pts": 120, "reb": 5,
        "ast": 2, "stl": 0, "blk": 0, "minutes": 20, "source": "espn",
        "game_date_et": "2026-01-04",
        "captured_utc": "2026-09-21T00:00:00Z"})
    audit.run_checks(con)
    assert con.execute(
        "SELECT COUNT(*) c FROM anomalies WHERE check_name='impossible-player-stat'"
    ).fetchone()["c"] >= 1


def test_audit_duplicate_injury_listing(con):
    for pub, cap in (("2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z"),
                     ("2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z")):
        db.insert(con, "injuries", {
            "player": "D. Up", "team": "LAL", "status": "out",
            "published_utc": pub, "captured_utc": cap,
            "source": "espn:injuries", "note": None, "source_url": "u"})
    audit.run_checks(con)
    assert con.execute(
        "SELECT COUNT(*) c FROM anomalies WHERE check_name='duplicate-injury-listing'"
    ).fetchone()["c"] >= 1


def test_audit_injury_listed_after_own_game(con):
    _game(con, "espn:g", tipoff="2026-01-01T00:00:00Z", home="LAL", away="BOS")
    db.insert(con, "injuries", {
        "player": "Late Guy", "team": "LAL", "status": "out",
        "published_utc": "2026-01-01T12:00:00Z",  # after LAL's game
        "captured_utc": "2026-01-01T12:05:00Z", "source": "espn:injuries",
        "note": None, "source_url": "u"})
    audit.run_checks(con)
    assert con.execute(
        "SELECT COUNT(*) c FROM anomalies WHERE check_name='injury-listed-after-own-game'"
    ).fetchone()["c"] >= 1


# ---------------------------------------------------------- signal backtest
def _two_team_season(con, season="2024-25", n=12):
    """BOS/LAL alternating venues, daily games. BOS wins i<8 by 5, except
    game 8 which is a 19-pt LAL blowout win (feeds the blowout-bounce rule),
    then LAL wins i in 9..n by 3. Box scores are stored so rolling/pace
    features are available. Deterministic and auditable."""
    for i in range(n):
        date = f"2024-11-{i+1:02d}"
        home, away = ("BOS", "LAL") if i % 2 == 0 else ("LAL", "BOS")
        if i == 8:
            hs, as_ = (90, 109) if home == "BOS" else (109, 90)
        elif i < 8:
            hs, as_ = (110, 105) if home == "BOS" else (105, 110)
        else:
            hs, as_ = (105, 108) if home == "BOS" else (108, 105)
        gid = f"sig:{season}:{i}"
        _game(con, gid, season=season, tipoff=f"{date}T00:00:00Z",
              home=home, away=away, hs=hs, as_=as_)
        for team, pts, opp, is_home in ((home, hs, as_, 1), (away, as_, hs, 0)):
            db.insert(con, "team_gamelogs", {
                "season": season, "game_id": gid, "game_date_et": date,
                "team": team, "opp": (away if is_home else home),
                "is_home": is_home, "pts": pts, "opp_pts": opp,
                "wl": "W" if pts > opp else "L",
                "fga": 85, "fg3a": 38, "fg3m": 14, "oreb": 10, "fta": 12,
                "reb": 44, "ast": 26, "tov": 13, "source": "espn",
                "captured_utc": f"{date}T03:00:00Z"})


def test_signal_backtest_rows_and_idempotency(con):
    _two_team_season(con)
    out1 = sb.run_signal_backtest(con, run_id="t1")
    assert out1["games"] == 12
    n1 = con.execute("SELECT COUNT(*) c FROM signal_backtests").fetchone()["c"]
    assert n1 > 0
    out2 = sb.run_signal_backtest(con, run_id="t1")  # idempotent
    n2 = con.execute("SELECT COUNT(*) c FROM signal_backtests").fetchone()["c"]
    assert n1 == n2
    # a new season appends new decisions without touching the old ones
    _two_team_season(con, season="2025-26")
    out3 = sb.run_signal_backtest(con, run_id="t1")
    assert out3["games"] == 24
    s = con.execute(
        "SELECT COUNT(*) c FROM signal_backtest_summary WHERE season='ALL'"
    ).fetchone()["c"]
    assert s >= 3  # at least NBA-003 (elo), NBA-021 (streak), NBA-002 (total)
    # blowout-bounce fired once (BOS blown out by 19 at game 8)
    r = con.execute("SELECT n_signals FROM signal_backtest_summary "
                    "WHERE season='ALL' AND strategy_id='NBA-020'").fetchone()
    assert r is not None and r["n_signals"] >= 1
    # streak-skeptic fired during BOS's 7-game streak (games 4..7)
    r = con.execute("SELECT n_signals FROM signal_backtest_summary "
                    "WHERE season='ALL' AND strategy_id='NBA-021'").fetchone()
    assert r is not None and r["n_signals"] >= 3


def test_signal_backtest_no_leakage(con):
    """The signal set for game i must be identical whether or not game i+1
    exists in the database (future results must not change past decisions)."""
    _two_team_season(con, n=12)
    sb.run_signal_backtest(con, run_id="t2")
    before = {r["game_id"]: (r["selection"], r["model_prob"], r["market"])
              for r in con.execute(
                  "SELECT game_id, selection, model_prob, market "
                  "FROM signal_backtests WHERE run_id='t2'")}
    # now add one more game and re-run: earlier decisions must be unchanged
    _two_team_season(con, season="2024-25", n=13)  # re-inserts 0..11 (ignored), adds 12
    sb.run_signal_backtest(con, run_id="t2")
    after = {r["game_id"]: (r["selection"], r["model_prob"], r["market"])
             for r in con.execute(
                 "SELECT game_id, selection, model_prob, market "
                 "FROM signal_backtests WHERE run_id='t2'")}
    for gid, sig in before.items():
        assert after.get(gid) == sig, f"decision for {gid} changed: {sig} -> {after.get(gid)}"
