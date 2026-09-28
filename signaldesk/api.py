"""FastAPI backend (roadmap Phase 3.17): chat-driven runs with live WebSocket
streaming, history, watchlists, settings, auth/sync.

    signaldesk serve --port 8787          # then open http://127.0.0.1:8787

Run model: POST /api/chat -> planner -> workflow thread -> EventBus events
fanned out to per-run queues -> WebSocket clients of that run -> report +
trace persisted to SQLite (synced to Supabase only with consent on).
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .agent.events import Event, EventBus, EventKind
from .agent.planner import Workflow, classify
from .config import Config
from .store import LocalStore

_DONE_SENTINEL = object()


class RunManager:
    """Thread-safe buffers + subscriber queues for live run streaming."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, list[Event]] = {}
        self._status: dict[str, str] = {}
        self._queues: dict[str, list[queue.Queue]] = {}

    def start(self, run_id: str) -> None:
        with self._lock:
            self._events[run_id] = []
            self._status[run_id] = "running"

    def append(self, run_id: str, event: Event) -> None:
        with self._lock:
            self._events.setdefault(run_id, []).append(event)
            targets = list(self._queues.get(run_id, []))
        for q in targets:
            q.put(event)

    def finish(self, run_id: str, status: str) -> None:
        with self._lock:
            self._status[run_id] = status
            targets = list(self._queues.get(run_id, []))
        for q in targets:
            q.put(_DONE_SENTINEL)

    def snapshot(self, run_id: str) -> tuple[list[Event], str]:
        with self._lock:
            return list(self._events.get(run_id, [])), self._status.get(run_id, "unknown")

    def subscribe(self, run_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._queues.setdefault(run_id, []).append(q)
            status = self._status.get(run_id, "unknown")
        if status != "running":
            q.put(_DONE_SENTINEL)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue) -> None:
        with self._lock:
            if q in self._queues.get(run_id, []):
                self._queues[run_id].remove(q)


class ChatIn(BaseModel):
    prompt: str
    demo: bool = False
    session_id: str | None = None


class ScanRequestIn(BaseModel):
    market: str = "crypto"
    demo: bool = False
    universe_size: int = 12
    entry_timeframes: list[str] | None = None  # None = settings/default; [] = off


class DeepDiveIn(BaseModel):
    symbol: str
    market: str = "equities"
    demo: bool = False
    entry_timeframes: list[str] | None = None


class AuthIn(BaseModel):
    email: str
    password: str


class WatchlistIn(BaseModel):
    name: str
    symbols: list[str] = []
    market: str = "crypto"


class SymbolsIn(BaseModel):
    add: list[str] = []
    remove: list[str] = []


class SettingsIn(BaseModel):
    consent_prompts: bool | None = None
    llm_provider: str | None = None
    llm_model: str | None = None      # "" resets to provider default
    theme: str | None = None
    entry_refinement: bool | None = None  # intraday (30m/15m/5m/1m) entry plans
    secrets: dict[str, str] = {}  # SECRET_KEYS -> value; "" clears


class ResolveIn(BaseModel):
    market: str = ""
    demo: bool = False
    include_demo: bool = False
    horizon: int = 14


class PaperIn(BaseModel):
    price: float | None = None        # actual fill price (fills only)
    note: str = ""


def create_app(config: Config | None = None) -> FastAPI:
    import os

    from . import userconfig

    config = config or Config.from_env()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    store = LocalStore(config.data_dir)
    settings = userconfig.Settings(config.data_dir)
    runs = RunManager()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> None:
        """Start/stop the daily outcome-resolution loop (WF-5; tests may
        disable it with SIGNALDESK_SCHEDULER=0)."""
        stop = asyncio.Event()
        task = None
        if os.environ.get("SIGNALDESK_SCHEDULER", "1").strip().lower() not in ("0", "false", "off"):
            from . import scheduler as scheduler_mod

            task = asyncio.create_task(scheduler_mod.loop(config.data_dir, stop))
        yield
        if task is not None:
            stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    app = FastAPI(title="SignalDesk", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )
    app.state.config = config
    app.state.store = store

    # --- tool builders -------------------------------------------------------
    def _tools(market: str, demo: bool, artifacts_dir: Path):
        from .cli import _demo_tools, _live_tools

        return _demo_tools(market, artifacts_dir) if demo else _live_tools(config, market, artifacts_dir)

    def _deep_tools(market: str, demo: bool, artifacts_dir: Path):
        from .workflows.deep_dive import DeepDiveToolSet

        if demo:
            from .tools import demo as d
            from .tools import demo_equities as de

            return DeepDiveToolSet(
                quotes=d.DemoQuotesTool(artifacts_dir, market),
                ohlcv=d.DemoOHLCVTool(artifacts_dir, market),
                search=d.DemoSearchTool(),
                fundamentals=(de.DemoFundamentalsTool(artifacts_dir) if market == "equities" else None),
                edgar=(de.DemoEdgarTool(artifacts_dir) if market == "equities" else None),
                earnings=(de.DemoEarningsTool(artifacts_dir) if market == "equities" else None),
            )
        from .tools.equities import EarningsTool, EdgarFactsTool, FundamentalsTool
        from .tools.search import SearchTool
        from .tools.yfinance_tools import YFinanceOHLCVTool, YFinanceQuotesTool

        return DeepDiveToolSet(
            quotes=YFinanceQuotesTool(artifacts_dir, market),
            ohlcv=YFinanceOHLCVTool(artifacts_dir, market),
            search=SearchTool(tavily_api_key=config.tavily_api_key),
            fundamentals=(FundamentalsTool(artifacts_dir) if market == "equities" else None),
            edgar=(EdgarFactsTool(artifacts_dir) if market == "equities" else None),
            earnings=(EarningsTool(artifacts_dir) if market == "equities" else None),
        )

    # --- run launchers (worker threads) --------------------------------------
    def _entry_timeframes(override: list[str] | None) -> list[str] | None:
        """Settings toggle gates the default; explicit overrides win."""
        if override is not None:
            return override
        return None if settings.get("entry_refinement") else []

    def _launch_scan(run_db_id: str, market: str, demo: bool, universe_size: int,
                     watchlist: str | None = None,
                     entry_timeframes: list[str] | None = None) -> None:
        from .workflows.market_scan import MarketScanRequest, run_market_scan

        run_dir = config.run_dir(datetime.now(UTC).strftime("api-%Y%m%dT%H%M%SZ-%f") + ("-demo" if demo else ""))
        universe_override, wl_name = None, None
        if watchlist:
            from . import watchlists as wl_mod

            wl = wl_mod.load(config.data_dir, watchlist)
            universe_override, market = wl.symbols, wl.market
            wl_name = watchlist
        bus = EventBus(sink=lambda ev: runs.append(run_db_id, ev))
        try:
            run = run_market_scan(
                MarketScanRequest(market=market, universe_size=universe_size,
                                  universe_override=universe_override, watchlist_name=wl_name,
                                  entry_timeframes=_entry_timeframes(entry_timeframes)),
                _tools(market, demo, run_dir / "artifacts"), bus, run_dir,
            )
            store.finish_run(
                run_db_id, report_md=run.report_md,
                report_json=json.dumps(run.report.model_dump(mode="json"), default=str),
                trace_jsonl=bus.to_jsonl(), scoring_preset=run.report.scoring_preset,
                run_dir=str(run_dir),
            )
            store.add_signals(
                run_db_id, market,
                [dict(symbol=s.symbol, direction=s.direction, score=s.score, entry=s.entry,
                      stop=s.stop, tp1=s.tp1, tp2=s.tp2) for s in run.report.signals],
            )
            runs.finish(run_db_id, "done")
        except Exception as exc:
            runs.append(run_db_id, Event(seq=0, phase="run", kind=EventKind.WARN,
                                         message=f"run failed: {exc}"))
            store.fail_run(run_db_id, str(exc))
            runs.finish(run_db_id, "failed")

    def _launch_deepdive(run_db_id: str, symbol: str, market: str, demo: bool,
                         entry_timeframes: list[str] | None = None) -> None:
        from .workflows.deep_dive import DeepDiveRequest, run_deep_dive

        run_dir = config.run_dir(datetime.now(UTC).strftime("api-%Y%m%dT%H%M%SZ-dd-%f"))
        bus = EventBus(sink=lambda ev: runs.append(run_db_id, ev))
        try:
            run = run_deep_dive(
                DeepDiveRequest(symbol=symbol, market=market,
                                entry_timeframes=_entry_timeframes(entry_timeframes)),
                _deep_tools(market, demo, run_dir / "artifacts"), bus, run_dir,
            )
            store.finish_run(
                run_db_id, report_md=run.report_md,
                report_json=json.dumps(run.report.model_dump(mode="json"), default=str),
                trace_jsonl=bus.to_jsonl(), scoring_preset="trend-momentum-v1",
                run_dir=str(run_dir),
            )
            runs.finish(run_db_id, "done")
        except Exception as exc:
            store.fail_run(run_db_id, str(exc))
            runs.finish(run_db_id, "failed")

    # --- REST: misc --------------------------------------------------------------
    @app.get("/api/version")
    def version() -> dict:
        import signaldesk

        return {"version": signaldesk.__version__}

    @app.get("/api/update-check")
    def update_check() -> dict:
        from .updates import check_for_update

        info = check_for_update()
        return info.__dict__

    @app.get("/api/onboarding")
    def onboarding_status() -> dict:
        return {"done": bool(settings.get("onboarding_done")),
                "consent_prompts": bool(settings.get("consent_prompts"))}

    @app.post("/api/onboarding/finish")
    def onboarding_finish() -> dict:
        settings.set(onboarding_done=True)
        return {"ok": True}

    # --- REST: runs ------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "time": datetime.now(UTC).isoformat(timespec="seconds")}

    @app.post("/api/chat")
    def chat(body: ChatIn) -> dict:
        prompt = body.prompt.strip()
        if not prompt:
            return {"error": "empty prompt"}
        session = store.create_session(title=prompt[:60]) if not body.session_id else None
        session_id = body.session_id or (session.id if session else "")
        prompt_row = store.add_prompt(session_id, prompt)
        if session:
            store.touch_session(session_id)

        plan = classify(prompt, openai_key=config.openai_api_key,
                        anthropic_key=config.anthropic_api_key,
                        openrouter_key=config.openrouter_api_key,
                        model=settings.get("llm_model") or config.llm_model)
        market = plan.market
        universe: list[str] = []
        watchlist = plan.watchlist
        if plan.workflow == Workflow.WATCHLIST_SCAN:
            from . import watchlists as wl_mod

            try:
                wl = wl_mod.load(config.data_dir, plan.watchlist or "default")
            except FileNotFoundError:
                return {"error": f"watchlist '{plan.watchlist}' not found", "session_id": session_id}
            universe, market = wl.symbols, wl.market
        if plan.workflow == Workflow.MARKET_SCAN and market not in ("crypto", "forex", "metals"):
            return {"error": f"scan for market '{market}' not wired yet",
                    "plan": plan.model_dump(mode="json"), "session_id": session_id}

        row = store.create_run(session_id=session_id, prompt_id=prompt_row.id,
                               workflow=plan.workflow.value, market=market, universe=universe)
        runs.start(row.id)
        if plan.workflow == Workflow.DEEP_DIVE:
            threading.Thread(target=_launch_deepdive, daemon=True,
                             args=(row.id, plan.symbol or "", market, body.demo)).start()
        else:
            threading.Thread(target=_launch_scan, daemon=True,
                             args=(row.id, market, body.demo, 12, watchlist, None)).start()
        return {"run_id": row.id, "session_id": session_id,
                "plan": plan.model_dump(mode="json")}

    @app.post("/api/scan")
    def scan_api(body: ScanRequestIn) -> dict:
        row = store.create_run(session_id="", prompt_id="", workflow="market_scan",
                               market=body.market, universe=[])
        runs.start(row.id)
        threading.Thread(target=_launch_scan, daemon=True,
                         args=(row.id, body.market, body.demo, body.universe_size, None,
                               body.entry_timeframes)).start()
        return {"run_id": row.id}

    @app.post("/api/deepdive")
    def deepdive_api(body: DeepDiveIn) -> dict:
        row = store.create_run(session_id="", prompt_id="", workflow="deep_dive",
                               market=body.market, universe=[body.symbol])
        runs.start(row.id)
        threading.Thread(target=_launch_deepdive, daemon=True,
                         args=(row.id, body.symbol, body.market, body.demo,
                               body.entry_timeframes)).start()
        return {"run_id": row.id}

    @app.get("/api/runs")
    def runs_list(limit: int = 50) -> list[dict]:
        return [
            {"id": r.id, "market": r.market, "workflow": r.workflow, "status": r.status,
             "started_at": r.started_at.isoformat(),
             "finished_at": r.finished_at.isoformat() if r.finished_at else None,
             "preset": r.scoring_preset, "has_report": bool(r.report_md), "error": r.error}
            for r in store.list_runs(limit=limit)
        ]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        row = store.get_run(run_id)
        if not row:
            return {"error": "not found"}
        events, live_status = runs.snapshot(run_id)
        if not events and row.trace_jsonl:
            # process restarted since the run: replay the persisted trace
            try:
                events = [Event(**json.loads(line))
                          for line in row.trace_jsonl.splitlines() if line.strip()]
            except Exception:
                events = []
        if live_status == "unknown":
            live_status = row.status
        return {
            "id": row.id, "market": row.market, "workflow": row.workflow, "status": row.status,
            "report_md": row.report_md, "report_json": json.loads(row.report_json or "{}"),
            "events": [e.model_dump() for e in events],
            "live_status": live_status, "error": row.error, "preset": row.scoring_preset,
            "started_at": row.started_at.isoformat(), "run_dir": row.run_dir,
        }

    @app.get("/api/runs/{run_id}/charts/{name}")
    def run_chart(run_id: str, name: str):
        """Serve a chart PNG from the run dir (path must stay inside it)."""
        from fastapi.responses import FileResponse

        row = store.get_run(run_id)
        if not row or not row.run_dir:
            return {"error": "run not found"}
        path = (Path(row.run_dir) / "sandbox" / "output" / "charts" / f"{Path(name).stem}.png")
        if not path.is_file():
            return {"error": "no chart"}
        return FileResponse(path, media_type="image/png")

    @app.get("/api/history")
    def history(limit: int = 50) -> dict:
        return {"sessions": [s.model_dump(mode="json") for s in store.list_sessions(limit)],
                "signals": [s.model_dump(mode="json") for s in store.list_signals(100)]}

    # --- REST: outcomes dashboard (WF-5) ----------------------------------------
    def _outcomes_payload(rows: list[dict], *, stale: bool) -> dict:
        import dataclasses

        from . import ledger as ledger_mod
        from . import metrics as metrics_mod
        from . import outcomes as outcomes_mod
        from . import paper as paper_mod
        from . import resolver as resolver_mod
        from . import scheduler as scheduler_mod
        from . import mtm as mtm_mod
        from . import universe as universe_mod

        records_by_id = {r.signal_id: r
                         for r in ledger_mod.read_records(ledger_mod.ledger_path(config.data_dir))}
        latest = outcomes_mod.load_latest(outcomes_mod.outcomes_path(config.data_dir))
        signals = resolver_mod.signal_rows(config.data_dir, limit=200)
        paper_by_id = paper_mod.first_events(config.data_dir)
        for row in signals:
            ev = paper_by_id.get(row["signal_id"])
            row["paper"] = ev.model_dump() if ev else None
        summary = metrics_mod.summarize(rows)
        bias_note = universe_mod.bias_note(config.data_dir)
        if bias_note:
            summary.notes.append(bias_note)
        return {
            "metrics": dataclasses.asdict(summary),
            "resolutions": rows,
            "signals": signals,
            "paper": paper_mod.paper_stats(config.data_dir, records_by_id, latest),
            "scheduler": scheduler_mod.load_state(config.data_dir),
            "mtm": mtm_mod.load_mtm(config.data_dir),
            "stale": stale,
        }

    @app.get("/api/outcomes")
    def outcomes_view(market: str = "", include_demo: bool = False) -> dict:
        """Cached outcome statistics (never fetches bars — the scheduler and
        POST /api/outcomes/resolve populate outcomes.jsonl)."""
        from . import ledger as ledger_mod
        from . import outcomes as outcomes_mod
        from . import resolver as resolver_mod

        out_file = outcomes_mod.outcomes_path(config.data_dir)
        ledger_file = ledger_mod.ledger_path(config.data_dir)
        # stale = ledger signals exist but are newer than the last resolution pass
        stale = ledger_file.is_file() and (
            (not out_file.is_file()) or ledger_file.stat().st_mtime > out_file.stat().st_mtime)
        rows: list[dict] = []
        if out_file.is_file():
            records = resolver_mod.select_records(config.data_dir, market=market or None,
                                                  include_demo=include_demo, backfill=False)
            rows = resolver_mod.join_rows(records, outcomes_mod.load_latest(out_file))
        return _outcomes_payload(rows, stale=stale)

    @app.post("/api/outcomes/resolve")
    def outcomes_resolve(body: ResolveIn) -> dict:
        """Resolve open ledger signals against daily bars now (blocking)."""
        from . import resolver as resolver_mod

        rows = resolver_mod.run_resolution(
            config.data_dir, market=body.market or None, demo=body.demo,
            include_demo=body.include_demo, horizon_bars=max(1, min(body.horizon, 250)))
        return _outcomes_payload(rows, stale=False)

    @app.post("/api/mtm/refresh")
    def mtm_refresh() -> dict:
        """Refresh the live mark-to-market snapshot (throttled to avoid
        hammering the quote source; serves the cache when called too soon)."""
        from . import mtm as mtm_mod

        return mtm_mod.refresh_if_stale(config.data_dir)

    @app.post("/api/paper/{signal_id}/fill")
    def paper_fill(signal_id: str, body: PaperIn | None = None) -> dict:
        from . import paper as paper_mod

        body = body or PaperIn()
        try:
            ev = paper_mod.log_event(config.data_dir, signal_id, "fill",
                                     price=body.price, note=body.note)
        except (KeyError, ValueError) as exc:
            return {"error": str(exc.args[0] if exc.args else exc)}
        return ev.model_dump()

    @app.post("/api/paper/{signal_id}/miss")
    def paper_miss(signal_id: str, body: PaperIn | None = None) -> dict:
        from . import paper as paper_mod

        body = body or PaperIn()
        try:
            ev = paper_mod.log_event(config.data_dir, signal_id, "miss", note=body.note)
        except (KeyError, ValueError) as exc:
            return {"error": str(exc.args[0] if exc.args else exc)}
        return ev.model_dump()

    # --- REST: watchlists ----------------------------------------------------
    @app.get("/api/watchlists")
    def wl_list() -> list[dict]:
        from . import watchlists as wl_mod

        return [w.__dict__ for w in wl_mod.list_all(config.data_dir)]

    @app.post("/api/watchlists")
    def wl_add(body: WatchlistIn) -> dict:
        from . import watchlists as wl_mod

        wl = wl_mod.add_symbols(config.data_dir, body.name, body.symbols, market=body.market)
        return wl.__dict__

    @app.post("/api/watchlists/{name}/symbols")
    def wl_patch(name: str, body: SymbolsIn) -> dict:
        from . import watchlists as wl_mod

        if body.add:
            wl_mod.add_symbols(config.data_dir, name, body.add)
        wl = wl_mod.remove_symbols(config.data_dir, name, body.remove) if body.remove \
            else wl_mod.load(config.data_dir, name)
        return wl.__dict__

    @app.post("/api/watchlists/{name}/scan")
    def wl_scan(name: str, demo: bool = False) -> dict:
        from . import watchlists as wl_mod

        wl = wl_mod.load(config.data_dir, name)
        row = store.create_run(session_id="", prompt_id="", workflow="watchlist_scan",
                               market=wl.market, universe=wl.symbols)
        runs.start(row.id)
        threading.Thread(target=_launch_scan, daemon=True,
                         args=(row.id, wl.market, demo, len(wl.symbols), name)).start()
        return {"run_id": row.id}

    # --- REST: settings -------------------------------------------------------
    @app.get("/api/settings")
    def get_settings() -> dict:
        return {**settings.as_dict(),
                "secrets": {k: userconfig.masked_secret(k) for k in userconfig.SECRET_KEYS}}

    @app.post("/api/settings")
    def set_settings(body: SettingsIn) -> dict:
        updates = {}
        if body.consent_prompts is not None:
            updates["consent_prompts"] = body.consent_prompts
        if body.llm_provider is not None:
            updates["llm_provider"] = body.llm_provider
        if body.llm_model is not None:
            updates["llm_model"] = body.llm_model
        if body.theme is not None:
            updates["theme"] = body.theme
        if body.entry_refinement is not None:
            updates["entry_refinement"] = body.entry_refinement
        if updates:
            settings.set(**updates)
        for key, value in body.secrets.items():
            if value:
                userconfig.set_secret(key, value)
            else:
                userconfig.delete_secret(key)
        return get_settings()

    # --- REST: auth & sync -----------------------------------------------------
    def _sb_client():
        from .sync import SupabaseClient, SupabaseConfig

        return SupabaseClient(SupabaseConfig(
            url=os.environ.get("SUPABASE_URL", ""),
            anon_key=os.environ.get("SUPABASE_ANON_KEY", ""),
        ))

    def _sb_client_active():
        """Client with access token refreshed from keychain, or None."""
        from .sync import SyncError

        if not os.environ.get("SUPABASE_URL"):
            return None
        try:
            token = userconfig.get_secret("SUPABASE_REFRESH_TOKEN")
        except Exception:
            token = None
        if not token:
            return None
        client = _sb_client()
        try:
            client.refresh(token)
        except SyncError:
            return None
        return client

    @app.post("/api/auth/signup")
    def signup(body: AuthIn) -> dict:
        from .sync import SyncError

        if not os.environ.get("SUPABASE_URL"):
            return {"error": "Supabase not configured (set SUPABASE_URL/ANON_KEY)"}
        try:
            auth = _sb_client().sign_up(body.email, body.password)
        except SyncError as exc:
            return {"error": str(exc)}
        if auth.refresh_token:
            userconfig.set_secret("SUPABASE_REFRESH_TOKEN", auth.refresh_token)
        settings.set(supabase_configured=True)
        return {"user_id": auth.user_id, "email": auth.email}

    @app.post("/api/auth/login")
    def login(body: AuthIn) -> dict:
        from .sync import SyncError

        if not os.environ.get("SUPABASE_URL"):
            return {"error": "Supabase not configured (set SUPABASE_URL/ANON_KEY)"}
        try:
            auth = _sb_client().sign_in(body.email, body.password)
        except SyncError as exc:
            return {"error": str(exc)}
        userconfig.set_secret("SUPABASE_REFRESH_TOKEN", auth.refresh_token)
        settings.set(supabase_configured=True)
        return {"user_id": auth.user_id, "email": auth.email}

    @app.post("/api/auth/logout")
    def logout() -> dict:
        try:
            userconfig.delete_secret("SUPABASE_REFRESH_TOKEN")
        except Exception:
            pass
        return {"ok": True}

    @app.get("/api/auth/status")
    def auth_status() -> dict:
        try:
            token = userconfig.get_secret("SUPABASE_REFRESH_TOKEN")
        except Exception:
            token = None
        return {"logged_in": bool(token), "consent": bool(settings.get("consent_prompts"))}

    @app.post("/api/sync")
    def sync_now() -> dict:
        from .sync import SyncEngine

        if not settings.get("consent_prompts"):
            return {"error": "consent off — sync disabled (FR-1.4)"}
        client = _sb_client_active()
        if client is None:
            return {"error": "not logged in / Supabase not configured"}
        engine = SyncEngine(client, store)
        try:
            pushed = engine.push_all(consent=True)
            pulled = engine.pull_watchlists(config.data_dir)
        except Exception as exc:
            return {"error": str(exc)}
        return {"pushed": pushed, "pulled": pulled}

    @app.post("/api/auth/delete-my-data")
    def delete_my_data() -> dict:
        client = _sb_client_active()
        if client is None:
            return {"error": "not logged in"}
        try:
            client.delete_my_data()
        except Exception as exc:
            return {"error": str(exc)}
        return {"ok": True, "note": "cloud rows deleted; local history untouched"}

    # --- WebSocket run stream --------------------------------------------------
    @app.websocket("/ws/runs/{run_id}")
    async def ws_run(ws: WebSocket, run_id: str) -> None:
        await ws.accept()
        events, status = runs.snapshot(run_id)
        for e in events:
            await ws.send_text(e.model_dump_json())
        if status != "running":
            await ws.send_text(json.dumps({"kind": "done", "status": status, "run_id": run_id}))
            await ws.close()
            return
        q = runs.subscribe(run_id)
        try:
            while True:
                try:
                    item = await asyncio.to_thread(q.get, True, 30)
                except queue.Empty:
                    await ws.send_text(json.dumps({"kind": "ping"}))
                    continue
                if item is _DONE_SENTINEL:
                    _, status = runs.snapshot(run_id)
                    await ws.send_text(json.dumps({"kind": "done", "status": status, "run_id": run_id}))
                    break
                await ws.send_text(item.model_dump_json())
        except WebSocketDisconnect:
            pass
        finally:
            runs.unsubscribe(run_id, q)

    # --- serve the built UI when present ----------------------------------------
    ui_dist = Path(__file__).parent / "ui" / "dist"
    if ui_dist.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(ui_dist), html=True), name="ui")

    return app


def main(port: int = 8787, host: str = "127.0.0.1") -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
