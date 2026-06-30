import os

# Set AGENT_DB_PATH before any test module is imported, so that main.py's
# module-level db init uses an in-memory SQLite rather than the on-disk path.
# This is needed for tests that import `main` at module level (e.g. TestClient).
os.environ.setdefault("AGENT_DB_PATH", ":memory:")
