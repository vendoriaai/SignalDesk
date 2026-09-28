"""Article text extraction — read the news, not just the snippet.

The scan's catalysts/context used to be one search-engine snippet deep.
This module fetches a top article's own text so a claim can quote the
article's opening: Tavily's extract API when a key is configured (reliable,
handles JS-heavy pages), otherwise a direct page fetch stripped with the
stdlib HTML parser. Any failure returns "" and the caller keeps the snippet
(rule R4: degrade with disclosure, never fail the scan over a news page).
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

import httpx

USER_AGENT = "SignalDesk/1.0 (research agent; contact: project maintainer)"
_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "form", "iframe", "header", "aside"}
_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "article", "section", "tr", "blockquote"}


class _TextExtractor(HTMLParser):
    """Collect visible text; skip script/style chrome; break on block tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"[ \t]+", " ", raw)
        return re.sub(r"\n{2,}", "\n", raw).strip()


def _strip_html(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
    except Exception:
        return ""
    return parser.text()


def _first_sentences(text: str, max_chars: int) -> str:
    """Whole sentences up to the budget; never cut mid-word mid-sentence."""
    if len(text) <= max_chars:
        return text
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        candidate = f"{out} {sentence}".strip()
        if len(candidate) > max_chars:
            break
        out = candidate
    return out or text[:max_chars].rsplit(" ", 1)[0]


def _tavily_extract(url: str, api_key: str, timeout: float) -> str:
    try:
        resp = httpx.post(
            "https://api.tavily.com/extract",
            json={"api_key": api_key, "urls": [url]},
            timeout=timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return unescape(str(results[0].get("raw_content") or "")) if results else ""
    except Exception:
        return ""


def _direct_fetch(url: str, timeout: float) -> str:
    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        resp.raise_for_status()
        content_type = str(resp.headers.get("content-type", ""))
        if content_type and "html" not in content_type and "xml" not in content_type and "text" not in content_type:
            return ""
        return _strip_html(resp.text[:400_000])
    except Exception:
        return ""


def extract_article(url: str, *, tavily_api_key: str | None = None,
                    max_chars: int = 600, timeout: float = 8.0) -> str:
    """Readable opening text of `url`, or "" when nothing could be fetched."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    text = ""
    if tavily_api_key:
        text = _tavily_extract(url, tavily_api_key, timeout)
    if not text:
        text = _direct_fetch(url, timeout)
    if not text:
        return ""
    return _first_sentences(text, max_chars)
