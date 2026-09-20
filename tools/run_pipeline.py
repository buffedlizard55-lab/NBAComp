"""Pipeline runner: register strategies → backtest → forward paper engine →
settlement → audit → site build. Single entrypoint used locally and in CI."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import audit, backtest, db, paper, sitegen, strategies as S, util  # noqa: E402

BACKTEST_SEASONS = ["2024-25", "2025-26"]


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
            "status": "active", "created_utc": ts, "updated_utc": ts}, replace=True)


def main():
    with db.get_db() as con:
        register_strategies(con)

        # 1) backtest on whatever verified price history exists
        bt = backtest.run_backtest(con, BACKTEST_SEASONS, run_id="bt-2026-09-20")
        print(f"backtest: {bt}")

        # 2) forward paper engine
        n_new = paper.generate_forward_bets(con)
        n_settled = paper.settle_finished(con)
        paper.mark_open_positions(con)
        print(f"forward: new={n_new} settled={n_settled}")

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
