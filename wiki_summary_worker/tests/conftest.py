"""Pytest config: make the wiki_summary_worker package importable from tests."""
import sys
from pathlib import Path

# Ensure NimoOS-AI/ is on sys.path so `import wiki_summary_worker` works.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
