"""Run event stream: the typed trace every workflow emits.

Each event is one UI/monitoring unit (a search query, a tool call, a sandbox
run, a result). The CLI prints them as JSONL; the M3 UI consumes the same bus
over a WebSocket.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field


class EventKind(str, Enum):
    PHASE = "phase"
    STEP = "step"
    TOOL = "tool"
    SEARCH = "search"
    SANDBOX = "sandbox"
    CHART = "chart"
    RESULT = "result"
    WARN = "warn"


class Event(BaseModel):
    seq: int
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    phase: str
    kind: EventKind
    message: str
    data: dict[str, Any] = {}


class EventBus:
    def __init__(self, sink: Callable[[Event], None] | None = None):
        self.events: list[Event] = []
        self._sink = sink
        self._seq = 0

    def emit(self, phase: str, kind: EventKind, message: str, **data: Any) -> Event:
        self._seq += 1
        event = Event(seq=self._seq, phase=phase, kind=kind, message=message, data=data)
        self.events.append(event)
        if self._sink:
            self._sink(event)
        return event

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(e.model_dump(), default=str) for e in self.events)
