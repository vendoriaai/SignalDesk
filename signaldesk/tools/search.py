"""Pluggable web search (TAD FR-3): Tavily BYOK, DuckDuckGo fallback.

Results are unstructured research input, not numbers: every claim the agent
reuses must carry its URL as a web citation (workflows.md R1).
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from .base import Source


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str
    published: str = ""


class SearchTool:
    """Not a CSV tool — research returns hits, so it sits beside Tool."""

    name = "web_search"

    def __init__(self, tavily_api_key: str | None = None):
        self.tavily_api_key = tavily_api_key

    def available(self) -> bool:
        return bool(self.tavily_api_key) or _ddgs_available()

    def query(self, q: str, max_results: int = 4) -> list[SearchHit]:
        if self.tavily_api_key:
            try:
                return self._tavily(q, max_results)
            except Exception:
                pass  # fall through to DDG
        if _ddgs_available():
            try:
                return self._ddg(q, max_results)
            except Exception:
                return []
        return []

    def batch(self, queries: list[str], max_results: int = 3) -> dict[str, list[SearchHit]]:
        return {q: self.query(q, max_results=max_results) for q in queries}

    def _tavily(self, q: str, max_results: int) -> list[SearchHit]:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.tavily_api_key,
                "query": q,
                "max_results": max_results,
                "search_depth": "basic",
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        return [
            SearchHit(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
                published=datetime.now(UTC).date().isoformat(),
            )
            for r in resp.json().get("results", [])
        ]

    def _ddg(self, q: str, max_results: int) -> list[SearchHit]:
        from ddgs import DDGS  # optional extra

        with DDGS() as ddgs:
            return [
                SearchHit(title=r.get("title", ""), url=r.get("href", ""), snippet=r.get("body", ""))
                for r in ddgs.text(q, max_results=max_results)
            ]


def _ddgs_available() -> bool:
    try:
        import ddgs  # noqa: F401
    except ImportError:
        return False
    return True


def hits_to_sources(hits: list[SearchHit]) -> list[Source]:
    return [Source(name=h.title or h.url, url=h.url, retrieved_at=datetime.now(UTC).isoformat(timespec="seconds")) for h in hits]
