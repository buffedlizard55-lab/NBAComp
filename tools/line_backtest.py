"""Run the line-based historical validation over the SBR archive.

Observed opening/closing spreads and totals only: this track publishes cover
rates and line accuracy, and computes no P&L at all (see
`nbacomp/line_backtest.py` for why that distinction is the whole point).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import backtest, db, line_backtest  # noqa: E402

RUN_ID = "line-sbr-2026-09-22"


def main() -> int:
    with db.get_db() as con:
        result = line_backtest.run(con)
        n = line_backtest.persist(con, result, RUN_ID)
        out = {
            "run_id": RUN_ID,
            "games": result["games"],
            "seasons": result["seasons"],
            "breakeven": result["breakeven"],
            "summary": result["summary"],
            "method": {
                "price_source": "none — observed lines only",
                "lines": "SBR archive opening/closing spread and total",
                "spread_min_cover": backtest.SPREAD_MIN_COVER,
                "spread_margin_sd": backtest.SPREAD_MARGIN_SD,
                "line_move_min": line_backtest.LINE_MOVE_MIN,
                "breakeven_juice": line_backtest.BREAKEVEN_AMERICAN,
                "state": "same-season, strictly-prior games only",
                "pnl": "not computed and not claimed",
            },
        }
        os.makedirs("data", exist_ok=True)
        with open("data/line_backtest.json", "w") as f:
            json.dump(out, f, indent=1)
        pooled = {k: v for k, v in result["summary"].items()
                  if k.endswith("|ALL")}
        print(json.dumps(pooled, indent=1))
        print(f"rows persisted: {n} (no P&L computed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
