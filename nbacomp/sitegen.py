"""Static site generator → repo root (GitHub Pages serves main:/).

Clean, fast, mobile-friendly. All numbers come from the database; when data is
missing the pages say so explicitly (never blank-styled fake content).
"""
from __future__ import annotations

import json
import os

from . import strategies as S
from .sources_registry import REGISTRY

NAV = [
    ("index.html", "Dashboard"),
    ("leaderboard.html", "Leaderboard"),
    ("strategies.html", "Strategies"),
    ("upcoming.html", "Upcoming Bets"),
    ("positions.html", "Open Positions"),
    ("history.html", "Trade History"),
    ("sources.html", "Data Sources"),
    ("research.html", "Research"),
    ("methodology.html", "Methodology"),
]


def _esc(x) -> str:
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def page(title: str, body: str, active: str = "index.html") -> str:
    links = []
    for href, label in NAV:
        cls = ' class="active"' if href == active else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    nav = "".join(links)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — NBAComp</title>
<link rel="stylesheet" href="style.css"></head>
<body>
<header><div class="brand"><a href="index.html">🏀 NBAComp</a>
<span class="tag">autonomous NBA betting research lab &amp; paper-trading competition</span></div>
<nav>{nav}</nav></header>
<main>{body}</main>
<footer>Simulated paper trading only. No real money. Data: ESPN, NBA.com/stats, Kalshi public API —
see <a href="sources.html">Data Sources</a>. Every number on this site is traceable to a logged,
timestamped source; unverified data is labeled as such.</footer>
</body></html>"""


def table(headers: list[str], rows: list[list], cls: str = "") -> str:
    if not rows:
        return "<p class='empty'>No rows yet — data pending collection.</p>"
    h = "".join(f"<th>{_esc(x)}</th>" for x in headers)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<div class='twrap'><table class='{cls}'><thead><tr>{h}</tr></thead><tbody>{trs}</tbody></table></div>"


def fmt_money(x) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}${x:,.2f}"


def fmt_pct(x) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.2f}%"


def strategy_performance(con, strategy_id: str, kind: str) -> dict:
    rows = con.execute(
        "SELECT * FROM bets WHERE strategy_id=? AND kind=? AND result IN ('win','loss','push','void')",
        (strategy_id, kind)).fetchall()
    if not rows:
        pending = con.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_id=? AND kind=? AND result='pending'",
            (strategy_id, kind)).fetchone()["c"]
        return {"bets": 0, "pending": pending}
    pnl = sum(r["pnl_usd"] or 0 for r in rows)
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    pushes = sum(1 for r in rows if r["result"] == "push")
    voids = sum(1 for r in rows if r["result"] == "void")
    decided = sum(1 for r in rows if r["result"] in ("win", "loss"))
    staked = sum(r["stake_usd"] or 0 for r in rows)
    sorted_rows = sorted(rows, key=lambda r: r["settlement_utc"] or "")
    curve, peak, mdd = 0.0, 0.0, 0.0
    pnl_series = []
    for r in sorted_rows:
        curve += r["pnl_usd"] or 0
        peak = max(peak, curve)
        mdd = max(mdd, peak - curve)
        pnl_series.append(r["pnl_usd"] or 0)
    # volatility: std of bet P&L (sample std)
    n = len(pnl_series)
    if n >= 2:
        mean = sum(pnl_series) / n
        var = sum((x - mean) ** 2 for x in pnl_series) / (n - 1)
        vol = var ** 0.5
    else:
        vol = None
    # streaks (longest winning and losing streaks, in chronological order)
    longest_win = longest_loss = cur_win = cur_loss = 0
    for r in sorted_rows:
        if r["result"] == "win":
            cur_win += 1; cur_loss = 0
            longest_win = max(longest_win, cur_win)
        elif r["result"] == "loss":
            cur_loss += 1; cur_win = 0
            longest_loss = max(longest_loss, cur_loss)
        else:
            cur_win = cur_loss = 0
    # largest win/loss
    decided_pnls = [r["pnl_usd"] for r in rows if r["result"] in ("win", "loss")]
    largest_win = max(decided_pnls) if decided_pnls else None
    largest_loss = min(decided_pnls) if decided_pnls else None
    odds = [abs(float(r["price"])) for r in rows if r["price"] is not None]
    pending = con.execute(
        "SELECT COUNT(*) c FROM bets WHERE strategy_id=? AND kind=? AND result='pending'",
        (strategy_id, kind)).fetchone()["c"]
    return {
        "bets": len(rows), "wins": wins, "losses": losses, "pushes": pushes,
        "voids": voids, "win_rate": wins / decided if decided else None,
        "loss_rate": losses / decided if decided else None,
        "pnl": round(pnl, 2), "roi": pnl / staked if staked else None,
        "staked": round(staked, 2), "max_dd": round(mdd, 2),
        "volatility": round(vol, 2) if vol is not None else None,
        "largest_win": round(largest_win, 2) if largest_win is not None else None,
        "largest_loss": round(largest_loss, 2) if largest_loss is not None else None,
        "longest_win": longest_win, "longest_loss": longest_loss,
        "avg_price": round(sum(odds) / len(odds), 1) if odds else None,
        "pending": pending,
    }


def strategy_profit_by(con, strategy_id: str, kind: str, group_by: str) -> dict[str, dict]:
    """Profit / bet count grouped by some dimension (market/team/player/month)."""
    rows = con.execute(
        "SELECT * FROM bets WHERE strategy_id=? AND kind=? AND result IN ('win','loss','push','void')",
        (strategy_id, kind)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        if group_by == "market":
            key = r["market"] or "unknown"
        elif group_by == "month":
            ts = r["settlement_utc"] or r["decision_utc"] or ""
            key = (ts[:7]) if ts else "unknown"
        elif group_by == "team":
            lab = r["game_label"] or ""
            key = lab.split("@")[1].strip().split()[0] if "@" in lab else "unknown"
        elif group_by == "player":
            sel = r["selection"] or ""
            key = sel.split()[0] if sel else "unknown"
        else:
            key = "all"
        d = out.setdefault(key, {"pnl": 0.0, "bets": 0, "wins": 0})
        d["pnl"] += r["pnl_usd"] or 0
        d["bets"] += 1
        if r["result"] == "win":
            d["wins"] += 1
    return out


def build_all(con, out_dir: str = "."):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    write_style(out_dir)
    _write_json(out_dir, "data/audit_summary.json", _audit_summary(con))
    index(con, out_dir)
    leaderboard(con, out_dir)
    strategies_page(con, out_dir)
    upcoming(con, out_dir)
    positions(con, out_dir)
    history(con, out_dir)
    sources_page(con, out_dir)
    research_page(con, out_dir)
    methodology(out_dir)


# ------------------------------------------------------------------ pieces

def _leaderboard_rows(con, kind: str) -> list[dict]:
    out = []
    for sid, meta in S.STRATEGIES.items():
        perf = strategy_performance(con, sid, kind)
        row = {
            "id": sid, "username": meta["username"], "name": meta["name"],
            "category": meta["category"], "version": meta["version"], **perf,
        }
        br_row = con.execute(
            "SELECT current FROM bankroll_events WHERE strategy_id=? ORDER BY as_of_utc DESC, id DESC LIMIT 1",
            (sid,)).fetchone()
        row["bankroll"] = br_row["current"] if br_row else S.STARTING_BANKROLL
        if perf.get("bets"):
            row["bankroll"] = round(S.STARTING_BANKROLL + perf["pnl"], 2)
        out.append(row)
    out.sort(key=lambda r: (-(r.get("pnl") or 0), -(r.get("bets") or 0)))
    return out


def _status_of(perf: dict, kind: str) -> str:
    if kind == "forward":
        if not perf.get("bets"):
            return "awaiting-opportunity"
        if perf.get("pending"):
            return "active (open bets)"
        return "active"
    return "backtested" if perf.get("bets") else "no historical data"


def index(con, out_dir):
    fwd = _leaderboard_rows(con, "forward")
    bt = _leaderboard_rows(con, "backtest")
    total_fwd_pnl = sum(r.get("pnl") or 0 for r in fwd)
    active = sum(1 for r in fwd if r.get("bets"))
    upd = con.execute("SELECT value FROM meta WHERE key='last_pipeline_utc'").fetchone()
    games = con.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
    verified = con.execute("SELECT COUNT(*) c FROM games WHERE verified=1").fetchone()["c"]
    inj = con.execute("SELECT COUNT(*) c FROM injuries").fetchone()["c"]
    kseries = con.execute("SELECT COUNT(DISTINCT series_ticker) c FROM kalshi_markets").fetchone()["c"]
    upcoming_rows = con.execute(
        "SELECT * FROM bets WHERE kind='forward' AND result='pending' ORDER BY tipoff_utc LIMIT 8").fetchall()
    recent = con.execute(
        "SELECT * FROM bets WHERE result IN ('win','loss') ORDER BY settlement_utc DESC LIMIT 8").fetchall()

    lb = table(
        ["#", "Username", "Strategy", "Bankroll", "P&L", "ROI", "Bets", "Win %", "Status"],
        [[i + 1, r["username"], f"<a href='strategies.html#{r['id']}'>{_esc(r['name'])}</a>",
          fmt_money(r["bankroll"]), fmt_money(r.get("pnl") or 0), fmt_pct(r.get("roi")),
          r.get("bets", 0),
          f"{r['win_rate'] * 100:.0f}%" if r.get("win_rate") is not None else "—",
          _status_of(r, "forward")] for i, r in enumerate(fwd[:10])])

    body = f"""
<div class="hero">
  <h1>NBA Paper-Trading Competition</h1>
  <p>{len(S.STRATEGIES)} system-generated strategies · ${S.STARTING_BANKROLL:,.0f} virtual bankroll each ·
     competition window {S.COMPETITION_START} → {S.COMPETITION_END} · primary objective: total return</p>
  <p class="muted">This is simulated paper trading on real, timestamped market data. No real money. Nothing on this
  site is betting advice.</p>
</div>
<div class="cards">
  <div class="card"><div class="k">Competition forward P&L</div><div class="v">{fmt_money(total_fwd_pnl)}</div></div>
  <div class="card"><div class="k">Strategies with live bets</div><div class="v">{active}/{len(S.STRATEGIES)}</div></div>
  <div class="card"><div class="k">Games in database</div><div class="v">{games:,} <span class="muted">({verified:,} cross-verified)</span></div></div>
  <div class="card"><div class="k">Injury listings collected</div><div class="v">{inj:,}</div></div>
  <div class="card"><div class="k">Kalshi NBA series live</div><div class="v">{kseries}</div></div>
</div>
<h2>Competition leaderboard <span class="muted">(forward paper trades)</span></h2>
{lb}
<p class="muted">Strategies with zero bets are shown as <i>awaiting-opportunity</i> — the 2026-27 season tips off in
late October 2026; strategies begin betting when verifiable prices and signals exist. Nothing is simulated before
real data is captured.</p>
<h2>Upcoming simulated bets</h2>
{table(["Strategy", "Game", "Tipoff (UTC)", "Market", "Pick", "Price", "Model prob", "Edge"],
       [[_esc(r["username"]), _esc(r["game_label"]), _esc(r["tipoff_utc"]), _esc(r["market"]),
         _esc(r["selection"]), _esc(r["price"]), f"{r['model_prob']:.3f}" if r["model_prob"] else "—",
         f"{r['edge']:+.3f}" if r["edge"] is not None else "—"] for r in upcoming_rows])}
<h2>Recently settled</h2>
{table(["Strategy", "Game", "Market", "Pick", "Result", "P&L"],
       [[_esc(r["username"]), _esc(r["game_label"]), _esc(r["market"]), _esc(r["selection"]),
         _esc(r["result"]), fmt_money(r["pnl_usd"])] for r in recent])}
<h2>Backtest snapshot <span class="muted">(separate from forward results — never mixed)</span></h2>
{table(["Strategy", "Backtest bets", "Win %", "Backtest P&L", "ROI", "Max DD"],
       [[f"<a href='strategies.html#{r['id']}'>{_esc(r['username'])}</a>", r.get("bets", 0),
         f"{r['win_rate'] * 100:.1f}%" if r.get("win_rate") is not None else "—",
         fmt_money(r.get("pnl") or 0), fmt_pct(r.get("roi")), fmt_money(r.get("max_dd") or 0)]
        for r in bt if r.get("bets")])}
<p class="muted">Last pipeline run: {_esc(upd["value"] if upd else "not yet run")}</p>
"""
    _write(out_dir, "index.html", page("Dashboard", body, "index.html"))


def leaderboard(con, out_dir):
    fwd = _leaderboard_rows(con, "forward")
    bt = _leaderboard_rows(con, "backtest")
    n_cat = len({r["category"] for r in fwd})
    games = con.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
    verified = con.execute("SELECT COUNT(*) c FROM games WHERE verified=1").fetchone()["c"]
    span = con.execute(
        "SELECT MIN(game_date_et) lo, MAX(game_date_et) hi FROM games").fetchone()
    season_sub = (f"{span['lo'][:7]} → {span['hi'][:7]} ({verified:,} cross-verified)"
                  if games and span["lo"] else "No games collected yet")
    settled = con.execute(
        "SELECT COUNT(*) c FROM bets WHERE result IN ('win','loss','push','void')").fetchone()["c"]
    markets = con.execute(
        "SELECT COUNT(DISTINCT market) c FROM bets WHERE result IN ('win','loss','push','void')"
    ).fetchone()["c"]
    wager_sub = (f"100% Immutable Ledger • {markets} Market Types" if settled
                 else "No wagers recorded yet")
    total_fwd_pnl = sum(r.get("pnl") or 0 for r in fwd)
    pending = con.execute(
        "SELECT COUNT(*) c FROM bets WHERE kind='forward' AND result='pending'").fetchone()["c"]
    leaders = [r for r in fwd if r.get("bets")]
    top = leaders[0] if leaders else None
    if top:
        top_val = f"@{_esc(top['username'])}"
        top_sub = f"PnL {fmt_money(top.get('pnl') or 0)} · ROI {fmt_pct(top.get('roi'))}"
    else:
        top_val, top_sub = "—", "No settled bets yet"
    upcoming_games = con.execute(
        "SELECT * FROM games WHERE status IS NULL OR status != 'final' "
        "ORDER BY tipoff_utc LIMIT 8").fetchall()
    upcoming_n = con.execute(
        "SELECT COUNT(*) c FROM games WHERE status IS NULL OR status != 'final'").fetchone()["c"]
    upd = con.execute("SELECT value FROM meta WHERE key='last_pipeline_utc'").fetchone()

    cats: dict[str, int] = {}
    for r in fwd:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    pills = [f"<button class='fbtn on' data-cat=''>All {n_cat} Categories</button>"]
    for c in sorted(cats):
        pills.append(f"<button class='fbtn' data-cat='{_esc(c)}'>{_esc(c)} ({cats[c]})</button>")

    top5_rows = "".join(_top5_row_html(r, i + 1) for i, r in enumerate(fwd[:5]))

    if upcoming_games:
        slate = table(
            ["Matchup", "Date / Time", "Status", "Signals"],
            [[f"<b>{_esc(g['away_team'])} @ {_esc(g['home_team'])}</b>",
              _esc(g["tipoff_utc"] or g["game_date_et"] or "TBD"),
              _esc(g["status"] or "scheduled"),
              f"{con.execute('SELECT COUNT(*) c FROM bets WHERE game_id=?', (g['game_id'],)).fetchone()['c']} bets"]
             for g in upcoming_games])
    else:
        slate = ("<p class='empty'>No upcoming games in the database yet — the slate fills in "
                 "automatically as collection captures the schedule.</p>")

    fwd_tbody = "".join(_lb_row_html(r, i + 1, "forward") for i, r in enumerate(fwd))
    tied_note = ("<p class='muted'>All strategies tied at $0.00 — no settled bets yet. "
                 "The 2026-27 season tips off in late October 2026; rows activate as "
                 "verifiable prices and signals exist.</p>" if not settled else "")

    payload = {
        "generated_utc": upd["value"] if upd else None,
        "starting_bankroll": S.STARTING_BANKROLL,
        "forward": [_lb_json(r, "forward") for r in fwd],
        "backtest": [_lb_json(r, "backtest") for r in bt],
    }
    _write_json(out_dir, "data/leaderboard.json", payload)
    script = LB_SCRIPT.replace("__LB_JSON__", json.dumps(payload))

    body = f"""
<h1>🏆 Strategy Leaderboard — {len(fwd)} Autonomous Personas</h1>
<p class="muted">Year-long autonomous NBA paper-trading competition across {len(fwd)} quantitative
personas covering {n_cat} research categories. Ranked by total forward P&amp;L (competition objective:
strongest returns). Risk metrics are displayed for context, not used for ranking. Backtest and forward
records are always separate — toggle below, never mixed.</p>
<div class="cards kpis">
  <div class="card"><div class="v">{games:,}</div><div class="k">Tracked NBA Games</div>
    <div class="s">{_esc(season_sub)}</div></div>
  <div class="card"><div class="v">{len(fwd)}</div><div class="k">Active Strategies</div>
    <div class="s">Across {n_cat} Research Categories</div></div>
  <div class="card"><div class="v">{settled:,}</div><div class="k">Settled Wagers</div>
    <div class="s">{_esc(wager_sub)}</div></div>
  <div class="card"><div class="v">{fmt_money(total_fwd_pnl)}</div><div class="k">Total Forward PnL</div>
    <div class="s">Strict Point-in-Time Fills</div></div>
  <div class="card"><div class="v">{pending}</div><div class="k">Pending Forward Bets</div>
    <div class="s">Published Before Tipoff</div></div>
  <div class="card"><div class="v small-v">{top_val}</div><div class="k">Top Strategy</div>
    <div class="s">{top_sub}</div></div>
</div>
<h2>🏆 Current Leaderboard (Top 5) <a href="#full" class="muted">View All {len(fwd)} →</a></h2>
<div class="twrap"><table><thead><tr><th>Rank</th><th>Username</th><th>Category</th>
<th>Win Rate</th><th>PnL ($)</th><th>ROI</th></tr></thead><tbody>{top5_rows}</tbody></table></div>
<h2>⚡ Upcoming Slate Pulse ({upcoming_n} Games)</h2>
{slate}
<h2 id="full">🏆 Full Competition Board</h2>
<div class="lb-controls">
  <div class="kind-toggle">
    <button id="kind_fwd" class="fbtn on">Forward (live paper)</button>
    <button id="kind_bt" class="fbtn">Backtest (historical sim)</button>
  </div>
  <input id="lb_q" type="search" placeholder="Search username, strategy, category…">
</div>
<div class="pills">{''.join(pills)}</div>
<p id="lb_count" class="muted"></p>
<div class="twrap"><table id="lb"><thead><tr>
<th data-k="rank">#</th><th data-k="username" class="sortable">Username ↕</th>
<th data-k="category" class="sortable">Category ↕</th><th data-k="version" class="sortable">Ver ↕</th>
<th data-k="pnl" class="sortable">Total PnL ($) ↕</th><th data-k="roi" class="sortable">ROI (%) ↕</th>
<th data-k="win_rate" class="sortable">Win Rate ↕</th><th data-k="bets" class="sortable">Bets ↕</th>
<th data-k="max_dd" class="sortable">Max DD ($) ↕</th><th data-k="bankroll" class="sortable">Bankroll ↕</th>
<th data-k="status" class="sortable">Status ↕</th>
</tr></thead><tbody id="lb_body">{fwd_tbody}</tbody></table></div>
{tied_note}
<h3>Forward P&L by month (competition-wide)</h3>
{_profit_by_competition(con, "forward")}
<h3>Forward P&L by strategy-category</h3>
{_profit_by_category(con, "forward")}
<p class="muted">Machine-readable board: <a href="data/leaderboard.json">data/leaderboard.json</a> ·
Last pipeline run: {_esc(upd["value"] if upd else "not yet run")}</p>
{script}
"""
    _write(out_dir, "leaderboard.html", page("Leaderboard", body, "leaderboard.html"))


def fmt_usd(x) -> str:
    if x is None:
        return "—"
    return f"${x:,.2f}"


def _top5_row_html(r: dict, rank: int) -> str:
    win = f"{r['win_rate'] * 100:.1f}%" if r.get("win_rate") is not None else "—"
    return (f"<tr><td><b>#{rank}</b></td><td>@{_esc(r['username'])}</td>"
            f"<td>{_esc(r['category'])}</td><td>{win}</td>"
            f"<td class='{_pnl_class(r)}'>{fmt_money(r.get('pnl') or 0)}</td>"
            f"<td><b>{fmt_pct(r.get('roi'))}</b></td></tr>")


def _pnl_class(r: dict) -> str:
    if not r.get("bets"):
        return ""
    return "pos" if (r.get("pnl") or 0) > 0 else ("neg" if (r.get("pnl") or 0) < 0 else "")


def _st_class(status: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in status.lower())


def _lb_row_html(r: dict, rank: int, kind: str) -> str:
    medal = f" class='medal m{rank}'" if rank <= 3 else ""
    win = f"{r['win_rate'] * 100:.1f}%" if r.get("win_rate") is not None else "—"
    st = _status_of(r, kind)
    return (
        f"<tr><td><b{medal}>#{rank}</b></td>"
        f"<td><b>@{_esc(r['username'])}</b><br><span class='muted'>{_esc(r['name'])}</span></td>"
        f"<td>{_esc(r['category'])}</td><td>v{_esc(r['version'])}</td>"
        f"<td class='{_pnl_class(r)}'>{fmt_money(r.get('pnl') or 0)}</td>"
        f"<td><b>{fmt_pct(r.get('roi'))}</b></td><td>{win}</td><td>{r.get('bets', 0)}</td>"
        f"<td>{fmt_usd(r.get('max_dd') or 0)}</td><td>{fmt_usd(r['bankroll'])}</td>"
        f"<td><span class='st st-{_st_class(st)}'>{_esc(st)}</span></td></tr>")


def _lb_json(r: dict, kind: str) -> dict:
    return {
        "id": r["id"], "username": r["username"], "name": r["name"],
        "category": r["category"], "version": r["version"],
        "pnl": r.get("pnl") or 0.0, "roi": r.get("roi"),
        "win_rate": r.get("win_rate"), "bets": r.get("bets", 0),
        "max_dd": r.get("max_dd") or 0.0, "bankroll": r["bankroll"],
        "pending": r.get("pending", 0), "status": _status_of(r, kind),
    }


LB_SCRIPT = """
<script>
const LB = __LB_JSON__;
const state = {kind: 'forward', cat: '', q: '', key: 'pnl', dir: -1};
const tb = document.getElementById('lb_body');
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function money(x) { if (x == null) return '—'; const s = '$' + Math.abs(x).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); return (x < 0 ? '-' : '+') + s; }
function pct(x) { if (x == null) return '—'; return (x >= 0 ? '+' : '') + (x * 100).toFixed(2) + '%'; }
function usd(x) { if (x == null) return '—'; return '$' + Math.abs(x).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function stClass(s) { return 'st-' + String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-'); }
function val(r, k) {
  if (k === 'rank') return 0;
  const v = r[k];
  if (v == null) return (k === 'roi' || k === 'win_rate') ? -Infinity : v;
  return v;
}
function render() {
  let rows = LB[state.kind].filter(r =>
    (!state.cat || r.category === state.cat) &&
    (!state.q || (r.username + ' ' + r.name + ' ' + r.category).toLowerCase().includes(state.q)));
  rows = rows.slice().sort((a, b) => {
    const va = val(a, state.key), vb = val(b, state.key);
    if (typeof va === 'string') return state.dir * va.localeCompare(vb);
    return state.dir * ((va > vb) - (va < vb));
  });
  document.getElementById('lb_count').textContent =
    'Showing ' + rows.length + ' of ' + LB[state.kind].length + ' strategies (' + state.kind + ')';
  tb.innerHTML = rows.map((r, i) => {
    const medal = (i < 3) ? ' class=\"medal m' + (i + 1) + '\"' : '';
    const cls = r.bets ? (r.pnl > 0 ? 'pos' : (r.pnl < 0 ? 'neg' : '')) : '';
    return '<tr><td><b' + medal + '>#' + (i + 1) + '</b></td>' +
      '<td><b>@' + esc(r.username) + '</b><br><span class=\"muted\">' + esc(r.name) + '</span></td>' +
      '<td>' + esc(r.category) + '</td><td>v' + esc(r.version) + '</td>' +
      '<td class=\"' + cls + '\">' + money(r.pnl) + '</td>' +
      '<td><b>' + pct(r.roi) + '</b></td>' +
      '<td>' + (r.win_rate == null ? '—' : (r.win_rate * 100).toFixed(1) + '%') + '</td>' +
      '<td>' + r.bets + '</td><td>' + usd(r.max_dd) + '</td>' +
      '<td>' + usd(r.bankroll) + '</td>' +
      '<td><span class=\"st ' + stClass(r.status) + '\">' + esc(r.status) + '</span></td></tr>';
  }).join('');
}
document.querySelectorAll('th.sortable').forEach(th => th.addEventListener('click', () => {
  const k = th.dataset.k;
  if (state.key === k) state.dir *= -1; else { state.key = k; state.dir = (k === 'username' || k === 'category' || k === 'status') ? 1 : -1; }
  render();
}));
document.querySelectorAll('.pills .fbtn').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.pills .fbtn').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); state.cat = b.dataset.cat; render();
}));
document.getElementById('lb_q').addEventListener('input', e => { state.q = e.target.value.toLowerCase(); render(); });
function setKind(k) {
  state.kind = k;
  document.getElementById('kind_fwd').classList.toggle('on', k === 'forward');
  document.getElementById('kind_bt').classList.toggle('on', k === 'backtest');
  render();
}
document.getElementById('kind_fwd').addEventListener('click', () => setKind('forward'));
document.getElementById('kind_bt').addEventListener('click', () => setKind('backtest'));
render();
</script>
"""


def _profit_by_competition(con, kind: str) -> str:
    rows = con.execute(
        "SELECT substr(COALESCE(settlement_utc, decision_utc), 1, 7) m, "
        "COALESCE(SUM(pnl_usd),0) p, COUNT(*) c FROM bets WHERE kind=? AND "
        "result IN ('win','loss','push','void') GROUP BY 1 ORDER BY 1",
        (kind,)).fetchall()
    if not rows:
        return "<p class='empty'>No settled bets yet.</p>"
    body = "<table><thead><tr><th>Month</th><th>P&L</th><th>Bets</th></tr></thead><tbody>"
    for r in rows:
        body += (f"<tr><td>{_esc(r['m'])}</td>"
                 f"<td>{fmt_money(r['p'])}</td>"
                 f"<td>{r['c']}</td></tr>")
    body += "</tbody></table>"
    return body


def _profit_by_category(con, kind: str) -> str:
    rows = con.execute(
        "SELECT s.category, COALESCE(SUM(b.pnl_usd),0) p, COUNT(*) c FROM bets b "
        "JOIN strategies s ON s.strategy_id=b.strategy_id "
        "WHERE b.kind=? AND b.result IN ('win','loss','push','void') GROUP BY 1 "
        "ORDER BY p DESC", (kind,)).fetchall()
    if not rows:
        return "<p class='empty'>No settled bets yet.</p>"
    body = "<table><thead><tr><th>Strategy category</th><th>P&L</th><th>Bets</th></tr></thead><tbody>"
    for r in rows:
        body += (f"<tr><td>{_esc(r['category'])}</td>"
                 f"<td>{fmt_money(r['p'])}</td>"
                 f"<td>{r['c']}</td></tr>")
    body += "</tbody></table>"
    return body


def strategies_page(con, out_dir):
    cards = []
    for sid, m in S.STRATEGIES.items():
        perf_f = strategy_performance(con, sid, "forward")
        perf_b = strategy_performance(con, sid, "backtest")
        why = _why_analysis(perf_b, perf_f)
        srcs = "".join(f"<code>{_esc(s)}</code> " for s in m["data_sources"])
        markets = "".join(f"<code>{_esc(x)}</code> " for x in m["market_types"])
        rules = "".join(f"<li>{_esc(r)}</li>" for r in m["entry_rules"])
        fails = "".join(f"<li>{_esc(f)}</li>" for f in m["failure_modes"])
        lims = "".join(f"<li>{_esc(f)}</li>" for f in m["data_limitations"])
        la = _esc(m["lookahead_controls"])
        bets = con.execute(
            "SELECT * FROM bets WHERE strategy_id=? ORDER BY decision_utc DESC LIMIT 5",
            (sid,)).fetchall()
        bt_rows = table(["Game", "Market", "Pick", "Price", "Model", "Result", "P&L"],
                        [[_esc(b["game_label"]), _esc(b["market"]), _esc(b["selection"]),
                          _esc(b["price"]), f"{b['model_prob']:.3f}" if b["model_prob"] else "—",
                          _esc(b["result"]), fmt_money(b["pnl_usd"])] for b in bets])
        # profit breakdowns
        fwd_by_market = strategy_profit_by(con, sid, "forward", "market")
        fwd_by_team = strategy_profit_by(con, sid, "forward", "team")
        fwd_by_month = strategy_profit_by(con, sid, "forward", "month")
        fwd_breakdowns = _profit_breakdown_html(fwd_by_market, "market") + \
                         _profit_breakdown_html(fwd_by_team, "team") + \
                         _profit_breakdown_html(fwd_by_month, "month")

        def perf_html(p, label):
            if not p.get("bets"):
                return (f"<div class='perf'><b>{label}</b>: no {label.split(' ')[0].lower()} bets yet"
                        f"{' — ' + _esc(p.get('note')) if p.get('note') else ''}.</div>")
            base = (f"<div class='perf'><b>{label}</b> — bets {p['bets']} · win {p['win_rate'] * 100:.1f}% · "
                    f"P&L {fmt_money(p['pnl'])} · ROI {fmt_pct(p['roi'])} · maxDD {fmt_money(p['max_dd'])}"
                    f" · pending {p['pending']}")
            base += f" · vol {fmt_money(p['volatility'])}" if p.get('volatility') is not None else ""
            base += (f" · largest W {fmt_money(p['largest_win'])} / L {fmt_money(p['largest_loss'])}"
                     f" · streaks {p['longest_win']}W / {p['longest_loss']}L")
            base += "</div>"
            return base

        cards.append(f"""
<div class="strategy-card" id="{sid}">
<h2>{_esc(m['name'])} <span class="mono">{sid} v{m['version']}</span>
<span class="pill">{_esc(m['category'])}</span>
<span class="pill status">{_status_of(perf_f, 'forward')}</span></h2>
<p class="username">plays as <b>@{_esc(m['username'])}</b></p>
<p><b>Thesis.</b> {_esc(m['thesis'])}</p>
<p><b>Rules.</b> {_esc(m['description'])}</p>
<ul class="rules">{rules}</ul>
<p><b>Markets.</b> {markets} <br><b>Data sources.</b> {srcs}</p>
<p><b>Sizing.</b> {_esc(m['sizing_rules'])} · <b>Expected edge.</b> {_esc(m['expected_edge'])}</p>
<p><b>Look-ahead controls.</b> {la}</p>
<details><summary>Failure modes</summary><ul>{fails}</ul></details>
<details><summary>Data limitations</summary><ul>{lims}</ul></details>
{perf_html(perf_b, 'Backtest (historical simulation)')}
{perf_html(perf_f, 'Forward (live paper competition)')}
<details><summary>Forward breakdowns (market / team / month)</summary>{fwd_breakdowns or "<p class='empty'>No forward bets yet.</p>"}</details>
<details><summary>Why it works / fails (auto-analysis, sample-size aware)</summary>{why}</details>
<details open><summary>Recent bets (all kinds)</summary>{bt_rows}</details>
</div>""")
    body = ("<h1>Strategies</h1>"
            "<p class='muted'>Every strategy is a versioned, testable hypothesis with explicit rules. "
            "Backtest and forward records are labeled separately and never mixed.</p>"
            + "".join(cards))
    _write(out_dir, "strategies.html", page("Strategies", body, "strategies.html"))


def _profit_breakdown_html(by: dict, dim: str) -> str:
    if not by:
        return ""
    rows = "".join(f"<tr><td>{_esc(k)}</td><td>{d.get('bets', 0)}</td>"
                   f"<td>{d.get('wins', 0)}</td>"
                   f"<td>{fmt_money(d.get('pnl', 0))}</td></tr>"
                   for k, d in sorted(by.items()))
    return ("<table><thead><tr><th>" + _esc(dim).title() +
            "</th><th>Bets</th><th>Wins</th><th>P&L</th></tr></thead><tbody>" + rows + "</tbody></table>")


def _why_analysis(bt: dict, fwd: dict) -> str:
    parts = []
    if bt.get("bets"):
        if (bt.get("pnl") or 0) > 0 and bt.get("win_rate", 0) > 0.5:
            parts.append("<b>Why it may have worked:</b> positive backtest P&L with win rate above 50% — "
                         "consistent with a real pricing gap, but the market may also adapt.")
        elif (bt.get("pnl") or 0) > 0:
            parts.append("<b>Why it may have worked:</b> positive P&L despite sub-50% win rate — "
                         "odds-driven returns; verify average price isn't doing all the work.")
        else:
            parts.append("<b>Why it may have failed:</b> negative backtest P&L — the hypothesized "
                         "edge is absent or already priced in; also check data-quality flags.")
        parts.append(f"<b>Sample:</b> {bt['bets']} backtest bets. "
                     + ("Small sample — treat as suggestive, not established." if bt["bets"] < 100
                        else "Sample size is non-trivial but season-level."))
    else:
        parts.append("<b>No backtest observations.</b> This is an explicit data limitation (no free "
                     "historical prices for these markets), not evidence either way.")
    if fwd.get("bets"):
        parts.append(f"<b>Forward so far:</b> {fwd['bets']} settled / {fwd.get('pending', 0)} pending.")
    else:
        parts.append("<b>Forward:</b> awaiting the 2026-27 season and first verifiable prices.")
    return "<p>" + "</p><p>".join(parts) + "</p>"


def upcoming(con, out_dir):
    rows = con.execute(
        "SELECT * FROM bets WHERE kind='forward' AND result='pending' ORDER BY tipoff_utc").fetchall()
    body = f"""
<h1>Upcoming simulated bets</h1>
<p class="muted">Every intended bet is published here BEFORE the game starts, with the price and model state
frozen at decision time. If this table is empty, no strategy currently sees a qualifying opportunity —
that is a result, not an outage.</p>
{table(["Strategy", "Game", "Tipoff (UTC)", "Market", "Pick", "Side", "Price (¢)", "Model prob",
        "Mkt prob", "Edge", "Stake", "To win", "Trigger", "Source ts"],
       [[_esc(r["username"]), _esc(r["game_label"]), _esc(r["tipoff_utc"]), _esc(r["market"]),
         _esc(r["selection"]), _esc(r["side"]), r["price"],
         f"{r['model_prob']:.3f}" if r["model_prob"] is not None else "—",
         f"{r['market_prob']:.3f}" if r["market_prob"] is not None else "—",
         f"{r['edge']:+.3f}" if r["edge"] is not None else "—",
         fmt_money(r["stake_usd"]), fmt_money(r["to_win_usd"]),
         f"<span class='trigger'>{_esc((r['notes'] or '')[:140])}</span>", _esc(r["source_ts"])]
        for r in rows])}
"""
    _write(out_dir, "upcoming.html", page("Upcoming Bets", body, "upcoming.html"))


def positions(con, out_dir):
    rows = con.execute(
        "SELECT * FROM bets WHERE kind='forward' AND result='pending' "
        "AND execution_status='simulated_fill' ORDER BY tipoff_utc").fetchall()
    body = f"""
<h1>Open positions</h1>
<p class="muted">Executed (simulated fill) bets awaiting settlement. Entry price and exposure are immutable
records; current marks come from the latest collected orderbook snapshots.</p>
{table(["Strategy", "Game", "Tipoff (UTC)", "Market", "Pick", "Entry ¢", "Contracts",
        "Stake", "To win", "Fees", "Entry ts"],
       [[_esc(r["username"]), _esc(r["game_label"]), _esc(r["tipoff_utc"]), _esc(r["market"]),
         _esc(r["selection"]), r["fill_price"], r["contracts"], fmt_money(r["stake_usd"]),
         fmt_money(r["to_win_usd"]), fmt_money(r["fee_usd"]), _esc(r["source_ts"])] for r in rows])}
"""
    _write(out_dir, "positions.html", page("Open Positions", body, "positions.html"))


def history(con, out_dir):
    rows = con.execute("SELECT * FROM bets ORDER BY decision_utc DESC").fetchall()
    recs = []
    for r in rows:
        recs.append({k: r[k] for k in (
            "bet_id", "kind", "run_id", "strategy_id", "username", "decision_utc", "game_id",
            "game_label", "tipoff_utc", "market", "selection", "side", "price", "price_format",
            "source", "source_ts", "model_prob", "market_prob", "edge", "stake_usd", "to_win_usd",
            "execution_status", "contracts", "fill_price", "fee_usd", "closing_price",
            "result", "settlement_utc", "settlement_source", "pnl_usd", "roi",
            "verification", "notes", "strategy_version")})
    _write_json(out_dir, "data/history.json", recs)
    body = f"""
<h1>Trade history</h1>
<p class="muted">{len(recs)} recorded bets — every simulated execution since inception, backtest and forward
labeled. Search and filter client-side; nothing is hidden, including losing strategies and flagged rows.</p>
<input id="q" type="search" placeholder="Search: strategy, team, market, result…">
<div class="filters">
  <select id="f_kind"><option value="">all kinds</option><option>forward</option><option>backtest</option></select>
  <select id="f_market"><option value="">all markets</option><option>kalshi:winner</option>
    <option>total</option><option>kalshi:prop:rebounds</option>
    <option>kalshi:prop:assists</option><option>kalshi:prop:points</option></select>
  <select id="f_result"><option value="">all results</option><option>win</option><option>loss</option>
    <option>push</option><option>pending</option></select>
  <select id="f_strat"><option value="">all strategies</option></select>
</div>
<p id="count" class="muted"></p>
<div class="twrap"><table id="hist"><thead><tr>
<th>Bet</th><th>Kind</th><th>Strategy</th><th>Decision (UTC)</th><th>Game</th><th>Market</th>
<th>Pick</th><th>Price</th><th>Closing</th><th>Model</th><th>Result</th><th>Stake</th><th>P&L</th></tr></thead>
<tbody></tbody></table></div>
<script>
const DATA = {json.dumps(recs)};
const tb = document.querySelector('#hist tbody');
const sel = document.getElementById('f_strat');
[...new Set(DATA.map(d => d.username))].sort().forEach(u => {{
  const o = document.createElement('option'); o.textContent = u; sel.appendChild(o);}});
function render() {{
  const q = document.getElementById('q').value.toLowerCase();
  const k = document.getElementById('f_kind').value;
  const rs = document.getElementById('f_result').value;
  const st = document.getElementById('f_strat').value;
  const m = document.getElementById('f_market').value;
  const rows = DATA.filter(d =>
    (!k || d.kind === k) && (!m || d.market === m) && (!rs || d.result === rs) &&
    (!st || d.username === st) &&
    (!q || JSON.stringify(d).toLowerCase().includes(q)));
  document.getElementById('count').textContent = rows.length + ' of ' + DATA.length + ' bets';
  tb.innerHTML = rows.slice(0, 400).map(d =>
    `<tr><td class="mono">${{d.bet_id}}</td><td>${{d.kind}}</td>
     <td>${{d.username}}</td><td>${{d.decision_utc}}</td>
     <td>${{d.game_label || ''}}</td><td>${{d.market}}</td><td>${{d.selection}}</td>
     <td>${{d.price}} ${{d.price_format}}</td>
     <td>${{d.closing_price == null ? '—' : (+d.closing_price).toFixed(1) + 'c'}}</td>
     <td>${{d.model_prob == null ? '—' : (+d.model_prob).toFixed(3)}}</td>
     <td class="${{d.result}}">${{d.result}}</td>
     <td>${{$}}${{(d.stake_usd || 0).toFixed(2)}}</td>
     <td class="${{(d.pnl_usd || 0) >= 0 ? 'pos' : 'neg'}}">${{d.pnl_usd == null ? '—' : (+d.pnl_usd).toFixed(2)}}</td></tr>`
  ).join('');
}}
['q', 'f_kind', 'f_result', 'f_strat', 'f_market'].forEach(id =>
  document.getElementById(id).addEventListener('input', render));
render();
</script>
"""
    _write(out_dir, "history.html", page("Trade History", body, "history.html"))


def sources_page(con, out_dir):
    rows = []
    for s in REGISTRY:
        st = con.execute("SELECT * FROM source_status WHERE source_id LIKE ?",
                         (s["id"].split(":")[0] + ":%",)).fetchone()
        rows.append([
            f"<b>{_esc(s['name'])}</b><br><span class='mono small'>{_esc(s['id'])}</span>",
            f"<a href='{_esc(s['url'])}' rel='noopener'>{_esc(s['url'])}</a>",
            _esc(s["data_type"]), _esc(s["cost"]),
            "yes" if s["registration_required"] else "no",
            "yes" if s["paid_plan_required"] else "no",
            _esc(s["rate_limits"]),
            _esc(s["reliability"]), _esc(s["last_verified"]),
            (f"<span class='ok'>OK ({st['http_status']})</span>" if st and st["ok"]
             else f"<span class='bad'>{_esc(st['detail'] or 'not checked this run')}</span>") if st
            else "<span class='muted'>checked at collection time</span>",
            _esc(s["verification_note"]),
        ])
    body = f"""
<h1>Data sources</h1>
<p class="muted">The core pipeline uses only keyless, free, public sources. Free trials and freemium tiers are
treated as NOT free. Reachability is re-verified on every collection run and shown below from the latest run.
If a source stops working mid-pipeline: <code>(1)</code> the failure is logged into
<code>collection_log</code> with HTTP status; <code>(2)</code> the affected subscriber
strategies get no signal in that run (no fallback to invented data); <code>(3)</code> the source-status row
flips to <code>ok=0</code> and is surfaced on this page; <code>(4)</code> the audit pass raises an anomaly.
The site does not depend on any single fragile endpoint.</p>
{table(["Source", "URL", "Data", "Cost", "Registration", "Paid plan", "Rate limits", "Reliability",
        "Last verified", "Latest run", "Verification notes"], rows)}
<h2>Known unavailable data (documented, not assumed)</h2>
<ul>
<li><b>Historical sportsbook closing lines (deep history):</b> no free, legal archive was found; paid archives
exist and are excluded by policy. Consequence: historical backtests run only where Kalshi candlestick history
exists; totals/spread model bets before that use clearly-labeled price assumptions (PRICED-ASSUMPTION).</li>
<li><b>Historical injury reports:</b> no free dated archive — injury strategies are forward-tested first.</li>
<li><b>Historical order-book depth:</b> Kalshi does not publish historical books; backtest entries use last
closed hourly trade price +1 tick, labeled as an execution assumption.</li>
<li><b>Referee assignments:</b> no reliable free historical feed found — referee strategies are NOT claimed.</li>
<li><b>NBA.com/stats advanced tracking data (drives, touches, etc.):</b> reachable from some networks but
blocked from GitHub Actions runners (Akamai 403 / connection tarpit). Replaced by ESPN box scores + Basketball-Reference
verification. Sister site NBAInjuryReport documented the same fingerprint mismatch.</li>
<li><b>Pre-2024-25 historical totals lines:</b> no free historical archive of sportsbook totals, so pre-2025
totals backtests are not attempted. Forecast models run forward from collection start.</li>
</ul>
"""
    _write(out_dir, "sources.html", page("Data Sources", body, "sources.html"))


def research_page(con, out_dir):
    rows = con.execute("SELECT * FROM research_log ORDER BY id").fetchall()
    items = "".join(
        f"<div class='research-item'><h3>R-{r['id']:03d} · {_esc(r['question'])}</h3>"
        f"<p><b>Date</b> {_esc(r['ts_utc'][:10])} · <b>Sources</b> {_esc(r['sources_searched'])}</p>"
        f"<p><b>Found.</b> {_esc(r['data_discovered'] or '—')}</p>"
        + (f"<p><b>Hypothesis.</b> {_esc(r['hypothesis'])}</p>" if r["hypothesis"] else "")
        + (f"<p><b>Test.</b> {_esc(r['test_performed'])}</p>" if r["test_performed"] else "")
        + (f"<p><b>Result.</b> {_esc(r['result'])}</p>" if r["result"] else "")
        + (f"<p><b>Verification.</b> {_esc(r['verification'])}</p>" if r["verification"] else "")
        + (f"<p><b>Decision.</b> {_esc(r['decision'])}</p>" if r["decision"] else "")
        + (f"<p><b>Next.</b> {_esc(r['next_steps'])}</p>" if r["next_steps"] else "")
        + "</div>" for r in rows)
    anomalies = con.execute(
        "SELECT * FROM anomalies ORDER BY detected_utc DESC LIMIT 50").fetchall()
    an = table(["When (UTC)", "Severity", "Check", "Detail"],
               [[_esc(a["detected_utc"]),
                 f"<span class='bad'>{_esc(a['severity'])}</span>" if a["severity"] == "critical"
                 else _esc(a["severity"]), _esc(a["check_name"]),
                 f"<code class='small'>{_esc(a['detail_json'][:220])}</code>"] for a in anomalies])
    body = f"""
<h1>Research log</h1>
<p class="muted">Every research question, what was searched, what was found, what was tested, and what was
decided — so research is auditable and never silently duplicated.</p>
{items or "<p class='empty'>No research entries.</p>"}
<h2>Anomaly register (latest 50)</h2>
<p class="muted">Automated checks run on every pipeline pass. Flags are shown, never silently fixed.</p>
{an}
"""
    _write(out_dir, "research.html", page("Research", body, "research.html"))


def methodology(out_dir):
    body = """
<h1>Methodology</h1>
<h2>Decision-time integrity (no look-ahead)</h2>
<ul>
<li>Every simulated bet stores <code>decision_utc</code>; model inputs (Elo ratings, rolling team stats,
rest/travel features) are built strictly from games dated before the decision.</li>
<li>Backtest prices come from the last <em>fully closed hourly</em> Kalshi candle before the decision
(+1 tick slippage). A price timestamped after the decision can never be selected — enforced in
<code>engine.PriceBook</code> and tested in the test-suite.</li>
<li>Injury adjustments use only listings published before the decision. Final injury status, final lineups,
closing prices, and game results are never used at decision time.</li>
<li>The forward engine refuses to place any bet less than 1 hour before tipoff (execution-latency guard).</li>
</ul>
<h2>Backtesting</h2>
<p>Chronological walk over verified game results. Each strategy's state carries forward; a game's result is
absorbed into the model only after its bets are placed. Settlement uses Kalshi's own recorded result when
available, cross-checked against the cross-verified final score; disagreements raise anomalies instead of
silent choices.</p>
<h2>Forward testing &amp; paper trading</h2>
<p>When historical prices don't exist (player props, sportsbook lines pre-2026-27), strategies are
forward-tested: at each scheduled collection, the captured price/injury/schedule state is frozen into a
simulated bet before tipoff, then settled from verified results. The forward ledger is the competition
leaderboard; backtest records are always displayed separately.</p>
<h2>Closing-line capture (where available)</h2>
<p>When a forward bet settles, the engine stores the last observed Kalshi candle close before tipoff in
the bet row's <code>closing_price</code> column. This is the closing-line value at the simulated decision
time; absences (no candles) are recorded as NULL, never guessed.</p>
<h2>Live / in-game betting — explicitly out of scope</h2>
<p>The current Kalshi NBA offering closes each winner market at tipoff and does not provide live in-game
prices. The forward engine therefore enforces a 1-hour pre-tip execution-latency guard and produces no
in-running decisions. Live betting research would require a different venue with observable in-game prices
and is documented as an explicit scope gap, not attempted.</p>
<h2>Overtime (OT) handling</h2>
<p>Kalshi game-winner markets are 'final-score' markets — overtime is included by exchange convention
(verified in settler behavior; tested). The system does not currently exploit any OT-specific edge
because Kalshi does not sell OT-only markets on regular-season NBA. Strategy NBA-018 OvertoneOlive is an
<em>observer</em>: it counts OT games (via box score <code>OT</code> period) so the OT-incidence rate can
be tracked against strategy outcomes, without placing OT-only bets.</p>
<h2>Execution realism</h2>
<ul>
<li>Kalshi fills: observed orderbook ask (forward) or last closed candle close +1 tick (backtest, depth
unobservable — labeled), plus Kalshi's published fee schedule: fee = ceil(0.07·C·P·(1−P)) dollars.</li>
<li>Model-vs-line totals bets (where no historical price source exists) are simulated at standard −110 and
every such bet row is labeled <i>PRICED-ASSUMPTION</i> — nothing is presented as an observed price.</li>
<li>Unknown limits (sportsbook max bets, Kalshi tier limits beyond documented rate limits) are recorded as
unknown, never invented. Kalshi order-book size caps fills at observed depth when books are captured.</li>
</ul>
<h2>P&amp;L, bankrolls, sizing</h2>
<p>Each strategy starts with a $1,000 virtual bankroll. Stake = 25% Kelly capped at 3% of current bankroll,
min $5, max 25% open exposure (enforced via the audit <code>exposure-cap-violated</code> check). Pushes return
the stake (P&amp;L 0). Kalshi push/void handling defers to the exchange's recorded result; unresolved markets
stay <i>pending</i>, never guessed.</p>
<h2>Data verification</h2>
<p>Final scores are cross-checked ESPN ↔ NBA.com; every verification (match/mismatch) is stored. Source
reachability is probed each collection run and published on the Sources page. Discrepancies raise anomalies,
are investigated in the research log, and are never silently resolved.</p>
<h2>Strategy versioning</h2>
<p>Material rule changes create a new version (e.g., NBA-001 v1.1); history is preserved — bet rows carry the
version that produced them and are never rewritten. Each bet row references both <code>strategy_id</code>
and <code>strategy_version</code> at execution time, so historical performance can always be sliced by
the version that produced it.</p>
<h2>What this site is not</h2>
<p>Not betting advice, not real money, not a guarantee of edge. It is an auditable research process: the
point is to find out, with real verified data, which hypotheses survive.</p>
"""
    _write(out_dir, "methodology.html", page("Methodology", body, "methodology.html"))


def write_style(out_dir):
    css = """
:root { --bg:#0f1216; --panel:#171c23; --ink:#e8ecf1; --muted:#9aa7b4; --line:#2a323c;
        --accent:#e8a33d; --good:#3fb96b; --bad:#e05252; --mono:ui-monospace,Menlo,monospace; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
header { padding:14px 20px; border-bottom:1px solid var(--line); background:var(--panel);
         position:sticky; top:0; z-index:5; }
.brand a { color:var(--ink); font-weight:700; font-size:18px; text-decoration:none; }
.brand .tag { color:var(--muted); margin-left:10px; font-size:12px; }
nav { margin-top:8px; display:flex; flex-wrap:wrap; gap:2px; }
nav a { color:var(--muted); text-decoration:none; padding:5px 10px; border-radius:6px; font-size:13.5px; }
nav a:hover { color:var(--ink); background:#222a34; }
nav a.active { color:var(--accent); background:#22201a; }
main { max-width:1200px; margin:0 auto; padding:20px; }
h1,h2 { font-weight:700; } h1 { font-size:26px; margin:.4em 0; } h2 { font-size:19px; margin-top:1.6em; }
a { color:var(--accent); }
.muted { color:var(--muted); font-size:13.5px; } .small { font-size:12px; }
.mono { font-family:var(--mono); font-size:12.5px; color:var(--muted); }
.hero { padding:18px 0 6px; } .hero h1 { margin:0 0 6px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:14px 0; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
.card .k { color:var(--muted); font-size:12.5px; } .card .v { font-size:22px; font-weight:700; margin-top:2px; }
.twrap { overflow-x:auto; border:1px solid var(--line); border-radius:10px; margin:10px 0; }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
th { text-align:left; color:var(--muted); font-weight:600; padding:8px 10px; border-bottom:1px solid var(--line);
     white-space:nowrap; background:var(--panel); position:sticky; top:0;}
td { padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:none; }
.win,.pos { color:var(--good); font-weight:600; } .loss,.neg,.bad { color:var(--bad); font-weight:600; }
.push,.void { color:var(--muted); }
.empty { color:var(--muted); background:var(--panel); border:1px dashed var(--line);
         padding:14px; border-radius:10px; }
.pill { display:inline-block; background:#222a34; color:var(--muted); border-radius:999px;
        padding:2px 10px; font-size:11.5px; margin-left:6px; vertical-align:middle; }
.pill.status { background:#1c2a22; color:var(--good); }
.strategy-card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
                 padding:16px 20px; margin:18px 0; }
.strategy-card h2 { margin-top:0; } .username { color:var(--muted); margin-top:-6px; }
.rules { columns:1; } .perf { background:#12161c; border:1px solid var(--line); border-radius:8px;
         padding:8px 12px; margin:8px 0; font-size:13.5px; }
details { margin:8px 0; } summary { cursor:pointer; color:var(--muted); }
.research-item { background:var(--panel); border:1px solid var(--line); border-radius:10px;
                 padding:12px 16px; margin:12px 0; }
.research-item h3 { margin:0 0 6px; font-size:15.5px; }
input[type=search], select { background:var(--panel); color:var(--ink); border:1px solid var(--line);
        border-radius:8px; padding:8px 10px; font-size:14px; margin:6px 8px 6px 0; }
#q { width:min(420px,100%); }
.trigger { color:var(--muted); font-size:12.5px; }
code { background:#12161c; border:1px solid var(--line); border-radius:5px; padding:1px 5px;
       font-size:12.5px; }
footer { border-top:1px solid var(--line); color:var(--muted); font-size:12.5px;
         padding:18px 20px; margin-top:30px; }
@media (max-width:720px){ main{padding:12px;} .card .v{font-size:18px;} th,td{padding:6px 7px;} }
/* --- competition leaderboard (NFLComp-style board) --- */
.cards.kpis { grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); }
.cards.kpis .v { font-size:26px; }
.cards.kpis .small-v { font-size:17px; word-break:break-all; }
.card .s { color:var(--muted); font-size:11.5px; margin-top:2px; }
.lb-controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:10px 0; }
.kind-toggle { display:flex; gap:6px; }
.fbtn { background:var(--panel); color:var(--muted); border:1px solid var(--line);
       border-radius:999px; padding:5px 13px; font-size:12.5px; cursor:pointer; }
.fbtn:hover { color:var(--ink); border-color:var(--accent); }
.fbtn.on { color:var(--accent); border-color:var(--accent); background:#22201a; font-weight:600; }
.pills { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 4px; }
#lb_q { flex:1; min-width:200px; }
th.sortable { cursor:pointer; user-select:none; }
th.sortable:hover { color:var(--accent); }
.medal { display:inline-block; min-width:34px; text-align:center; border-radius:6px; padding:1px 6px; }
.m1 { background:#3a2f14; color:#f2c94c; } .m2 { background:#2b3038; color:#cfd6de; }
.m3 { background:#33241a; color:#e09a5f; }
.st { display:inline-block; border-radius:999px; padding:2px 10px; font-size:11px;
     font-weight:600; white-space:nowrap; background:#222a34; color:var(--muted); }
.st-active, .st-active--open-bets- { background:#1c2a22; color:var(--good); }
.st-awaiting-opportunity { background:#232a33; color:var(--muted); }
.st-backtested { background:#1d2634; color:#6aa8e8; }
.st-no-historical-data { background:#232a33; color:var(--muted); }
"""
    _write(out_dir, "style.css", css)


def _audit_summary(con) -> dict:
    rows = con.execute(
        "SELECT severity, COUNT(*) c FROM anomalies GROUP BY severity").fetchall()
    return {r["severity"]: r["c"] for r in rows}


def _write(out_dir, name, content):
    with open(os.path.join(out_dir, name), "w") as f:
        f.write(content)


def _write_json(out_dir, name, obj):
    with open(os.path.join(out_dir, name), "w") as f:
        json.dump(obj, f, default=str)
