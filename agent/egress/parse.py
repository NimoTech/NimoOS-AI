"""
egress/parse.py — command pre-parser for external upload detection.

Parses shell commands to detect upload intent before execution,
allowing the egress DLP layer to prompt users before data leaves
the NimoOS environment.

Pure stdlib, Python 3.11+. Never raises exceptions to callers.
"""

from __future__ import annotations

import ipaddress
import shlex
import socket
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


# ─── Internal network ranges (mirrors egress-proxy isInternal) ────────────────

_INTERNAL_V4 = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
]

_INTERNAL_V6 = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Cloud metadata endpoints (mirrors egress-proxy metadataIPs). These fall
# inside the "internal" link-local/ULA ranges above but are the classic
# SSRF credential-exfil target, so they must be treated as external
# (blockable) rather than silently allowed through as internal.
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure IMDS
    ipaddress.ip_address("169.254.170.2"),    # ECS task metadata
    ipaddress.ip_address("fd00:ec2::254"),    # IPv6 IMDS
}


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class UploadIntent:
    """Describes a detected upload command."""
    host: str
    method: str          # HTTP method or transport verb, e.g. "POST", "PUT", "SCP", "RSYNC"
    files: list[str] = field(default_factory=list)
    inline: bool = False  # True when reading from stdin (@-)
    external: bool = False


# ─── IP classification ────────────────────────────────────────────────────────

def _is_external_host(host: str) -> bool:
    """
    Return True if host resolves to any non-internal IP address.

    Conservative: if *any* resolved IP is external, returns True.
    If resolution fails entirely, returns True (fail-safe / block).

    Internal ranges mirror egress-proxy isInternal():
      127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16,
      169.254.0.0/16, ::1/128, fc00::/7, fe80::/10.
    """
    # Determine bare_host (strip port if present, handle IPv6 literals).
    bare_host = host

    # Strip IPv6 zone ID (e.g. "fc00::1%eth0" → "fc00::1") before any parsing.
    if "%" in bare_host:
        bare_host = bare_host.split("%", 1)[0]

    # Remove brackets around IPv6 literals like [::1] or [::1]:8080
    if bare_host.startswith("["):
        close = bare_host.find("]")
        if close != -1:
            bare_host = bare_host[1:close]
        # else: malformed — leave as-is and let getaddrinfo fail
    elif ":" in bare_host:
        # Could be "host:port" or a bare IPv6 address like "::1" or "fc00::1".
        # First, try to parse as a plain IPv6 address. If that works, use as-is.
        try:
            ipaddress.ip_address(bare_host)
            # It IS a valid IPv6 literal — don't strip anything.
        except ValueError:
            # Not a valid IP literal; treat rightmost ":" as host:port separator.
            try:
                bare_host, _ = bare_host.rsplit(":", 1)
            except ValueError:
                pass

    try:
        results = socket.getaddrinfo(bare_host, None)
    except (socket.gaierror, OSError):
        # Resolution failed → conservative: treat as external
        return True

    if not results:
        return True

    found_any = False
    for _family, _type, _proto, _canonname, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        found_any = True

        # Unwrap IPv4-mapped IPv6 (::ffff:x.x.x.x)
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
            ip_obj = ip_obj.ipv4_mapped

        if ip_obj in _METADATA_IPS:
            return True  # metadata endpoint — treat as external so the A-path blocks it

        internal = False
        if isinstance(ip_obj, ipaddress.IPv4Address):
            for net in _INTERNAL_V4:
                if ip_obj in net:
                    internal = True
                    break
        else:  # IPv6
            for net in _INTERNAL_V6:
                if ip_obj in net:
                    internal = True
                    break

        if not internal:
            return True  # Any external IP → treat whole host as external

    if not found_any:
        return True  # Couldn't parse any IP → conservative

    return False


# ─── URL / host extraction helpers ───────────────────────────────────────────

def _extract_host_from_url(url: str) -> Optional[str]:
    """Extract hostname from an http(s):// URL. Returns None if not a web URL."""
    try:
        if url.startswith(("http://", "https://")):
            parsed = urlparse(url)
            return parsed.hostname or None
    except Exception:
        pass
    return None


def _extract_host_from_scp(token: str) -> Optional[str]:
    """
    Extract host from scp/rsync-style 'user@host:path' or 'host:path'.
    Returns None if the token doesn't look like a remote target.
    """
    # Must contain ':' and the part before ':' must not look like a local path
    if ":" not in token:
        return None
    host_part, _path = token.split(":", 1)
    # Skip if it looks like an absolute path or option
    if host_part.startswith("/") or host_part.startswith("-"):
        return None
    # Skip plain protocol schemes
    if host_part in ("http", "https", "ftp", "sftp"):
        return None
    # Strip user@ prefix
    if "@" in host_part:
        _user, host_part = host_part.rsplit("@", 1)
    if not host_part:
        return None
    return host_part


def _extract_file_from_at(value: str) -> Optional[str]:
    """
    Extract file path from a value starting with '@'.
    '@-' means stdin → returns None (caller sets inline=True).
    '@/path/...' → returns '/path/...'.
    Also handles 'key=@/path' form used by -F.
    """
    # Handle 'key=@path'
    if "=" in value:
        _key, val = value.split("=", 1)
        value = val
    if not value.startswith("@"):
        return None
    path = value[1:]
    if path == "-":
        return None  # stdin
    return path if path else None


# ─── Tool-specific parsers ────────────────────────────────────────────────────

# Upload-indicating flags for curl
_CURL_UPLOAD_FLAGS = {
    "-d", "--data",
    "--data-binary", "--data-raw", "--data-ascii",
    "-T", "--upload-file",
    "-F", "--form",
}

# Method-override flags
_CURL_METHOD_FLAGS = {"-X", "--request"}


def _parse_curl(tokens: list[str]) -> Optional[UploadIntent]:
    """Parse curl/wget/http (httpie) command tokens."""
    host: Optional[str] = None
    files: list[str] = []
    inline = False
    has_upload = False
    method: Optional[str] = None

    i = 1  # skip 'curl'
    while i < len(tokens):
        tok = tokens[i]

        # Method override
        if tok in _CURL_METHOD_FLAGS:
            if i + 1 < len(tokens):
                method = tokens[i + 1].upper()
                i += 2
                continue
            i += 1
            continue

        # Combined -XPOST / -XPUT style
        if tok.startswith("-X") and len(tok) > 2:
            method = tok[2:].upper()
            i += 1
            continue

        # Upload flags with separate value
        if tok in _CURL_UPLOAD_FLAGS:
            has_upload = True
            if i + 1 < len(tokens):
                val = tokens[i + 1]
                i += 2
                # Determine implicit method
                if tok in ("-T", "--upload-file"):
                    if method is None:
                        method = "PUT"
                    # Value is a file path directly
                    if val and not val.startswith("-"):
                        files.append(val)
                else:
                    # -d / -F style: look for @path
                    if val == "@-" or val.endswith("=@-"):
                        inline = True
                    else:
                        extracted = _extract_file_from_at(val)
                        if extracted:
                            files.append(extracted)
            else:
                i += 1
            continue

        # Upload flags with inline value (--data=@path)
        for flag in _CURL_UPLOAD_FLAGS:
            if tok.startswith(flag + "="):
                has_upload = True
                val = tok[len(flag) + 1:]
                if val == "@-":
                    inline = True
                else:
                    extracted = _extract_file_from_at(val)
                    if extracted:
                        files.append(extracted)
                break

        # URL detection
        if tok.startswith(("http://", "https://")):
            extracted_host = _extract_host_from_url(tok)
            if extracted_host:
                host = extracted_host

        i += 1

    # wget / httpie POST detection: if method is POST/PUT from -X
    if method in ("POST", "PUT"):
        has_upload = True

    if not has_upload or host is None:
        return None

    if method is None:
        # Default: -d/-F imply POST, -T implies PUT (already set above)
        method = "POST"

    external = _is_external_host(host)
    return UploadIntent(host=host, method=method, files=files, inline=inline, external=external)


def _parse_scp(tokens: list[str]) -> Optional[UploadIntent]:
    """
    Parse scp command. Upload = local source, remote destination.
    scp [opts] <src> <dst>
    We look for a remote host:path pattern in the destination (last non-option token).
    """
    # Collect non-option tokens
    non_opts = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            # scp options that take an argument
            if tok in ("-i", "-l", "-o", "-P", "-c", "-F", "-J", "-S", "-B"):
                i += 2  # skip option and its value
            else:
                i += 1
            continue
        non_opts.append(tok)
        i += 1

    if len(non_opts) < 2:
        return None

    # In scp, destination is the last token
    dst = non_opts[-1]
    src_tokens = non_opts[:-1]

    host = _extract_host_from_scp(dst)
    if host is None:
        return None

    # Source files (local paths)
    files = [f for f in src_tokens if not f.startswith("-")]

    external = _is_external_host(host)
    return UploadIntent(host=host, method="SCP", files=files, inline=False, external=external)


def _parse_rsync(tokens: list[str]) -> Optional[UploadIntent]:
    """
    Parse rsync command. Upload = local → remote.
    rsync [opts] <src>... <dst>
    """
    non_opts = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            # rsync options that take an argument
            if tok in ("--rsh", "-e", "--bwlimit", "--timeout",
                        "--contimeout", "--port", "--address",
                        "--log-file", "--password-file",
                        "--include", "--exclude",
                        "--include-from", "--exclude-from",
                        "--files-from", "--filter",
                        "-T", "--temp-dir",
                        "--partial-dir", "--log-file-format",
                        "--max-size", "--min-size"):
                i += 2
            else:
                i += 1
            continue
        non_opts.append(tok)
        i += 1

    if len(non_opts) < 2:
        return None

    dst = non_opts[-1]
    src_tokens = non_opts[:-1]

    host = _extract_host_from_scp(dst)  # same user@host: format
    if host is None:
        return None

    files = list(src_tokens)
    external = _is_external_host(host)
    return UploadIntent(host=host, method="RSYNC", files=files, inline=False, external=external)


# ─── Public API ───────────────────────────────────────────────────────────────

_TOOL_PARSERS = {
    "curl": _parse_curl,
    "wget": _parse_curl,   # wget uses similar flags for POST
    "http": _parse_curl,   # httpie
    "scp": _parse_scp,
    "rsync": _parse_rsync,
}


def parse_upload(command: str) -> Optional[UploadIntent]:
    """
    Parse a shell command string and return an UploadIntent if it
    represents an upload operation, or None otherwise.

    Never raises exceptions — any parse failure returns None,
    deferring to the egress proxy's byte-threshold path (path B).
    """
    if not command or not command.strip():
        return None

    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes etc. — can't parse safely
        return None

    if not tokens:
        return None

    # Handle pipelines: if command contains '|', focus on the last segment
    # that is a network tool (e.g. cat f | curl --data-binary @- https://ext)
    # We do a simple linear scan for the rightmost known tool.
    # Re-split each pipe segment individually.
    if "|" in command:
        segments = command.split("|")
        for seg in reversed(segments):
            seg = seg.strip()
            try:
                seg_tokens = shlex.split(seg)
            except ValueError:
                continue
            if not seg_tokens:
                continue
            tool = seg_tokens[0].lstrip("./")
            if tool in _TOOL_PARSERS:
                # Let the tool parser decide inline based on @- presence.
                # Do NOT force inline=True here: "cat f | curl -F k=@/DATA/file ..."
                # uploads a disk file (not stdin) and must be classified as inline=False.
                result = _TOOL_PARSERS[tool](seg_tokens)
                return result
        return None

    tool = tokens[0].lstrip("./")
    parser = _TOOL_PARSERS.get(tool)
    if parser is None:
        return None

    try:
        return parser(tokens)
    except Exception:
        # Defensive: any unexpected error → None (let proxy handle it)
        return None
