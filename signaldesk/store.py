"""Local-first persistence (roadmap Phase 3.17, TAD 3.5).

SQLite via SQLModel is the durable local cache: chat sessions, prompts, runs,
reports, signals. All data stays queryable offline; the sync layer upserts to
Supabase when (and only when) consent is on.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class SessionRow(SQLModel, table=True):
    __tablename__ = "chat_sessions"
    id: str = Field(default_factory=_new_id, primary_key=True)
    title: str = "New session"
    created_at: datetime = Field(default_factory=_now)
    last_used_at: datetime = Field(default_factory=_now)


class PromptRow(SQLModel, table=True):
    __tablename__ = "prompts"
    id: str = Field(default_factory=_new_id, primary_key=True)
    session_id: str = Field(index=True)
    text: str
    created_at: datetime = Field(default_factory=_now)


class RunRow(SQLModel, table=True):
    __tablename__ = "runs"
    id: str = Field(default_factory=_new_id, primary_key=True)
    session_id: str = Field(default="", index=True)
    prompt_id: str = ""
    workflow: str = ""            # market_scan | forex_scan | deep_dive | watchlist_scan
    market: str = ""
    universe: str = "[]"          # JSON array
    scoring_preset: str = ""
    status: str = "running"       # running | done | failed
    report_md: str = ""
    report_json: str = "{}"
    trace_jsonl: str = ""
    error: str = ""
    run_dir: str = ""             # on-disk run dir (charts live inside)
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None


class SignalRow(SQLModel, table=True):
    __tablename__ = "signals"
    id: str = Field(default_factory=_new_id, primary_key=True)
    run_id: str = Field(index=True)
    symbol: str
    market: str
    direction: str = "LONG"
    score: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    emitted_at: datetime = Field(default_factory=_now)
    outcome: str = "OPEN"         # WF-5 (v1.1) fills TP1/TP2/SL
    resolved_at: datetime | None = None


class LocalStore:
    """SQLite-backed repository with the exact row shapes Supabase mirrors."""

    def __init__(self, data_dir: Path):
        self.db_path = Path(data_dir) / "signaldesk.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
        SQLModel.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Minimal column add for databases created before run_dir existed."""
        from sqlalchemy import text

        with self.engine.begin() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(runs)"))}
            if "run_dir" not in cols:
                conn.execute(text("ALTER TABLE runs ADD COLUMN run_dir TEXT DEFAULT ''"))

    # sessions ----------------------------------------------------------
    def create_session(self, title: str = "New session") -> SessionRow:
        with Session(self.engine) as s:
            row = SessionRow(title=title)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def touch_session(self, session_id: str) -> None:
        with Session(self.engine) as s:
            row = s.get(SessionRow, session_id)
            if row:
                row.last_used_at = _now()
                s.add(row)
                s.commit()

    def list_sessions(self, limit: int = 50) -> list[SessionRow]:
        with Session(self.engine) as s:
            return list(s.exec(select(SessionRow).order_by(SessionRow.last_used_at.desc()).limit(limit)))

    # prompts ------------------------------------------------------------
    def add_prompt(self, session_id: str, text: str) -> PromptRow:
        with Session(self.engine) as s:
            row = PromptRow(session_id=session_id, text=text)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    # runs ----------------------------------------------------------------
    def create_run(self, *, session_id: str, prompt_id: str, workflow: str,
                   market: str, universe: list[str]) -> RunRow:
        import json

        with Session(self.engine) as s:
            row = RunRow(session_id=session_id, prompt_id=prompt_id, workflow=workflow,
                         market=market, universe=json.dumps(universe))
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def finish_run(self, run_id: str, *, report_md: str, report_json: str,
                   trace_jsonl: str, scoring_preset: str, run_dir: str = "") -> None:
        with Session(self.engine) as s:
            row = s.get(RunRow, run_id)
            if not row:
                return
            row.status = "done"
            row.report_md = report_md
            row.report_json = report_json
            row.trace_jsonl = trace_jsonl
            row.scoring_preset = scoring_preset
            if run_dir:
                row.run_dir = run_dir
            row.finished_at = _now()
            s.add(row)
            s.commit()

    def fail_run(self, run_id: str, error: str) -> None:
        with Session(self.engine) as s:
            row = s.get(RunRow, run_id)
            if not row:
                return
            row.status = "failed"
            row.error = error
            row.finished_at = _now()
            s.add(row)
            s.commit()

    def get_run(self, run_id: str) -> RunRow | None:
        with Session(self.engine) as s:
            return s.get(RunRow, run_id)

    def list_runs(self, session_id: str | None = None, limit: int = 50) -> list[RunRow]:
        with Session(self.engine) as s:
            q = select(RunRow).order_by(RunRow.started_at.desc()).limit(limit)
            if session_id:
                q = q.where(RunRow.session_id == session_id)
            return list(s.exec(q))

    # signals --------------------------------------------------------------
    def add_signals(self, run_id: str, market: str, signals: list[dict]) -> None:
        with Session(self.engine) as s:
            for sig in signals:
                s.add(SignalRow(run_id=run_id, market=market, **sig))
            s.commit()

    def list_signals(self, limit: int = 200) -> list[SignalRow]:
        with Session(self.engine) as s:
            return list(s.exec(select(SignalRow).order_by(SignalRow.emitted_at.desc()).limit(limit)))

    # utilities for sync -----------------------------------------------------
    def runs_since(self, since: datetime | None, limit: int = 500) -> list[RunRow]:
        with Session(self.engine) as s:
            q = select(RunRow).order_by(RunRow.started_at).limit(limit)
            if since:
                q = q.where(RunRow.started_at >= since)
            return list(s.exec(q))

    def prompts_since(self, since: datetime | None) -> list[PromptRow]:
        with Session(self.engine) as s:
            q = select(PromptRow).order_by(PromptRow.created_at)
            if since:
                q = q.where(PromptRow.created_at >= since)
            return list(s.exec(q))

    def sessions_all(self) -> list[SessionRow]:
        with Session(self.engine) as s:
            return list(s.exec(select(SessionRow)))
