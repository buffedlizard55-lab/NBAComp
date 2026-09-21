"""Pipeline runner: register strategies → backtest → forward paper engine →
settlement → audit → site build. Single entrypoint used locally and in CI."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import (audit, backtest, db, hist_backtest, paper,  # noqa: E402
                     signal_backtest, sitegen, strategies as S, util, validation)

BACKTEST_SEASONS = ["2023-24", "2024-25", "2025-26"]


def register_strategies(con):
    ts = util.utcnow_iso()

    def j(v):
        return v if isinstance(v, str) else json.dumps(v)

    for sid, m in S.STRATEGIES.items():
        db.insert(con, "strategies", {
            "strategy_id": sid, "version": m["version"], "name": m["name"],
            "username": m["username"], "category": m["category"], "thesis": m["thesis"],
            "description": m["description"],
            "market_types": j(m["market_types"]),
            "data_sources": j(m["data_sources"]),
            "entry_rules": j(m["entry_rules"]), "exit_rules": j(m["exit_rules"]),
            "sizing_rules": j(m["sizing_rules"]), "historical_window": j(m["historical_window"]),
            "expected_edge": j(m["expected_edge"]), "failure_modes": j(m["failure_modes"]),
            "data_limitations": j(m["data_limitations"]),
            "lookahead_controls": j(m["lookahead_controls"]), "lineage": m.get("lineage"),
            "version_history": j(m.get("history") or []),
            "status": "active", "created_utc": ts, "updated_utc": ts}, replace=True)


def main():
    with db.get_db() as con:
        register_strategies(con)

        # 1) price backtest on whatever verified price history exists
        bt = backtest.run_backtest(con, BACKTEST_SEASONS, run_id="bt-2026-09-20")
        print(f"backtest: {bt}")

        # 1b) signal-validation backtest (outcome-only, no prices — no free
        # historical price series exists; this validates the decision rules
        # against verified results and is labeled as such on the site)
        sig = signal_backtest.run_signal_backtest(con, run_id="sigbt-2026-09-21")
        print(f"signal-backtest: {sig}")

        # 1c) price-based historical simulation over the validated SBR archive
        # (real moneylines; the ONLY price-verified historical result the
        # project has, because no other free price history exists)
        if con.execute("SELECT COUNT(*) c FROM hist_odds").fetchone()["c"]:
            hres = hist_backtest.run(con)
            n_rows = hist_backtest.persist(con, hres, "hist-sbr-2026-09-21")
            with open("data/hist_backtest.json", "w") as f:
                json.dump({"run_id": "hist-sbr-2026-09-21", "games": hres["games"],
                           "seasons": hres["seasons"], "summary": hres["summary"],
                           "method": {"flat_stake": hist_backtest.FLAT_STAKE,
                                      "min_edge": hist_backtest.MIN_EDGE,
                                      "shrink_to_market": hist_backtest.SHRINK,
                                      "max_credible_edge": validation.MAX_CREDIBLE_EDGE,
                                      "price_source": "SBR archive moneyline",
                                      "state": "same-season, strictly-prior games"}},
                          f, indent=1)
            print(f"hist-backtest: {hres['games']} priced games, "
                  f"{len(hres['bets'])} simulated bets, {n_rows} rows")

        # 2) forward paper engine
        n_new = paper.generate_forward_bets(con)
        n_settled = paper.settle_finished(con)
        paper.mark_open_positions(con)
        print(f"forward: new={n_new} settled={n_settled}")

        # 2a) quarantine: flag open bets whose decision state is invalid
        q = paper.quarantine_bets(con, run_id=f"quarantine-{util.utcnow_iso()[:10]}")
        if q:
            print("quarantined:", q)

        # 2b) validation tiers: derived from stored evidence, published so the
        # site can show exactly why each strategy is trading or parked
        tiers = validation.all_tiers(con)
        from collections import Counter
        print("tiers:", dict(Counter(t["tier"] for t in tiers.values())))
        for sid, t in tiers.items():
            db.insert(con, "meta", {
                "key": f"tier:{sid}",
                "value": json.dumps({"tier": t["tier"], "detail": t["detail"],
                                     "policy": t["policy"], "reason": t["reason"],
                                     "run_id": (t["evidence"] or {}).get("run_id")}),
                "updated_utc": util.utcnow_iso()}, replace=True)

        # 3) audit
        summary = audit.run_checks(con)
        print(f"audit: {summary['critical']} critical / {summary['warn']} warn / "
              f"{summary['info']} info")

        # 4) site
        db.insert(con, "meta", {"key": "last_pipeline_utc", "value": util.utcnow_iso(),
                                "updated_utc": util.utcnow_iso()}, replace=True)
        sitegen.build_all(con, out_dir=".")
        print("site built: index.html + 8 pages + data/history.json")


if __name__ == "__main__":
    main()
