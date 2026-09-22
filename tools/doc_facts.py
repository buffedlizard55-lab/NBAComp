#!/usr/bin/env python
"""Generated-facts blocks for README.md and PROJECT_REPORT.md.

Documentation drift is a defect this repository has already shipped three times
("14 vs 19 strategies", "49/54 vs actual test count", and a README saying "24
strategies" next to a 27-row registry), and every time it was caught by hand
after the fact. This tool removes the hand-work: the figures that can be read
out of the repository are GENERATED into marked blocks, and `--check` fails
when a committed file disagrees with what the repository says now.

    python tools/doc_facts.py --write    # regenerate the blocks in place
    python tools/doc_facts.py --check    # CI gate: exit 1 on any drift

Only facts that change when CODE changes are generated here (the strategy
registry, the offline test count). Row counts are deliberately NOT copied into
prose: they move on every collection run, so any copy would be stale within
six hours — `tools/db_report.py` publishes them to `data/db_report.txt` on
every run and that artifact is the only place they are quoted. Nothing here
invents a number.
"""
from __future__ import annotations

import argparse
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbacomp import strategies as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ generators

def test_functions() -> dict[str, int]:
    """`def test_*` per test file, counted by AST (no pytest needed).

    This is a count of FUNCTIONS, not of pytest items: a parametrized test
    function collects several items. Both numbers are published, labelled as
    what they are, because quoting the smaller one as "the suite collects N
    tests" would be false.
    """
    out: dict[str, int] = {}
    tests_dir = os.path.join(ROOT, "tests")
    if not os.path.isdir(tests_dir):
        return out
    for name in sorted(os.listdir(tests_dir)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        with open(os.path.join(tests_dir, name)) as f:
            tree = ast.parse(f.read(), filename=name)
        out[f"tests/{name}"] = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_"))
    return out


def test_items() -> dict[str, int] | None:
    """Test items pytest actually collects, per file (None if pytest is absent).

    `pytest --collect-only -q` prints one `path: N` line per file; that is the
    real collection count, parametrized cases included.
    """
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        path, _, n = line.rpartition(": ")
        if path.startswith("tests/") and n.strip().isdigit():
            out[path.strip()] = int(n.strip())
    return out or None


def test_counts() -> dict[str, int]:
    """Per-file counts: pytest's own collection where available, else AST."""
    return test_items() or test_functions()


def block_headline(_db_path: str) -> str:
    """The figures prose quotes, generated so they cannot drift."""
    counts = test_counts()
    funcs = test_functions()
    items = test_items()
    suite = (f"**{sum(counts.values())} tests** collected by pytest in "
             f"{len(counts)} files ({sum(funcs.values())} test functions; "
             f"parametrized cases expand)" if items else
             f"**{sum(funcs.values())} test functions** in {len(funcs)} files "
             f"(pytest is not installed here, so the collected item count is "
             f"not quoted)")
    return "\n".join([
        f"- strategies registered: **{len(S.STRATEGIES)}** — `nbacomp/strategies.py` "
        f"is the only source of this number, and the table below is generated from it",
        f"- offline test suite: {suite}",
        "- live database row counts are **not quoted here**: read "
        "`data/db_report.txt`, which every pipeline run regenerates",
    ])


def block_strategy_table(_db_path: str) -> str:
    lines = ["| ID | Username | Category | Version |",
             "|----|----------|----------|---------|"]
    for sid in sorted(S.STRATEGIES):
        m = S.STRATEGIES[sid]
        lines.append(f"| {sid} | {m['username']} | {m['category']} | {m['version']} |")
    return "\n".join(lines)


def block_suite(_db_path: str) -> str:
    counts = test_counts()
    funcs = test_functions()
    items = test_items()
    head = (f"`python -m pytest tests` collects **{sum(counts.values())} tests** "
            f"across {len(counts)} files (offline, no network needed). "
            f"{sum(funcs.values())} of those are test functions; the difference is "
            f"parametrized cases:" if items else
            f"`tests/` holds **{sum(funcs.values())} test functions** across "
            f"{len(funcs)} files. pytest is not installed in this environment, so "
            f"the collected item count is deliberately not quoted:")
    lines = [head, ""]
    lines += [f"- `{f}` — {n} tests" for f, n in sorted(counts.items())]
    return "\n".join(lines)


BLOCKS = {
    "README.md": {
        "headline": block_headline,
        "strategy-table": block_strategy_table,
        "suite": block_suite,
    },
    "PROJECT_REPORT.md": {
        "headline": block_headline,
        "suite": block_suite,
    },
}


# ------------------------------------------------------------------- rewriting

def _markers(name: str) -> tuple[str, str]:
    return f"<!-- facts:{name} -->", f"<!-- /facts:{name} -->"


def render(path: str, db_path: str) -> str | None:
    """File content with every marked block regenerated (None if absent)."""
    full = os.path.join(ROOT, path)
    if not os.path.exists(full):
        return None
    with open(full) as f:
        text = f.read()
    for name, fn in BLOCKS.get(path, {}).items():
        begin, end = _markers(name)
        if begin not in text or end not in text:
            continue
        pre, rest = text.split(begin, 1)
        _old, post = rest.split(end, 1)
        text = f"{pre}{begin}\n{fn(db_path)}\n{end}{post}"
    return text


def check(db_path: str) -> int:
    bad = 0
    for path in BLOCKS:
        full = os.path.join(ROOT, path)
        if not os.path.exists(full):
            print(f"::error::{path} is missing")
            bad += 1
            continue
        with open(full) as f:
            old = f.read()
        new = render(path, db_path)
        if new == old:
            print(f"{path}: generated facts match the repository")
            continue
        bad += 1
        print(f"::error::{path}: generated facts are stale — run "
              f"`python tools/doc_facts.py --write` and commit the result")
        for i, (a, b) in enumerate(zip(old.splitlines(), new.splitlines())):
            if a != b:
                print(f"  first difference at line {i + 1}\n"
                      f"    committed: {a}\n    actual:    {b}")
                break
    return 1 if bad else 0


def write(db_path: str) -> int:
    for path in BLOCKS:
        new = render(path, db_path)
        if new is None:
            print(f"{path}: not present, skipped")
            continue
        with open(os.path.join(ROOT, path), "w") as f:
            f.write(new)
        print(f"{path}: regenerated {', '.join(BLOCKS[path])}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="regenerate/verify documentation facts")
    ap.add_argument("--write", action="store_true",
                    help="regenerate the marked blocks in place")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any committed block disagrees with the repository")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "nbacomp.db"),
                    help="unused by the current blocks; kept for the call signature")
    args = ap.parse_args(argv)
    if args.write:
        return write(args.db)
    return check(args.db)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
