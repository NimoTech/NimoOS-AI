"""Fetch one web page through the egress-proxy and hand back markdown.

Every request goes through the proxy at PROXY_URL. That is the whole SSRF
story: internal / cloud-metadata / NAT64 addresses, the 80-443 port policy,
the DNS-rebinding re-check at dial time and the TOFU confirmation card all
live in the Go proxy (deploy/agent/egress-proxy/main.go). Nothing here
re-implements them, because two copies of an IP-classification rule is how
one of them ends up wrong.

Cross-host redirects are NOT followed. They are handed back to the model as
`redirect_to` so it calls again with the new URL — a redirect the fetcher
followed silently would be a way around the per-host confirmation the proxy
just granted for a different host.
"""
from __future__ import annotations

import os
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from web import extract

MAX_URL_LEN = 2048
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_SEC = 20.0
CACHE_TTL_SEC = 900          # 15 minutes, same as Claude Code's WebFetch
MAX_SAME_HOST_HOPS = 3

_OK_TYPES = ("text/html", "text/plain", "application/json",
             "application/xhtml+xml", "text/xml", "application/xml")

PROXY_URL = os.environ.get("NIMOOS_EGRESS_PROXY_URL",
                           "http://169.254.7.1:8888")

# url -> (expires_at_monotonic, result dict)
_CACHE: dict[str, tuple[float, dict]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def normalize_url(raw: str) -> tuple[str, str]:
    """Return (url, error) — exactly one is non-empty."""
    s = (raw or "").strip()
    if not s:
        return "", "url is empty"
    if len(s) > MAX_URL_LEN:
        return "", f"url too long (>{MAX_URL_LEN} characters)"
    try:
        parts = urlsplit(s)
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            return "", f"unsupported url scheme {scheme or '(none)'!r} — only http/https"
        host = (parts.hostname or "").lower()
        if not host:
            return "", "url has no host"
        port = parts.port
    except ValueError as exc:
        # urlsplit()/.port raise ValueError on out-of-range ports, non-numeric
        # ports, and broken IPv6 literals — all ordinary garbage a model can
        # hand us from a page it just read, not something to let propagate.
        return "", f"malformed url: {exc}"
    # Drop userinfo: credentials in a URL are never something to forward.
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, parts.path or "/",
                       parts.query, "")), ""


def _content_type_ok(ctype: str) -> bool:
    head = (ctype or "").split(";")[0].strip().lower()
    return head in _OK_TYPES


async def _read_capped(resp: httpx.Response) -> str:
    chunks, total = [], 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > MAX_BYTES:
            chunks.append(chunk[: MAX_BYTES - (total - len(chunk))])
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _fetch_once(client: httpx.AsyncClient, url: str) -> dict:
    """One request, no redirect following. Returns a raw dict or an error dict."""
    try:
        req = client.build_request(
            "GET", url,
            headers={"User-Agent": _user_agent(),
                     "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9"},
            timeout=TIMEOUT_SEC,
        )
        resp = await client.send(req, stream=True, follow_redirects=False)
    except httpx.ConnectError as exc:
        return {"error": f"cannot reach the egress proxy at {PROXY_URL} "
                         f"({exc}) — web access is unavailable right now"}
    except httpx.TimeoutException:
        return {"error": f"timed out after {TIMEOUT_SEC:.0f}s fetching {url}"}
    except httpx.HTTPError as exc:
        return {"error": f"fetch failed: {exc}"}

    try:
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            return {"_redirect": urljoin(url, loc) if loc else ""}
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code} from {url}"}
        ctype = resp.headers.get("Content-Type", "")
        if not _content_type_ok(ctype):
            return {"error": f"unsupported content type {ctype!r} — for "
                             f"documents (PDF, Office files) use read_document "
                             f"instead"}
        return {"_body": await _read_capped(resp)}
    except httpx.TimeoutException:
        return {"error": f"timed out after {TIMEOUT_SEC:.0f}s fetching {url}"}
    except httpx.HTTPError as exc:
        # Covers a connection that stalls or drops mid-body (ReadTimeout is
        # already caught above; RemoteProtocolError etc. land here).
        return {"error": f"fetch failed: {exc}"}
    finally:
        await resp.aclose()


def _user_agent() -> str:
    return f"NimoOS-Agent/{os.environ.get('NIMOOS_VERSION', 'dev')}"


async def fetch_page(url: str, *, max_chars: int = 30000, client=None) -> dict:
    """Fetch *url* and return markdown, a redirect notice, or an error."""
    clean, err = normalize_url(url)
    if err:
        return {"error": err}

    hit = _CACHE.get(clean)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    owns = client is None
    c = client or httpx.AsyncClient(proxy=PROXY_URL)
    try:
        current = clean
        for _ in range(MAX_SAME_HOST_HOPS + 1):
            raw = await _fetch_once(c, current)
            if "error" in raw:
                return raw
            if "_redirect" in raw:
                target = raw["_redirect"]
                if not target:
                    return {"error": f"redirect from {current} had no Location"}
                # Run the redirect target through the same hygiene as the
                # initial URL (scheme upgrade, userinfo strip, IPv6 bracket)
                # before comparing hosts or dialing it — a redirect is not a
                # trusted URL just because it came from a host we already
                # confirmed.
                norm_target, err = normalize_url(target)
                if err:
                    return {"error": err}
                if urlsplit(norm_target).hostname != urlsplit(current).hostname:
                    # Hand it back; do NOT dial the new host ourselves.
                    return {"redirect_to": norm_target}
                current = norm_target
                continue
            body = raw["_body"]
            md = extract.to_markdown(body, url=current)
            truncated = len(md) > max_chars
            result = {
                "url": clean,
                "final_url": current,
                "title": extract.title_of(body),
                "content_markdown": md[:max_chars] if truncated else md,
                "truncated": truncated,
            }
            _CACHE[clean] = (time.monotonic() + CACHE_TTL_SEC, result)
            return result
        return {"error": f"too many same-host redirects starting at {clean}"}
    finally:
        if owns:
            await c.aclose()
