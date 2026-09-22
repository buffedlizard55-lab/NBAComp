"""Tests for tools/verify_deployed.py (offline; network is injected).

The deployed-site check is the only end-to-end evidence that the public Pages
site is what the repository says it is, so its classification logic is tested
here rather than trusted.
"""
import hashlib
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "verify_deployed", os.path.join(ROOT, "tools", "verify_deployed.py"))
vd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vd)


def _fetch_from(blobs, status=200):
    calls = []

    def fetch(url, timeout=30):
        calls.append(url)
        path = url.split("?")[0].rstrip("/").split("/")[-1]
        return status, blobs.get(path, b"")
    fetch.calls = calls
    return fetch


def test_identical_bytes_are_ok():
    blobs = {"index.html": b"<h1>hi</h1>", "style.css": b"body{}"}
    res = vd.verify_pages(list(blobs), blobs, _fetch_from(blobs), "https://x.github.io/r")
    assert [r["verdict"] for r in res] == [vd.OK, vd.OK]
    assert res[0]["live_sha256"] == hashlib.sha256(b"<h1>hi</h1>").hexdigest()


def test_one_changed_byte_is_a_mismatch_not_a_pass():
    blobs = {"index.html": b"<h1>hi</h1>"}
    stale = {"index.html": b"<h1>ho</h1>"}
    res = vd.verify_pages(list(blobs), blobs, _fetch_from(stale), "https://x.github.io/r")
    assert res[0]["verdict"] == vd.MISMATCH
    assert res[0]["live_bytes"] == res[0]["committed_bytes"]  # same length, different bytes


def test_missing_page_is_fail_not_mismatch():
    blobs = {"index.html": b"<h1>hi</h1>", "research.html": b"<h1>r</h1>"}
    served = {"index.html": b"<h1>hi</h1>"}  # research.html absent
    res = vd.verify_pages(sorted(blobs), blobs, _fetch_from(served, status=404),
                          "https://x.github.io/r")
    verdicts = {r["path"]: r["verdict"] for r in res}
    assert verdicts["index.html"] == vd.FAIL
    assert verdicts["research.html"] == vd.FAIL


def test_retry_recovers_from_a_stale_edge():
    blobs = {"index.html": b"<h1>hi</h1>"}
    state = {"n": 0}

    def fetch(url, timeout=30):
        state["n"] += 1
        return (200, b"<h1>stale</h1>") if state["n"] == 1 else (200, b"<h1>hi</h1>")

    res = vd.verify_pages(list(blobs), blobs, fetch, "https://x.github.io/r", retry_wait=0.01)
    assert res[0]["verdict"] == vd.OK and state["n"] == 2


def test_cache_bust_query_is_appended_once():
    blobs = {"index.html": b"x"}
    fetch = _fetch_from(blobs)
    vd.verify_pages(list(blobs), blobs, fetch, "https://x.github.io/r/", cache_bust="abc123")
    assert fetch.calls == ["https://x.github.io/r/index.html?cb=abc123"]


def test_wait_for_deploy_matches_built_commit_and_times_out():
    class FakeTime:
        def __init__(self):
            self.t = 0.0

        def time(self):
            return self.t

        def sleep(self, s):
            self.t += s
    ft = FakeTime()
    seq = [
        {"status": "building", "commit": "0" * 40},
        {"status": "built", "commit": "abc1234" + "0" * 33},
    ]

    def api(path):
        return 200, seq.pop(0)
    out = vd.wait_for_deploy("o/r", "abc1234", 60, interval=10, api=api,
                             sleep=ft.sleep, log=lambda *a: None, clock=ft.time)
    assert out["matched"] is True and out["commit"].startswith("abc1234")

    out = vd.wait_for_deploy("o/r", "f" * 40, 30, interval=10,
                             api=lambda p: (200, {"status": "built", "commit": "a" * 40}),
                             sleep=ft.sleep, log=lambda *a: None, clock=ft.time)
    assert out["matched"] is False and "waited" in out["note"]


def test_report_is_written_and_verdict_reflects_mismatches(tmp_path, monkeypatch):
    # main() must produce a machine-readable artifact and a non-zero exit code
    # when the live site does not match the commit being verified.
    ref_dir = tmp_path
    (ref_dir / "index.html").write_bytes(b"<h1>hi</h1>")
    out = tmp_path / "live_verify.json"
    monkeypatch.setattr(vd, "pages_list_from_git", lambda ref: ["index.html"])
    monkeypatch.setattr(vd, "git_blobs", lambda ref, paths: {"index.html": b"<h1>hi</h1>"})
    monkeypatch.setattr(vd, "default_fetcher", lambda url, timeout=30: (200, b"<h1>hi</h1>"))
    rc = vd.main(["--ref", "HEAD", "--out", str(out), "--no-fail"])
    assert rc == 0
    rep = json.loads(out.read_text())
    assert rep["verdict"] == "ok" and rep["pages_checked"] == 1

    monkeypatch.setattr(vd, "default_fetcher", lambda url, timeout=30: (200, b"<h1>STALE</h1>"))
    rc = vd.main(["--ref", "HEAD", "--out", str(out)])
    assert rc == 1
    rep = json.loads(out.read_text())
    assert rep["verdict"] == "failed" and rep["mismatches"][0]["path"] == "index.html"
