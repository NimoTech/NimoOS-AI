"""
Tests for agent/egress/rules.py — path blacklist + content regex DLP.

All tests use real assertions; no mocks except tmp_path for file I/O.
"""

from __future__ import annotations

import pytest

from egress.rules import Verdict, assess


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _write(tmp_path, name: str, content: str | bytes) -> str:
    """Write a temp file and return its absolute path string."""
    p = tmp_path / name
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        p.write_bytes(content)
    return str(p)


# ─── Path blacklist tests ─────────────────────────────────────────────────────

class TestPathBlacklist:
    """Files whose paths match BUILTIN_HARD_BLACKLIST → block, no content read."""

    def test_ssh_dir_blocked(self):
        v = assess(["/home/user/.ssh/id_rsa"])
        assert v.level == "block"
        assert "blacklist" in v.reason

    def test_pem_extension_blocked(self):
        v = assess(["/tmp/server.pem"])
        assert v.level == "block"
        assert "blacklist" in v.reason

    def test_key_extension_blocked(self):
        v = assess(["/tmp/mykey.key"])
        assert v.level == "block"
        assert "blacklist" in v.reason

    def test_aws_credentials_blocked(self):
        v = assess(["/home/user/.aws/credentials"])
        assert v.level == "block"
        assert "blacklist" in v.reason

    def test_gnupg_dir_blocked(self):
        v = assess(["/home/user/.gnupg/secring.gpg"])
        assert v.level == "block"
        assert "blacklist" in v.reason

    def test_id_rsa_star_blocked(self):
        v = assess(["/home/alice/id_rsa.pub"])
        assert v.level == "block"

    def test_id_ed25519_blocked(self):
        v = assess(["/root/id_ed25519"])
        assert v.level == "block"

    def test_p12_extension_blocked(self):
        v = assess(["/tmp/cert.p12"])
        assert v.level == "block"

    def test_first_file_blocked_short_circuits(self, tmp_path):
        """Second file is a real readable clean file; first path match → block."""
        clean = _write(tmp_path, "clean.txt", "hello world")
        v = assess(["/home/user/.ssh/config", clean])
        assert v.level == "block"

    def test_second_file_blocked(self, tmp_path):
        """First file is clean, second hits blacklist."""
        clean = _write(tmp_path, "clean.txt", "hello world")
        v = assess([clean, "/home/user/.aws/config"])
        assert v.level == "block"


# ─── Content scan — block patterns ───────────────────────────────────────────

class TestContentBlock:
    """Content containing high-danger markers → block."""

    def test_rsa_private_key_header(self, tmp_path):
        p = _write(tmp_path, "key.txt",
                   "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK...\n-----END RSA PRIVATE KEY-----\n")
        v = assess([p])
        assert v.level == "block"
        assert "private key" in v.reason

    def test_ec_private_key_header(self, tmp_path):
        p = _write(tmp_path, "ec.txt",
                   "-----BEGIN EC PRIVATE KEY-----\nMHQCAQEE...\n-----END EC PRIVATE KEY-----\n")
        v = assess([p])
        assert v.level == "block"

    def test_generic_private_key_header(self, tmp_path):
        """Pattern covers 'BEGIN PRIVATE KEY' (PKCS#8 unencrypted)."""
        p = _write(tmp_path, "pk8.txt",
                   "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkq...\n-----END PRIVATE KEY-----\n")
        v = assess([p])
        assert v.level == "block"

    def test_aws_akid_exactly_16_alphanum(self, tmp_path):
        p = _write(tmp_path, "creds.txt",
                   "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\naws_secret_access_key = wJalrXUt\n")
        v = assess([p])
        assert v.level == "block"
        assert "AWS" in v.reason

    def test_aws_akid_inline(self):
        payload = b"token: AKIAIOSFODNN7EXAMPLE\n"
        v = assess([], inline_payload=payload)
        assert v.level == "block"

    def test_private_key_inline(self):
        payload = b"-----BEGIN RSA PRIVATE KEY-----\ndata\n"
        v = assess([], inline_payload=payload)
        assert v.level == "block"

    def test_aws_akid_wrong_length_not_blocked(self, tmp_path):
        """AKID with only 15 chars after AKIA → should NOT match (must be exactly 16)."""
        p = _write(tmp_path, "short.txt", "AKIA123456789012345")  # 19 chars → > 16, no match
        # The regex is AKIA[0-9A-Z]{16} → matches the first 20 chars exactly
        # "AKIAIOSFODNN7EXAMPLE" is 20 chars: AKIA + 16 chars.
        # Here "AKIA123456789012345" is AKIA + 15 chars — should not match
        p2 = _write(tmp_path, "short2.txt", "AKIA123456789012")  # exactly AKIA+12 → no match
        v = assess([p2])
        assert v.level == "clean"


# ─── Content scan — suspect patterns ─────────────────────────────────────────

class TestContentSuspect:
    """Content containing medium-danger markers → suspect."""

    def test_jwt_token_detected(self, tmp_path):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        p = _write(tmp_path, "token.txt", f"Authorization: Bearer {jwt}\n")
        v = assess([p])
        assert v.level == "suspect"
        assert "JWT" in v.reason

    def test_jwt_inline(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9"
            ".eyJzdWIiOiJ1c2VyIn0"
            ".abcdefghijklmnopqrst"
        )
        v = assess([], inline_payload=jwt.encode())
        assert v.level == "suspect"

    def test_high_pii_density_emails_and_phones(self, tmp_path):
        content = (
            "Contact: alice@example.com, bob@corp.io\n"
            "Phone: 13812345678\n"
            "Alt: carol@mail.org\n"
        )
        p = _write(tmp_path, "contacts.txt", content)
        v = assess([p])
        assert v.level == "suspect"
        assert "PII" in v.reason

    def test_high_pii_density_cn_id_cards(self, tmp_path):
        content = (
            "ID1: 11010119900101001X\n"
            "ID2: 310101199001010018\n"
            "ID3: 440101199001010013\n"
        )
        p = _write(tmp_path, "ids.txt", content)
        v = assess([p])
        assert v.level == "suspect"
        assert "PII" in v.reason

    def test_pii_below_threshold_is_clean(self, tmp_path):
        """Two PII items → below threshold of 3 → clean."""
        content = "email: alice@example.com\nphone: 13812345678\n"
        p = _write(tmp_path, "sparse.txt", content)
        v = assess([p])
        assert v.level == "clean"

    def test_pii_exactly_at_threshold(self, tmp_path):
        """Exactly 3 PII items → suspect."""
        content = (
            "a@a.com b@b.com\n"
            "13800138000\n"
        )
        p = _write(tmp_path, "threshold.txt", content)
        v = assess([p])
        assert v.level == "suspect"


# ─── Clean content ────────────────────────────────────────────────────────────

class TestClean:
    def test_plain_text_clean(self, tmp_path):
        p = _write(tmp_path, "readme.txt", "Hello, NimoOS!\nThis is a plain text file.\n")
        v = assess([p])
        assert v.level == "clean"

    def test_empty_files_list_no_inline(self):
        v = assess([])
        assert v.level == "clean"

    def test_empty_inline_payload(self):
        v = assess([], inline_payload=b"")
        assert v.level == "clean"

    def test_inline_plain_text_clean(self):
        v = assess([], inline_payload=b"just some normal data\n")
        assert v.level == "clean"


# ─── Unreadable / missing file ────────────────────────────────────────────────

class TestUnreadable:
    def test_nonexistent_file_suspect(self):
        v = assess(["/nonexistent/path/ghost.txt"])
        assert v.level == "suspect"
        assert "could not read" in v.reason

    def test_path_blacklist_wins_over_missing(self):
        """Blacklisted path → block even if file doesn't exist (no I/O)."""
        v = assess(["/home/user/.ssh/nonexistent_key"])
        assert v.level == "block"


# ─── Priority ordering ────────────────────────────────────────────────────────

class TestPriority:
    def test_block_beats_suspect(self, tmp_path):
        """First file is a JWT (suspect), second file has a private key (block)."""
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9"
            ".eyJzdWIiOiJ1c2VyIn0"
            ".abcdefghijklmnopqrst"
        )
        suspect_file = _write(tmp_path, "jwt.txt", f"token: {jwt}")
        block_file = _write(tmp_path, "priv.txt", "-----BEGIN RSA PRIVATE KEY-----\n")
        v = assess([suspect_file, block_file])
        assert v.level == "block"

    def test_path_blacklist_before_content(self, tmp_path):
        """Even if the file is clean content, matching path → block (no file read)."""
        p = _write(tmp_path, "safe_content.pem", "hello world no secrets here\n")
        v = assess([p])
        assert v.level == "block"

    def test_inline_suspect_when_files_clean(self, tmp_path):
        """Files are clean but inline payload triggers suspect."""
        clean = _write(tmp_path, "clean.txt", "hello world")
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9"
            ".eyJzdWIiOiJ1c2VyIn0"
            ".abcdefghijklmnopqrst"
        )
        v = assess([clean], inline_payload=jwt.encode())
        assert v.level == "suspect"
