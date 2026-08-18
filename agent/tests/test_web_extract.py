"""web/extract.py — HTML to markdown with a zero-dependency backstop."""
from __future__ import annotations

from web import extract

_PAGE = """<!doctype html>
<html><head><title>  Hello Title  </title></head>
<body>
  <nav>menu menu menu</nav>
  <article><h1>Real Heading</h1>
  <p>The body paragraph that carries the meaning of the page.</p></article>
  <footer>copyright boilerplate</footer>
</body></html>"""


def test_extracts_the_body_text():
    md = extract.to_markdown(_PAGE, url="https://x.test/a")
    assert "The body paragraph that carries the meaning of the page." in md


def test_title_is_trimmed():
    assert extract.title_of(_PAGE) == "Hello Title"


def test_title_missing_returns_empty():
    assert extract.title_of("<html><body>no title</body></html>") == ""


def test_backstop_strips_tags_when_extractor_unavailable(monkeypatch):
    monkeypatch.setattr(extract, "_try_trafilatura", lambda html, url: "")
    md = extract.to_markdown(_PAGE, url="https://x.test/a")
    assert "The body paragraph" in md
    assert "<p>" not in md
    assert "<script" not in md


def test_backstop_drops_script_and_style_contents(monkeypatch):
    monkeypatch.setattr(extract, "_try_trafilatura", lambda html, url: "")
    html = ("<html><body><style>.a{color:red}</style>"
            "<script>var secret = 1;</script><p>visible</p></body></html>")
    md = extract.to_markdown(html)
    assert "visible" in md
    assert "color:red" not in md
    assert "var secret" not in md


def test_never_returns_none_for_empty_input():
    assert extract.to_markdown("") == ""
