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

import asyncio
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

# RSS/Atom are here because a feed is the only EXACT source an agent-loop
# digest has: real timestamps, real titles, the publisher's own summary, no
# API key. Without them a scheduled task reported all three competitor blogs
# as "feed not supported" and fell back to scraping listing pages, which is
# how a six-day-old item ends up inside a 72-hour report. `text/xml` and
# `application/xml` were already listed but no real feed serves those:
# Shopify's `.atom` sends application/atom+xml, WordPress's `/feed/` and
# frame.work's `blog.rss` send application/rss+xml.
_OK_TYPES = ("text/html", "text/plain", "application/json",
             "application/xhtml+xml", "text/xml", "application/xml",
             "application/rss+xml", "application/atom+xml")

DEFAULT_PROXY_URL = "http://169.254.7.1:8888"
# os.environ.get() returns "" — not the default — for a variable that is set but
# empty, and an empty proxy URL makes httpx.AsyncClient() raise out of a
# function contracted never to raise. Treat blank as unset.
PROXY_URL = os.environ.get("NIMOOS_EGRESS_PROXY_URL", "").strip() or DEFAULT_PROXY_URL

MAX_CACHE_ENTRIES = 64

# (url, max_chars) -> (expires_at_monotonic, result dict).
#
# max_chars is part of the key on purpose: a first call with max_chars=30000
# caches a truncated body, and without it a later call asking for 60000 would be
# served that same truncation, still flagged truncated:true, for the rest of the
# TTL — with no way for the model to reach the rest.
#
# Bounded and expiry-evicting because each entry can hold up to 60 KB of
# markdown and the agent container runs under a hard memory limit; an unbounded
# dict is an OOM waiting for a browsing session long enough to find it.
_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _cache_get(key: tuple[str, int]) -> dict | None:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    if hit[0] <= time.monotonic():
        _CACHE.pop(key, None)      # drop it rather than wait to overwrite
        return None
    return hit[1]


def _cache_put(key: tuple[str, int], value: dict) -> None:
    now = time.monotonic()
    for stale in [k for k, v in _CACHE.items() if v[0] <= now]:
        _CACHE.pop(stale, None)
    while len(_CACHE) >= MAX_CACHE_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)))     # dicts are insertion-ordered: oldest
    _CACHE[key] = (now + CACHE_TTL_SEC, value)


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


# Retry policy for a throttled or briefly broken host.
#
# Found on the live box: a task fetched four Reddit search feeds in quick
# succession and three came back 429 — Reddit throttles a datacenter IP hard.
# The pacing in web/backends.py covers SEARCH only, so a throttled feed became
# "0 items" in the report, indistinguishable from "that source had nothing".
#
# Retry, not global pacing: ten YouTube channel feeds fetched back to back all
# succeeded, so serializing every host behind one interval would slow the common
# case to fix the uncommon one.
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SEC = 2.0          # doubled per retry
FETCH_MAX_BACKOFF_SEC = 30.0     # ceiling, incl. a server's own Retry-After
_FETCH_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Injection seam for tests (real sleeps would make this the slowest file here).
_retry_sleep = asyncio.sleep


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """How long to wait before the next attempt.

    `Retry-After` wins when the server sends a usable one — a server that names
    its own cooldown knows better than our curve — but it is capped: a hostile
    or broken value ("99999") must not park the run for an hour. Anything
    unparseable falls back to the curve rather than to zero, which would spin.
    """
    raw = (resp.headers.get("Retry-After") or "").strip()
    if raw:
        try:
            return min(float(int(raw)), FETCH_MAX_BACKOFF_SEC)
        except (TypeError, ValueError):
            pass                    # a date form, or junk — use the curve
    return min(FETCH_BACKOFF_SEC * (2 ** attempt), FETCH_MAX_BACKOFF_SEC)


async def _fetch_once(client: httpx.AsyncClient, url: str) -> dict:
    """One logical fetch: no redirect following, but retried when throttled.

    Returns a raw dict or an error dict. A retried status that never clears is
    still reported with its code — a throttled source must never look like an
    empty one.
    """
    for attempt in range(FETCH_ATTEMPTS):
        result = await _fetch_attempt(client, url, attempt)
        retry_after = result.pop("_retry_after", None)
        if retry_after is None or attempt == FETCH_ATTEMPTS - 1:
            return result
        await _retry_sleep(retry_after)
    return result       # unreachable; the loop always returns


async def _fetch_attempt(client: httpx.AsyncClient, url: str,
                         attempt: int = 0) -> dict:
    """One HTTP request. A retryable status carries `_retry_after` back up.

    `attempt` only feeds the backoff curve, so a host that keeps throttling gets
    progressively more room instead of the same short pause three times.
    """
    try:
        req = client.build_request(
            "GET", url,
            headers={"User-Agent": _user_agent(),
                     # Feed types are advertised too: a server that content-
                     # negotiates would otherwise answer a feed URL with HTML,
                     # which is precisely not what the caller asked for.
                     "Accept": "text/html,application/xhtml+xml,"
                               "application/rss+xml,application/atom+xml,"
                               "text/plain;q=0.9"},
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
        if resp.status_code in _FETCH_RETRY_STATUSES:
            # Hand the delay up to _fetch_once, which owns the attempt budget.
            return {"error": f"HTTP {resp.status_code} from {url}",
                    "_retry_after": _retry_delay(resp, attempt)}
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code} from {url}"}
        ctype = resp.headers.get("Content-Type", "")
        if not _content_type_ok(ctype):
            # The header is remote, attacker-controlled text, and this string
            # ends up in the model's context. Interpolating it would be a
            # direct injection channel into the one region the system prompt
            # tells the model to trust, so the type is deliberately NOT
            # echoed: what the model needs is that the type is unsupported and
            # what to use instead.
            return {"error": "unsupported content type — this tool only reads "
                             "web pages; for documents (PDF, Office files) use "
                             "read_document instead"}
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

    key = (clean, int(max_chars))
    hit = _cache_get(key)
    if hit is not None:
        return hit

    owns = client is None
    if client is not None:
        c = client
    else:
        # Inside the error handling: PROXY_URL comes from the environment, and a
        # malformed value makes this constructor raise. This function is
        # contracted never to raise, so a bad proxy URL has to come back as the
        # same "web access is unavailable" error a dead proxy produces.
        try:
            c = httpx.AsyncClient(proxy=PROXY_URL)
        except Exception as exc:  # noqa: BLE001 — httpx raises several types here
            return {"error": f"cannot reach the egress proxy at {PROXY_URL} "
                             f"({exc}) — web access is unavailable right now"}
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
            _cache_put(key, result)
            return result
        return {"error": f"too many same-host redirects starting at {clean}"}
    finally:
        if owns:
            await c.aclose()
