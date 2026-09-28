"""Background daily outcome resolution for the desktop server (WF-5, item 28).

While the app is open, open ledger signals should keep resolving even if
nobody runs `signaldesk outcomes`. The server loop checks hourly and runs one
resolution pass per local day — immediately on startup when today's pass
hasn't happened yet, which covers the typical open-app-in-the-evening pattern.

A failed pass (no network, data source down) is retried on the next hourly
check rather than being marked done for the day; the failure is recorded in
the state file and shown on the dashboard (degrade-and-disclose, rule R4).
Disable entirely with SIGNALDESK_SCHEDULER=0 (tests, CI).

State lives in `<data_dir>/outcomes_state.json` so restarts don't re-resolve
more than once a day and the dashboard can show when resolution last ran.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import date, datetime
from pathlib import Path

from . import resolver as resolver_mod

CHECK_INTERVAL_S = 3600
STATE_FILENAME = "outcomes_state.json"

_lock = threading.Lock()


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_FILENAME


def load_state(data_dir: Path) -> dict:
    path = state_path(data_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def due(state: dict, today: date | None = None) -> bool:
    """A daily pass is due when none has succeeded yet today (local date)."""
    return str(state.get("last_resolution_date", "")) != str((today or date.today()).isoformat())


def run_if_due(data_dir: Path, *, now: datetime | None = None) -> dict | None:
    """Run one resolution pass when today's hasn't succeeded yet.

    Returns the updated state dict, or None when no pass was due. A lock keeps
    the hourly loop and a manual trigger from resolving concurrently.
    """
    with _lock:
        now = now or datetime.now()
        state = load_state(data_dir)
        if not due(state, now.date()):
            return None
        state.update({"last_run_at": now.isoformat(timespec="seconds")})
        try:
            rows = resolver_mod.run_resolution(data_dir)
        except Exception as exc:
            # not marked done for the day: the next check retries
            state.update({"last_status": "error",
                          "last_error": f"{type(exc).__name__}: {exc}"})
            save_state(data_dir, state)
            return state
        state.update({"last_status": "ok", "last_error": "",
                      "last_resolution_date": now.date().isoformat(),
                      "last_counts": {"rows": len(rows)}})
        save_state(data_dir, state)
        return state


async def loop(data_dir: Path, stop_event: asyncio.Event) -> None:
    """Hourly check; resolves when the day's pass is still pending."""
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(run_if_due, data_dir)
        except Exception:
            pass  # never kill the server loop; run_if_due records failures
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
