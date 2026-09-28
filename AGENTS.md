# DOX framework

- DOX is highly performant AGENTS.md hierarchy installed here
- Agent must follow DOX instructions across any edits

## Core Contract

- AGENTS.md files are binding work contracts for their subtrees
- Work products, source materials, instructions, records, assets, and durable docs must stay understandable from the nearest applicable AGENTS.md plus every parent AGENTS.md above it

## Read Before Editing

1. Read the root AGENTS.md
2. Identify every file or folder you expect to touch
3. Walk from the repository root to each target path
4. Read every AGENTS.md found along each route
5. If a parent AGENTS.md lists a child AGENTS.md whose scope contains the path, read that child and continue from there
6. Use the nearest AGENTS.md as the local contract and parent docs for repo-wide rules
7. If docs conflict, the closer doc controls local work details, but no child doc may weaken DOX

Do not rely on memory. Re-read the applicable DOX chain in the current session before editing.

## Update After Editing

Every meaningful change requires a DOX pass before the task is done.

Update the closest owning AGENTS.md when a change affects:

- purpose, scope, ownership, or responsibilities
- durable structure, contracts, workflows, or operating rules
- required inputs, outputs, permissions, constraints, side effects, or artifacts
- user preferences about behavior, communication, process, organization, or quality
- AGENTS.md creation, deletion, move, rename, or index contents

Update parent docs when parent-level structure, ownership, workflow, or child index changes. Update child docs when parent changes alter local rules. Remove stale or contradictory text immediately. Small edits that do not change behavior or contracts may leave docs unchanged, but the DOX pass still must happen.

## Hierarchy

- Root AGENTS.md is the DOX rail: project-wide instructions, global preferences, durable workflow rules, and the top-level Child DOX Index
- Child AGENTS.md files own domain-specific instructions and their own Child DOX Index
- Each parent explains what its direct children cover and what stays owned by the parent
- The closer a doc is to the work, the more specific and practical it must be

## Child Doc Shape

- Create a child AGENTS.md when a folder becomes a durable boundary with its own purpose, rules, responsibilities, workflow, materials, or quality standards
- Work Guidance must reflect the current standards of the project or user instructions; if there are no specific standards or instructions yet, leave it empty
- Verification must reflect an existing check; if no verification framework exists yet, leave it empty and update it when one exists

Default section order:
- Purpose
- Ownership
- Local Contracts
- Work Guidance
- Verification
- Child DOX Index

## Style

- Keep docs concise, current, and operational
- Document stable contracts, not diary entries
- Put broad rules in parent docs and concrete details in child docs
- Prefer direct bullets with explicit names
- Do not duplicate rules across many files unless each scope needs a local version
- Delete stale notes instead of explaining history
- Trim obvious statements, repeated rules, misplaced detail, and warnings for risks that no longer exist

## Closeout

1. Re-check changed paths against the DOX chain
2. Update nearest owning docs and any affected parents or children
3. Refresh every affected Child DOX Index
4. Remove stale or contradictory text
5. Run existing verification when relevant
6. Report any docs intentionally left unchanged and why

## User Preferences

When the user requests a durable behavior change, record it here or in the relevant child AGENTS.md

## Child DOX Index

Project tree after the P0 measurement work (2026-09-27, v1.0.0 + unreleased).
No child AGENTS.md files: the tree is a single small package and the existing docs
already own each boundary. Split out a child AGENTS.md when a subtree gains its own
durable rules beyond what the docs below state.

- `README.md` — quickstart, current feature status, dev commands (incl. `signaldesk outcomes`/`ledger`)
- `CHANGELOG.md` — release notes (v1.0.0; Unreleased carries the measurement layer)
- `prd.md` — product spec (assumptions, FR/NFR, milestones)
- `tad.md` — architecture of record; module contracts (incl. §3.8 measurement layer)
- `workflows.md` — agent workflow specs (WF-1..WF-4 implemented; WF-5 CLI part shipped); global rules R1–R7
- `roadmap.md` — phase plan; current position: Phase 4 done (packaging), P0 measurement foundation shipped, Phase 5 items 28–37 next
- `data-model.sql` — Supabase schema + RLS (must be applied to the hosted project before /api/sync works)
- `update-manifest.json` — current-release manifest (bump on every release; powers /api/update-check)
- `mkdocs.yml` + `docs/` — docs site (quickstart, data sources, strategies, privacy)
- `packaging/` — PyInstaller spec (`signaldesk.spec`, buildable: verified 104 MB Windows
  bundle) + NSIS script (`signaldesk.nsi`); installers are built per-OS by CI
- `.github/workflows/build-installers.yml` — tag-triggered win/mac/linux build + release
- `evals/cases.json` — 30-case planner/workflow corpus (runner: `signaldesk/evals/runner.py`)
- `signaldesk/` — Python package: `markets.py`, `agent/` (events, planner),
  `tools/` (read-only adapters; `yfinance_tools.py` normalizes empty/odd Yahoo
  frames and retries with backoff + a 20 s download timeout before dropping
  the symbol),
  `sandbox/` (matplotlib allowed in whitelist; frozen builds spawn
  `sandbox-exec` child mode; children import the first-party package via a
  pinned PYTHONPATH so a stale editable install can't break them; per-call
  timeouts, scan chart pass 180 s),
  `citations/`, `analysis/` (indicators + charts incl. entry-level charts),
  `strategy/` (presets, trade plans, `MIN_RISK_ATR_MULT`/`MIN_RISK_COST_MULT`
  risk floor, Phase 7.5 ATR-only entry refinement),
  `workflows/` (market_scan, deep_dive, entry_refine), `report/`,
  `costs.py` (round-trip cost assumptions + cost-in-R + break-even win rate),
  `ledger.py` (append-only `signals.jsonl`: actionable levels, snapshot hash,
  weights fingerprint, demo flag; `backfill()` from past reports),
  `outcomes.py` (triple-barrier resolver; declared conventions in the module
  docstring), `metrics.py` (expectancy/CI/hit-rate/profit-factor/breakdowns),
  `watchlists.py` (local JSON store), `store.py` (SQLite/SQLModel cache),
  `userconfig.py` (keychain secrets: LLM incl. OpenRouter, search, data, Supabase tokens),
  `sync.py` (Supabase REST, consent-gated), `api.py` (FastAPI + WS + chart
  endpoint /api/runs/{id}/charts/{name}.png), `desktop.py` (pywebview shell),
  `updates.py` (update manifest check; manual download in v1), `cli.py`
- `ui/` — React/Vite frontend (chat with live trace incl. chart events +
  "Charts analyzed" gallery; history view replays the persisted trace and the
  same gallery via `/api/runs/{id}`; build output in `signaldesk/ui/dist`,
  served by api.py). The outcome statistics are CLI-only for now (Phase 5 item 28).
- `tests/` — pytest suite (135 tests); new modules must add coverage here

Root-level rules: keep every report figure cited (R1), read-only tools only, degrade
with disclosure instead of failing (R4), no "signals = prediction" wording anywhere,
cost-aware risk units (R6 — never emit a plan whose cost swamps its risk unit), and
fixed outcome accounting (R7 — resolver conventions in `signaldesk/outcomes.py` are
never re-labelled after seeing results; statistics come with intervals and
sample-size caveats).
Secrets only via OS keychain (`signaldesk/userconfig.py`); cloud sync only with
consent on (`sync.push_all(consent=...)`) — never weaken these.
