#!/usr/bin/env python
"""Print the number of games in the database (used by CI). Exits 0 with a number."""
import sys

sys.path.insert(0, ".")
from nbacomp import db  # noqa: E402

try:
    con = db.connect()
    print(con.execute("SELECT COUNT(*) FROM games").fetchone()[0])
except Exception:
    print(0)
