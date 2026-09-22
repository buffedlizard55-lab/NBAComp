"""The audit summary must separate "this pass" from "the append-only log".

`anomalies` is never pruned and most checks re-fire every run, so its totals grow
monotonically. Publishing them under bare severity keys made
data/audit_summary.json (critical: 25) appear to contradict the pipeline's
"audit (this pass): 0 critical" line for the same pass.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from nbacomp import audit, db, sitegen


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


def test_audit_pass_stamps_its_own_counts(con):
    summary = audit.run_checks(con)
    stamp = con.execute("SELECT value FROM meta WHERE key='audit_last_pass'").fetchone()
    assert stamp, "the audit pass must record what it found"
    got = json.loads(stamp["value"])
    assert (got["critical"], got["warn"], got["info"]) == (
        summary["critical"], summary["warn"], summary["info"])
    assert got["finished_utc"].endswith("Z")


def test_summary_distinguishes_latest_pass_from_cumulative(con):
    audit.run_checks(con)
    # an anomaly of a kind that will never re-fire (an earlier run's defect)
    db.log_anomaly(con, "critical", "historical-defect",
                   {"detail": "recorded by an earlier run, never pruned"})
    second = audit.run_checks(con)
    s = sitegen._audit_summary(con)
    assert s["latest_run_counts"] == {k: second[k] for k in ("critical", "warn", "info")}
    assert s["critical"] > s["latest_run_counts"]["critical"], "cumulative keeps history"
    assert "append-only" in s["scope"]
    assert "historical-defect" in [c["check_name"] for c in s["critical_checks_ever_recorded"]]


def test_run_counts_note_quotes_the_latest_pass(con):
    first = audit.run_checks(con)
    note = sitegen._run_counts_note(con)
    assert "Latest audit pass" in note and "cumulative" in note
    assert f"{first['critical']} critical / {first['warn']} warn / {first['info']} info" in note
