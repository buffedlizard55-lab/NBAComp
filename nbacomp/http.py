"""Minimal stdlib HTTP with browser-like headers, retries, and honest failures.

Every collector records the HTTP outcome into source_status so the site can
show what was actually reachable at collection time. Nothing is fabricated on
failure: callers receive None and log it.
"""
from __future__ import annotations

import gzip
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
}

_last_call: dict[str, float] = {}


class HttpResult:
    __slots__ = ("status", "body", "json", "url", "error", "transport")

    def __init__(self, status: int, body: bytes | None, url: str, error: str | None = None,
                 transport: str = "urllib"):
        self.status = status
        self.body = body
        self.url = url
        self.error = error
        self.transport = transport
        self.json = None
        if body and not error:
            try:
                self.json = json.loads(body.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.json = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.json is not None

    @property
    def ok_body(self) -> bool:
        """2xx with a non-empty body, regardless of content type.

        For HTML/text endpoints (Basketball-Reference): .ok requires
        parseable JSON, which HTML never is — using .ok there silently
        fails every fetch (found 2026-09-21: BRef backfill AND verify
        could never succeed). JSON callers must keep using .ok.
        """
        return 200 <= self.status < 300 and bool(self.body)


def get(url: str, params: dict | None = None, headers: dict | None = None,
        timeout: float = 20.0, retries: int = 2, min_interval: float = 0.0,
        extra: dict | None = None) -> HttpResult:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    host = urllib.parse.urlsplit(url).netloc
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    if extra:
        h.update(extra)
    for attempt in range(retries + 1):
        if min_interval:
            last = _last_call.get(host, 0.0)
            wait = min_interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        _last_call[host] = time.time()
        req = urllib.request.Request(url, headers=h, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
                return HttpResult(resp.status, data, url)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return HttpResult(e.code, body, url, error=f"HTTP {e.code}")
        except Exception as e:  # URLError, timeout, SSL...
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return HttpResult(0, None, url, error=f"{type(e).__name__}: {e}")
    return HttpResult(0, None, url, error="unreachable")


def build_url(url: str, params: dict | None = None) -> str:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return url


def node_fetch(url: str, params: dict | None = None, timeout: float = 40.0) -> HttpResult:
    """GET via Node.js fetch() (different TLS stack than urllib).

    Runner-verified 2026-09-20: Akamai 403s urllib/curl fingerprints on ESPN
    hosts while Node fetch gets 200 for the same public URL. Same data, same
    source — only the transport differs, and the transport is recorded on the
    result so collection logs stay honest about how each byte arrived.
    """
    import subprocess

    full = build_url(url, params)
    script = (
        "fetch(" + json.dumps(full) + ",{headers:{'User-Agent':" + json.dumps(USER_AGENT) +
        ",'Accept':'application/json, text/plain, */*'}})"
        ".then(async r=>{const t=await r.text();"
        "console.log(JSON.stringify({status:r.status,body:t.slice(0,4000000)}))})"
        ".catch(e=>{console.log(JSON.stringify({status:0,error:String(e).slice(0,300)}))})"
    )
    try:
        p = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        return HttpResult(0, None, full, error="node not available", transport="node")
    except subprocess.TimeoutExpired:
        return HttpResult(0, None, full, error="node fetch timeout", transport="node")
    try:
        out = (p.stdout or "").strip().splitlines()
        js = json.loads(out[-1]) if out else {}
    except Exception as e:
        return HttpResult(0, None, full, error=f"node output parse: {e}", transport="node")
    if js.get("status") == 200 and js.get("body") is not None:
        return HttpResult(200, js["body"].encode("utf-8", "replace"), full, transport="node")
    return HttpResult(int(js.get("status") or 0), None, full,
                      error=str(js.get("error") or f"HTTP {js.get('status')}"),
                      transport="node")
