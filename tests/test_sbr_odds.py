"""SBR historical-odds parser tests (research pass, 2026-09-21).

The fixture below reproduces rows copied from the real archive page
(https://www.sportsbookreviewsonline.com/scoresoddsarchives/nba-odds-2022-23)
as fetched during the research pass, including the two structural quirks that
make this source dangerous to parse naively:

  * which row of a game carries the TOTAL in Open/Close varies (rot 501/502
    puts the total on the visitor row; rot 533/534 puts it on the home row),
  * a game can be a pick'em ("pk") or nearly so.

The tests assert that (a) real rows parse, (b) structurally impossible or
internally inconsistent rows are REJECTED rather than stored, and (c) every
stored row satisfies the cross-checks the collector relies on.
"""
from __future__ import annotations

import os
import sys

import pytest

from nbacomp import db, util
from nbacomp.sources import sbr

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture()
def con(tmp_path):
    c = db.connect(str(tmp_path / "sbr.db"))
    yield c
    c.close()


def _table(rows: list[str]) -> str:
    return ("<html><body><table><tbody>"
            + "".join(f"<tr>{r}</tr>" for r in rows)
            + "</tbody></table></body></html>")


def _cells(*vals) -> str:
    return "".join(f"<td>{v}</td>" for v in vals)


# Verbatim rows from the archive (rot 501/502, 533/534, 557/558 of 2022-23).
REAL_ROWS = [
    _cells("1018", "501", "V", "Philadelphia", "29", "34", "25", "29", "117",
           "229", "216", "135", "107"),
    _cells("1018", "502", "H", "Boston", "24", "39", "35", "28", "126",
           "7", "3", "-155", "2"),
    _cells("1021", "533", "V", "Chicago", "30", "20", "24", "26", "100",
           "2.5", "221", "110", "0.5"),
    _cells("1021", "534", "H", "Washington", "26", "30", "21", "25", "102",
           "222.5", "2", "-130", "112"),
    _cells("1022", "557", "V", "Detroit", "35", "21", "23", "36", "115",
           "227.5", "232", "-110", "112.5"),
    _cells("1022", "558", "H", "Indiana", "25", "27", "36", "36", "124",
           "pk", "1", "-110", "1.5"),
]


def test_real_rows_parse_with_totals_spreads_and_ml():
    """Two of the three real games parse; the third is a documented reject.

    rot 533/534 (Chicago@Washington) prints a spread value in the visitor row's
    Open column and a total in its Close column (and vice versa on the home
    row). Which value is the open and which the close cannot be determined for
    that game from the page, so it is REJECTED — a price we cannot label is a
    price we do not store.
    """
    games, rejects = sbr.parse_season(_table(REAL_ROWS), "2022-23")
    assert len(games) == 2
    assert [r["reason"] for r in rejects] == [
        "open/close disagree on which row carries the total"]
    by_rot = {g["rot"]: g for g in games}
    g1 = by_rot[501]
    assert (g1["away"], g1["home"]) == ("PHI", "BOS")
    assert g1["game_date_et"] == "2022-10-18"
    assert (g1["away_final"], g1["home_final"]) == (117, 126)
    assert (g1["open_total"], g1["close_total"]) == (229.0, 216.0)
    assert (g1["ml_away"], g1["ml_home"]) == (135, -155)
    # home row carries +7/+3 (home favoured), so the home spread is -7 / -3
    assert (g1["open_home_spread"], g1["close_home_spread"]) == (-7.0, -3.0)
    # pick'em spread (total printed on the visitor row, spread on the home row)
    g3 = by_rot[557]
    assert g3["open_home_spread"] == 0.0 and g3["close_home_spread"] == -1.0
    assert (g3["open_total"], g3["close_total"]) == (227.5, 232.0)


def test_quarter_sums_are_enforced():
    bad = list(REAL_ROWS)
    bad[0] = _cells("1018", "501", "V", "Philadelphia", "29", "34", "25", "20",
                    "117", "229", "216", "135", "107")  # quarters now sum to 108
    games, rejects = sbr.parse_season(_table(bad), "2022-23")
    assert not any(g["rot"] == 501 for g in games)
    assert any("do not sum" in r["reason"] for r in rejects)


def test_ambiguous_line_columns_are_rejected_not_guessed():
    bad = list(REAL_ROWS)
    bad[0] = _cells("1018", "501", "V", "Philadelphia", "29", "34", "25", "29",
                    "117", "229", "216", "135", "107")
    bad[1] = _cells("1018", "502", "H", "Boston", "24", "39", "35", "28", "126",
                    "220", "218", "-155", "2")
    games, rejects = sbr.parse_season(_table(bad), "2022-23")
    assert not any(g["rot"] == 501 for g in games)
    assert any("2 totals + 2 spreads" in r["reason"] for r in rejects)


def test_unknown_team_labels_are_rejected():
    bad = list(REAL_ROWS)
    bad[0] = _cells("1018", "501", "V", "Springfield", "29", "34", "25", "29",
                    "117", "229", "216", "135", "107")
    games, rejects = sbr.parse_season(_table(bad), "2022-23")
    assert rejects and "unknown team" in rejects[0]["reason"]


def test_non_consecutive_rot_numbers_are_rejected():
    bad = list(REAL_ROWS)
    bad[1] = _cells("1018", "599", "H", "Boston", "24", "39", "35", "28", "126",
                    "7", "3", "-155", "2")
    games, rejects = sbr.parse_season(_table(bad), "2022-23")
    assert not any(g["rot"] == 501 for g in games)
    assert rejects


def test_spread_and_moneyline_must_agree():
    """A row whose spread contradicts its moneyline is a parse artefact."""
    bad = list(REAL_ROWS)
    bad[0] = _cells("1018", "501", "V", "Philadelphia", "29", "34", "25", "29",
                    "117", "229", "216", "600", "107")
    bad[1] = _cells("1018", "502", "H", "Boston", "24", "39", "35", "28", "126",
                    "7", "3", "-1200", "2")
    games, rejects = sbr.parse_season(_table(bad), "2022-23")
    assert not any(g["rot"] == 501 for g in games)
    assert any("spread/ML disagree" in r["reason"] for r in rejects)


def test_collector_stores_only_validated_rows_and_counts_rejects(con, monkeypatch):
    html = _table(REAL_ROWS)
    monkeypatch.setattr(sbr, "fetch_season",
                        lambda season: _FakeResult(html))
    stats = __import__("nbacomp.collect", fromlist=["x"]).collect_sbr_season(con, "2022-23")
    assert stats["stored"] == 2 and stats["rejected"] == 1
    rows = con.execute("SELECT * FROM hist_odds ORDER BY game_date_et, away").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["cross_checked"] == 0          # not yet independently verified
        assert r["away_q"] and r["home_q"]      # quarters retained as evidence
        assert r["source_url"].endswith("nba-odds-2022-23")
    # re-running must not duplicate rows (primary key + replace), and must
    # record the anomaly when the source edits a stored row
    monkeypatch.setattr(sbr, "fetch_season",
                        lambda season: _FakeResult(html.replace(">229<", ">231<")))
    stats2 = __import__("nbacomp.collect", fromlist=["x"]).collect_sbr_season(con, "2022-23")
    assert stats2["stored"] == 2
    assert con.execute("SELECT COUNT(*) c FROM hist_odds").fetchone()["c"] == 2
    assert con.execute("SELECT COUNT(*) c FROM anomalies WHERE check_name='sbr-row-changed'").fetchone()["c"] == 1


class _FakeResult:
    def __init__(self, html: str):
        self.status = 200
        self.body = html.encode()
        self.error = None
        self.json = None
        self.ok = True
        self.ok_body = True
