import os
import re

# Set AGENT_DB_PATH before any test module is imported, so that main.py's
# module-level db init uses an in-memory SQLite rather than the on-disk path.
# This is needed for tests that import `main` at module level (e.g. TestClient).
os.environ.setdefault("AGENT_DB_PATH", ":memory:")


_FENCE_RE = re.compile(
    r'\A<untrusted-data source="(?P<source>[^"]*)">\n(?P<body>.*)\n'
    r"</untrusted-data>\Z",
    re.DOTALL,
)


def unfence(value, *, source=None):
    """Assert `value` is a `<untrusted-data>` fence and return its body.

    External content injected into the agent's context is wrapped by
    fences.fence_untrusted (L3 injection guardrail). Tests that care about the
    payload use this to look inside, so the assertion still covers the content
    AND pins the fence in place — dropping the fence makes them fail.
    """
    assert isinstance(value, str), f"expected a fenced string, got {type(value)}"
    m = _FENCE_RE.match(value)
    assert m is not None, f"expected an <untrusted-data> fence, got: {value!r}"
    if source is not None:
        assert m.group("source") == source, (
            f'fence source is {m.group("source")!r}, expected {source!r}'
        )
    return m.group("body")
