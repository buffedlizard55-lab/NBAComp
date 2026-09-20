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
    __slots__ = ("status", "body", "json", "url", "error")

    def __init__(self, status: int, body: bytes | None, url: str, error: str | None = None):
        self.status = status
        self.body = body
        self.url = url
        self.error = error
        self.json = None
        if body and not error:
            try:
                self.json = json.loads(body.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.json = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.json is not None


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
