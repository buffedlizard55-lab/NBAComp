#!/usr/bin/env python
"""Checkpoint the SQLite WAL into the main DB file before committing (CI)."""
import sys

sys.path.insert(0, ".")
from nbacomp import db  # noqa: E402

con = db.connect()
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.close()
print("checkpointed")
