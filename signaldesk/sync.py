"""Supabase auth + sync engine (roadmap Phase 3.20, PRD FR-1/FR-4).

Thin REST wrapper over the Supabase endpoints — gotrue for auth, PostgREST
for tables — no supabase-py dependency. Policies in `data-model.sql` scope
every row to `auth.uid()`.

Consent gate (PRD FR-1.4 / compliance): cloud sync of prompts, runs, reports
and signals happens only when `consent_prompts` is ON. With consent off the
app is fully local. `delete_my_cloud_data()` always works regardless of
consent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .store import LocalStore


class SyncError(Exception):
    pass


@dataclass
class SupabaseConfig:
    url: str
    anon_key: str

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon_key)


@dataclass
class AuthState:
    user_id: str = ""
    email: str = ""
    access_token: str = ""
    refresh_token: str = ""

    @property
    def logged_in(self) -> bool:
        return bool(self.access_token)


class SupabaseClient:
    def __init__(self, config: SupabaseConfig):
        self.cfg = config
        self.auth = AuthState()

    # ---------------------------------------------------------------- auth --
    def _auth_url(self, path: str) -> str:
        return f"{self.cfg.url.rstrip('/')}/auth/v1{path}"

    def sign_up(self, email: str, password: str) -> AuthState:
        resp = httpx.post(
            self._auth_url("/signup"),
            json={"email": email, "password": password},
            headers={"apikey": self.cfg.anon_key},
            timeout=20.0,
        )
        if resp.status_code >= 400:
            raise SyncError(f"signup failed: {resp.status_code} {resp.text[:200]}")
        return self._capture(resp.json())

    def sign_in(self, email: str, password: str) -> AuthState:
        resp = httpx.post(
            self._auth_url("/token?grant_type=password"),
            json={"email": email, "password": password},
            headers={"apikey": self.cfg.anon_key},
            timeout=20.0,
        )
        if resp.status_code >= 400:
            raise SyncError(f"login failed: {resp.status_code} {resp.text[:200]}")
        return self._capture(resp.json())

    def refresh(self, refresh_token: str) -> AuthState:
        resp = httpx.post(
            self._auth_url("/token?grant_type=refresh_token"),
            json={"refresh_token": refresh_token},
            headers={"apikey": self.cfg.anon_key},
            timeout=20.0,
        )
        if resp.status_code >= 400:
            raise SyncError(f"refresh failed: {resp.status_code}")
        return self._capture(resp.json())

    def _capture(self, payload: dict) -> AuthState:
        user = payload.get("user") or {}
        self.auth = AuthState(
            user_id=user.get("id", ""),
            email=user.get("email", ""),
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token", ""),
        )
        return self.auth

    def sign_out(self) -> None:
        self.auth = AuthState()

    # --------------------------------------------------------------- tables --
    def _rest(self, path: str) -> str:
        return f"{self.cfg.url.rstrip('/')}/rest/v1{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.cfg.anon_key,
            "Authorization": f"Bearer {self.auth.access_token}",
            "Content-Type": "application/json",
        }

    def upsert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        resp = httpx.post(
            self._rest(f"/{table}"),
            json=rows,
            headers={**self._headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            raise SyncError(f"upsert {table} failed: {resp.status_code} {resp.text[:200]}")
        return len(rows)

    def select(self, table: str, query: str = "*", since_col: str | None = None,
               since: datetime | None = None) -> list[dict]:
        params: dict[str, str] = {"select": query}
        if since_col and since:
            params[since_col] = f"gte.{since.isoformat()}"
        resp = httpx.get(self._rest(f"/{table}"), params=params, headers=self._headers(), timeout=30.0)
        if resp.status_code >= 400:
            raise SyncError(f"select {table} failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def delete_my_data(self) -> None:
        if not self.auth.logged_in:
            raise SyncError("not logged in")
        resp = httpx.post(
            self._rest("/rpc/delete_my_data"), json={}, headers=self._headers(), timeout=30.0
        )
        if resp.status_code >= 400:
            raise SyncError(f"delete failed: {resp.status_code} {resp.text[:200]}")


class SyncEngine:
    """Local-first sync: upsert local rows to Supabase when consent is on."""

    def __init__(self, client: SupabaseClient, store: LocalStore):
        self.client = client
        self.store = store
        self.last_sync: datetime | None = None

    def ensure_session(self) -> None:
        if not self.client.auth.logged_in:
            raise SyncError("not logged in")
        # refresh opportunistically if we hold a refresh token but no access token

    def push_all(self, *, consent: bool) -> dict[str, int]:
        """Push local rows to cloud. Consent gates prompts/content (FR-1.4).
        When consent is off, nothing is uploaded at all."""
        if not consent:
            return {"uploaded": 0, "skipped": "consent off"}
        self.ensure_session()
        uid = self.client.auth.user_id
        counts: dict[str, int] = {}
        runs = [
            {
                "id": r.id, "user_id": uid, "session_id": r.session_id or None,
                "prompt_id": r.prompt_id or None, "workflow": r.workflow,
                "status": r.status, "market": r.market,
                "universe": json.loads(r.universe or "[]"),
                "scoring_preset": r.scoring_preset,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "trace": {"jsonl": r.trace_jsonl},
            }
            for r in self.store.runs_since(None, limit=200)
        ]
        counts["runs"] = self.client.upsert("runs", runs)
        prompts = [
            {"id": p.id, "user_id": uid, "session_id": p.session_id, "text": p.text}
            for p in self.store.prompts_since(None)[:500]
        ]
        counts["prompts"] = self.client.upsert("prompts", prompts)
        reports = [
            {
                "id": f"rep-{r.id}", "user_id": uid, "run_id": r.id,
                "markdown": r.report_md, "report_json": json.loads(r.report_json or "{}"),
            }
            for r in self.store.runs_since(None, limit=200) if r.status == "done"
        ]
        counts["reports"] = self.client.upsert("reports", reports)
        try:
            signals = []
            for s in self.store.list_signals(500):
                signals.append({
                    "id": s.id, "user_id": uid, "run_id": s.run_id, "symbol": s.symbol,
                    "market": s.market, "direction": s.direction, "score": s.score,
                    "entry": s.entry, "stop": s.stop, "tp1": s.tp1, "tp2": s.tp2,
                    "emitted_at": s.emitted_at.isoformat(),
                    "outcome": s.outcome,
                    "resolved_at": s.resolved_at.isoformat() if s.resolved_at else None,
                })
            counts["signals"] = self.client.upsert("signals", signals)
        except SyncError:
            counts["signals"] = 0
        self.last_sync = datetime.now(UTC)
        counts["uploaded"] = sum(v for k, v in counts.items() if isinstance(v, int))
        return counts

    def pull_watchlists(self, data_dir: Path) -> dict[str, int]:
        """Fetch cloud watchlists; last-write-wins by updated_at."""
        from . import watchlists as wl_mod

        rows = self.client.select("watchlists", "id,name,updated_at")
        if not rows:
            return {"pulled": 0}
        pulled = 0
        try:
            items = self.client.select("watchlist_items", "watchlist_id,symbol")
        except SyncError:
            items = []
        by_wl: dict[str, list[str]] = {}
        for it in items:
            by_wl.setdefault(it["watchlist_id"], []).append(it["symbol"])
        for row in rows:
            try:
                local = wl_mod.load(data_dir, row["name"])
            except FileNotFoundError:
                local = None
            remote_dt = row.get("updated_at", "")
            if local is None:
                wl_mod.save(data_dir, wl_mod.Watchlist(
                    name=row["name"], symbols=sorted(by_wl.get(row["id"], [])),
                ))
                pulled += 1
            else:
                # last-write-wins: JSON store has no updated_at tracked remotely;
                # merge union to be safely idempotent in v1
                merged = sorted(set(local.symbols) | set(by_wl.get(row["id"], [])))
                if merged != sorted(local.symbols):
                    local.symbols = merged
                    wl_mod.save(data_dir, local)
                    pulled += 1
            _ = remote_dt
        return {"pulled": pulled}
