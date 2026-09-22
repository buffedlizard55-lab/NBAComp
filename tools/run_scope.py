#!/usr/bin/env python3
"""Decide, and then police, what a collect-and-build run is allowed to skip.

Why this is a separate, tested program
--------------------------------------
The workflow used to decide "should this run do anything?" by looking at the
subject of the last commit::

    if git log -1 --format=%s | grep -qF '[auto]'; then skip=true; fi

The bot's own pipeline commits are titled ``[auto] collect + pipeline + site
build ...``, so every run that started *after* one of those commits concluded it
was its own echo and skipped. Evidence (GitHub Actions jobs API, 2026-09-21/22):
runs 35660560504 (06:23 cron), 35602774751 (12:23 cron) and the 05:07Z run each
executed steps 1-5 and then skipped steps 6-21 -- no collection, no pipeline, no
site build -- and still reported **success**, because a run that skips every
step has nothing left to fail. The scheduled system was silently idle.

The rule the workflow actually wants is about *who fired it*, not about what the
head commit says:

* ``push`` -> the bot may skip its own auto-commit echo (belt and braces: such
  pushes do not even trigger the workflow, because every path it writes is in
  ``paths-ignore``); ``[probe]`` commits take the fast probe-only path.
* ``schedule`` / ``workflow_dispatch`` -> **never** skip. An operator asking for
  a backfill, or a cron tick, must always do the work.

``--verify`` re-reads the decisions the workflow made and exits non-zero on any
state that means "this run did nothing and must not report success".
"""

from __future__ import annotations

import argparse
import sys

AUTO_TAG = "[auto]"
PROBE_TAG = "[probe]"

SKIPPABLE_EVENTS = {"push"}


def decide(event: str, message: str = "") -> dict[str, bool]:
    """Return {'skip': bool, 'probe': bool} for a workflow event + commit subject."""
    event = (event or "").strip()
    subject = (message or "").splitlines()[0] if message else ""
    if event not in SKIPPABLE_EVENTS:
        # Schedule and manual runs always work. This is the fix: an inherited
        # "[auto]" subject can no longer turn a cron run into a no-op.
        return {"skip": False, "probe": False}
    probe = PROBE_TAG in subject
    # A probe commit takes the probe-only path, which is itself a skip of the
    # full work steps, so it always carries skip=true.
    return {
        "skip": probe or AUTO_TAG in subject,
        "probe": probe,
    }


def verify(event: str, skip: bool, probe: bool, daily_outcome: str | None = None) -> list[str]:
    """Return a list of fatal problems with the decisions/outcomes of a run."""
    problems: list[str] = []
    event = (event or "").strip()

    if skip and event not in SKIPPABLE_EVENTS:
        problems.append(
            f"event '{event}' must never take the skip path (only {sorted(SKIPPABLE_EVENTS)})"
        )
    if skip and not probe and event in SKIPPABLE_EVENTS:
        # The bot's own echo on push: legitimately nothing to do.
        pass
    elif skip and not probe:
        problems.append(
            "skip=true with probe=false: every work step is skipped and the run would "
            "report success having done nothing"
        )
    if probe and not skip:
        problems.append("probe=true with skip=false: probe-only path mixed with full work")

    if daily_outcome is not None and event not in SKIPPABLE_EVENTS:
        if skip or probe:
            problems.append(f"event '{event}' skipped collection (daily step outcome={daily_outcome})")
        elif daily_outcome == "skipped":
            problems.append(
                f"event '{event}' skipped the daily collection step for an unknown reason "
                "(outcome=skipped while the scope flags said it should run)"
            )
        elif daily_outcome not in ("success",):
            problems.append(f"daily collection step outcome is '{daily_outcome}', not 'success'")
    return problems


def _bool(x) -> bool:
    return str(x).strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--event", required=True, help="github.event_name")
    ap.add_argument("--message", default="", help="github.event.head_commit.message")
    ap.add_argument("--verify", action="store_true", help="police the run instead of deciding it")
    ap.add_argument("--skip", default="false")
    ap.add_argument("--probe", default="false")
    ap.add_argument("--daily-outcome", default=None,
                    help="steps.<daily>.outcome from the workflow (optional)")
    ap.add_argument("--out", default=None, help="append KEY=VALUE lines for $GITHUB_OUTPUT")
    args = ap.parse_args(argv)

    if not args.verify:
        d = decide(args.event, args.message)
        lines = [f"skip={'true' if d['skip'] else 'false'}",
                 f"probe={'true' if d['probe'] else 'false'}"]
        for line in lines:
            print(line)
        if args.out:
            with open(args.out, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return 0

    problems = verify(args.event, _bool(args.skip), _bool(args.probe), args.daily_outcome)
    for p in problems:
        print(f"::error::{p}", file=sys.stderr)
    if problems:
        print(f"run scope INVALID for event '{args.event}': {len(problems)} problem(s)",
              file=sys.stderr)
        return 1
    print(f"run scope valid for event '{args.event}' "
          f"(skip={args.skip}, probe={args.probe}, daily={args.daily_outcome})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
