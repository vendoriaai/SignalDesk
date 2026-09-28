"""Uniform read-only tool interface (TAD 3.2).

All data acquisition flows through `Tool.run()`, which returns CSV artifacts
plus a human summary plus web sources. The headless core runs tools
synchronously and sequentially; async orchestration arrives with the LangGraph
/UI engine in M3.

Registry guard: a tool whose name looks write-side (send/create/update/delete)
is rejected unless a `trading` capability flag is explicitly enabled — nothing
in v1 is allowed to touch an account.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field


class Source(BaseModel):
    name: str
    url: str = ""
    retrieved_at: str = ""


class ToolResult(BaseModel):
    csv_files: list[Path] = []
    summary: str = ""
    sources: list[Source] = Field(default_factory=list)
    degraded: bool = False  # True when the tool failed soft (see rule R4)


_WRITE_PREFIXES = ("send_", "create_", "update_", "delete_", "place_", "order")


class Tool(ABC):
    name: str = "tool"
    description: str = ""

    @abstractmethod
    def run(self, **kwargs) -> ToolResult: ...


class ToolRegistry:
    def __init__(self, allow_trading: bool = False) -> None:
        self.allow_trading = allow_trading
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name.startswith(_WRITE_PREFIXES) and not self.allow_trading:
            raise PermissionError(f"write-side tool '{tool.name}' rejected (read-only v1)")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)


class TokenBucket:
    """Minimal per-adapter rate limiter: capacity tokens, refilled over time."""

    def __init__(self, rate_per_sec: float, capacity: int = 4) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            time.sleep(max(0.0, (1 - self._tokens) / self.rate))
