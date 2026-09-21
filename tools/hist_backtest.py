"""Run the price-based historical simulation over the SBR archive."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import db, hist_backtest  # noqa: E402

RUN_ID = "hist-sbr-2026-09-21"


def main() -> int:
    with db.get_db() as con:
        result = hist_backtest.run(con)
        n = hist_backtest.persist(con, result, RUN_ID)
        out = {
            "run_id": RUN_ID,
            "games": result["games"],
            "seasons": result["seasons"],
            "summary": result["summary"],
            "method": {
                "bankroll": hist_backtest.START_BANKROLL,
                "flat_stake": hist_backtest.FLAT_STAKE,
                "min_edge": hist_backtest.MIN_EDGE,
                "shrink_to_market": hist_backtest.SHRINK,
                "max_credible_edge": 0.08,
                "price_source": "SBR archive moneyline (single pre-game price)",
                "state": "same-season, strictly-prior games only",
                "excluded": "totals/spreads: the archive has lines but no per-side "
                            "prices, and an assumed -110 would be an assumption",
            },
        }
        os.makedirs("data", exist_ok=True)
        with open("data/hist_backtest.json", "w") as f:
            json.dump(out, f, indent=1)
        print(json.dumps({k: v for k, v in result["summary"].items()
                          if "|" not in k}, indent=1))
        print(f"rows persisted: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
