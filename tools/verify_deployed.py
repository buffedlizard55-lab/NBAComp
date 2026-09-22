#!/usr/bin/env python3
"""Verify the DEPLOYED GitHub Pages site byte-for-byte against the repository.

The claim "the site is live and correct" is only checkable by fetching it. This
tool fetches every published page from the Pages URL, hashes the bytes, and
compares them with the exact bytes committed at a given git ref. Byte identity
is the strongest available statement: GitHub Pages serves the committed file
unchanged, so a mismatch means the deployment is stale or the URL is wrong --
never "close enough".

It writes ``data/live_verify.json`` (machine-readable evidence: ref, commit,
per-page HTTP status/bytes/sha256, verdict) and exits non-zero on any FAIL or
MISMATCH unless ``--no-fail`` is given.

Network access is required, so this runs in GitHub Actions, not in the offline
unit test suite; the classification logic is tested with an injected fetcher
(``tests/test_verify_deployed.py``).

Reused idea, not code: MasterSite (buffedlizard55-lab/MasterSite) keeps an
independent read-only verifier plus ``tools/last_live_verify.json`` recording
per-field OK/MISMATCH/FAIL against the live API. That convention is what earns
"verified live" a machine-readable artifact instead of a sentence in a report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = "buffedlizard55-lab/NBAComp"
PAGES = [
    "index.html", "leaderboard.html", "strategies.html", "upcoming.html",
    "positions.html", "history.html", "sources.html", "research.html",
    "methodology.html", "style.css",
]
OK, MISMATCH, FAIL = "OK", "MISMATCH", "FAIL"


def default_fetcher(url: str, timeout: int = 30):
    """Return (http_status, body_bytes). Never raises for HTTP errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "nbacomp-verify-deployed"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body
    except Exception as e:  # network/TLS/timeout
        return 0, str(e).encode()


def api_get(path: str):
    """Minimal GitHub API read (token from env if present). Returns (status, json)."""
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "nbacomp-verify-deployed"},
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"message": e.reason}
    except Exception as e:
        return 0, {"message": str(e)}


def git_blobs(ref: str, paths: list[str]) -> dict[str, bytes]:
    """Bytes of each path at ref, straight from git (no working-tree state)."""
    out: dict[str, bytes] = {}
    for p in paths:
        try:
            out[p] = subprocess.run(
                ["git", "show", f"{ref}:{p}"], check=True, capture_output=True,
            ).stdout
        except subprocess.CalledProcessError:
            out[p] = b""
    return out


def pages_list_from_git(ref: str) -> list[str]:
    """Every published artifact present at ref: root *.html plus style.css."""
    try:
        names = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref], check=True,
            capture_output=True, text=True,
        ).stdout.split()
    except subprocess.CalledProcessError:
        return list(PAGES)
    found = [n for n in names if "/" not in n and (n.endswith(".html") or n == "style.css")]
    return sorted(found) or list(PAGES)


def verify_pages(paths: list[str], blobs: dict[str, bytes], fetch, base_url: str,
                 cache_bust: str = "", retry_wait: float = 0.0) -> list[dict]:
    """Fetch each path and compare bytes with the committed blob."""
    results = []
    base = base_url.rstrip("/") + "/"
    for p in paths:
        url = base + p + (f"?cb={cache_bust}" if cache_bust else "")
        status, body = fetch(url)
        committed = blobs.get(p, b"")
        got = hashlib.sha256(body).hexdigest()
        want = hashlib.sha256(committed).hexdigest()
        verdict = OK if (status == 200 and got == want) else (FAIL if status != 200 else MISMATCH)
        if verdict == MISMATCH and retry_wait:
            # A stale CDN edge is worth one slow retry before calling it a mismatch.
            time.sleep(retry_wait)
            status, body = fetch(url + ("&" if "?" in url else "?") + "retry=1")
            got = hashlib.sha256(body).hexdigest()
            verdict = OK if (status == 200 and got == want) else (FAIL if status != 200 else MISMATCH)
        results.append({
            "path": p, "url": url, "http_status": status, "live_bytes": len(body),
            "committed_bytes": len(committed), "live_sha256": got, "committed_sha256": want,
            "verdict": verdict,
        })
    return results


def wait_for_deploy(repo: str, target_commit: str, max_wait: int, interval: int = 15,
                    api=api_get, sleep=time.sleep, log=print, clock=time.time) -> dict:
    """Poll Pages builds until the built commit is `target_commit` (or time runs out)."""
    deadline = clock() + max_wait
    last = {}
    while True:
        status, data = api(f"/repos/{repo}/pages/builds/latest")
        last = data if isinstance(data, dict) else {}
        built = str(last.get("commit") or "")
        state = str(last.get("status") or "")
        log(f"pages build: status={state or 'unknown'} commit={built[:8] or 'unknown'} "
            f"target={target_commit[:8]}")
        if status == 200 and state == "built" and built.startswith(target_commit[:8]):
            return {"status": state, "commit": built, "matched": True}
        if clock() >= deadline:
            return {"status": state, "commit": built, "matched": False,
                    "note": f"waited {max_wait}s for commit {target_commit[:8]}"}
        sleep(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--commit", default=None, help="commit Pages must have built (default: ref)")
    ap.add_argument("--out", default="data/live_verify.json")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait for the Pages build")
    ap.add_argument("--retry-wait", type=float, default=0.0)
    ap.add_argument("--no-fail", action="store_true")
    args = ap.parse_args(argv)

    owner, name = args.repo.split("/")
    base_url = args.base_url or f"https://{owner}.github.io/{name}/"
    ref = args.ref
    commit = args.commit or subprocess.run(
        ["git", "rev-parse", ref], check=True, capture_output=True, text=True,
    ).stdout.strip()

    deploy = {"matched": None}
    if args.wait:
        deploy = wait_for_deploy(args.repo, commit, args.wait)

    paths = pages_list_from_git(ref)
    blobs = git_blobs(ref, paths)
    results = verify_pages(paths, blobs, default_fetcher, base_url,
                           cache_bust=commit[:12], retry_wait=args.retry_wait)

    mismatches = [r for r in results if r["verdict"] != OK]
    report = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": args.repo,
        "base_url": base_url,
        "ref": ref,
        "commit": commit,
        "pages_build": deploy,
        "pages_checked": len(results),
        "pages_ok": len(results) - len(mismatches),
        "verdict": "ok" if not mismatches else "failed",
        "mismatches": [{k: r[k] for k in ("path", "url", "http_status", "verdict",
                                          "live_bytes", "committed_bytes",
                                          "live_sha256", "committed_sha256")}
                       for r in mismatches],
        "pages": results,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
        fh.write("\n")

    print(f"deployed-site verification: {report['pages_ok']}/{report['pages_checked']} pages "
          f"byte-identical at {commit[:8]} -> {report['verdict']}")
    for m in report["mismatches"]:
        print(f"  {m['verdict']}: {m['path']} http={m['http_status']} "
              f"live={m['live_bytes']}B committed={m['committed_bytes']}B")
    if mismatches and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
