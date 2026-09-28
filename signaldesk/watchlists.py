"""Local watchlist store (PRD FR-5, workflow WF-4).

v0 persists to the local data dir as JSON; the M3 sync layer replaces the
backend with SQLite + Supabase upsert (last-writer-wins) without touching
callers. A watchlist records its market so scans pick the right adapters.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class Watchlist:
    name: str
    market: str = "crypto"
    symbols: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    last_scan_report: str | None = None  # path to the previous report.json (diff source)


def _wl_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "watchlists"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(data_dir: Path, name: str) -> Path:
    safe = "".join(c for c in name.lower().strip() if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError("watchlist name must contain letters or digits")
    return _wl_dir(data_dir) / f"{safe}.json"


def save(data_dir: Path, wl: Watchlist) -> Path:
    p = _path(data_dir, wl.name)
    p.write_text(json.dumps(wl.__dict__, indent=2), encoding="utf-8")
    return p


def load(data_dir: Path, name: str) -> Watchlist:
    p = _path(data_dir, name)
    if not p.exists():
        raise FileNotFoundError(f"watchlist '{name}' not found in {_wl_dir(data_dir)}")
    return Watchlist(**json.loads(p.read_text(encoding="utf-8")))


def upsert(data_dir: Path, wl: Watchlist) -> None:
    try:
        save(data_dir, wl)
    except FileNotFoundError:
        pass


def list_all(data_dir: Path) -> list[Watchlist]:
    out = []
    for p in sorted(_wl_dir(data_dir).glob("*.json")):
        try:
            out.append(Watchlist(**json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def add_symbols(data_dir: Path, name: str, symbols: list[str], market: str = "crypto") -> Watchlist:
    try:
        wl = load(data_dir, name)
    except FileNotFoundError:
        wl = Watchlist(name=name.lower(), market=market)
    for s in symbols:
        s = s.upper()
        if s not in wl.symbols:
            wl.symbols.append(s)
    save(data_dir, wl)
    return wl


def remove_symbols(data_dir: Path, name: str, symbols: list[str]) -> Watchlist:
    wl = load(data_dir, name)
    wl.symbols = [s for s in wl.symbols if s.upper() not in {x.upper() for x in symbols}]
    save(data_dir, wl)
    return wl
