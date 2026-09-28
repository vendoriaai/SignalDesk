-- SignalDesk cloud data model (Supabase Postgres) — TAD 3.5 / PRD FR-4
-- Apply with: psql "$SUPABASE_DB_URL" -f data-model.sql
-- or paste into the Supabase SQL editor. Every table is RLS-scoped to
-- auth.uid(); there is no service-role cross-user access in v1.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- profile --
create table if not exists public.profiles (
    user_id             uuid primary key references auth.users (id) on delete cascade,
    email               text not null,
    consent_prompts     boolean not null default false,   -- opt-in prompt/result collection
    consent_updated_at  timestamptz not null default now(),
    created_at          timestamptz not null default now()
);

-- ---------------------------------------------------------------- sessions --
create table if not exists public.sessions (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users (id) on delete cascade,
    title       text not null default 'New session',
    created_at  timestamptz not null default now(),
    last_used_at timestamptz not null default now()
);

-- ----------------------------------------------------------------- prompts --
create table if not exists public.prompts (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users (id) on delete cascade,
    session_id  uuid not null references public.sessions (id) on delete cascade,
    text        text not null,
    created_at  timestamptz not null default now()
);

-- --------------------------------------------------------------------- runs --
create table if not exists public.runs (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references auth.users (id) on delete cascade,
    session_id      uuid not null references public.sessions (id) on delete cascade,
    prompt_id       uuid references public.prompts (id) on delete set null,
    workflow        text not null,              -- e.g. 'WF-1 market_scan'
    status          text not null default 'completed',
    market          text,
    universe        text[],
    scoring_preset  text,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    trace           jsonb                       -- full event trace (replayable)
);

-- ----------------------------------------------------------------- reports --
create table if not exists public.reports (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users (id) on delete cascade,
    run_id      uuid not null references public.runs (id) on delete cascade,
    format      text not null default 'markdown+json',
    markdown    text not null,
    report_json jsonb not null,
    citations   jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);

-- --------------------------------------------------------------- artifacts --
create table if not exists public.artifacts (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users (id) on delete cascade,
    run_id      uuid not null references public.runs (id) on delete cascade,
    kind        text not null,                  -- quotes | ohlcv | movers | sentiment | ...
    filename    text not null,
    row_count   integer,
    storage_path text,                          -- only set when uploaded with consent
    created_at  timestamptz not null default now()
);

-- -------------------------------------------------------------- watchlists --
create table if not exists public.watchlists (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users (id) on delete cascade,
    name        text not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (user_id, name)
);

create table if not exists public.watchlist_items (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users (id) on delete cascade,
    watchlist_id  uuid not null references public.watchlists (id) on delete cascade,
    symbol        text not null,
    added_at      timestamptz not null default now(),
    unique (watchlist_id, symbol)
);

-- ----------------------------------------------------------------- signals --
create table if not exists public.signals (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users (id) on delete cascade,
    run_id      uuid not null references public.runs (id) on delete cascade,
    symbol      text not null,
    market      text not null,
    direction   text not null default 'LONG',
    score       numeric not null,
    entry       numeric not null,
    stop        numeric not null,
    tp1         numeric not null,
    tp2         numeric not null,
    emitted_at  timestamptz not null default now(),
    -- populated by WF-5 outcome tracker (v1.1)
    outcome     text check (outcome in ('TP1','TP2','SL','OPEN')) default 'OPEN',
    resolved_at timestamptz
);

-- ------------------------------------------------------------------ indexes
create index if not exists idx_sessions_user      on public.sessions (user_id);
create index if not exists idx_prompts_session    on public.prompts (session_id);
create index if not exists idx_runs_session       on public.runs (session_id);
create index if not exists idx_signals_user_open  on public.signals (user_id) where outcome = 'OPEN';
create index if not exists idx_watchlists_user    on public.watchlists (user_id);

-- ---------------------------------------------------------------------- RLS
alter table public.profiles        enable row level security;
alter table public.sessions        enable row level security;
alter table public.prompts         enable row level security;
alter table public.runs            enable row level security;
alter table public.reports         enable row level security;
alter table public.artifacts       enable row level security;
alter table public.watchlists      enable row level security;
alter table public.watchlist_items enable row level security;
alter table public.signals         enable row level security;

-- one policy per verb per table: rows are visible/mutable only to their owner
do $$
declare t text;
begin
  foreach t in array array[
    'profiles','sessions','prompts','runs','reports','artifacts',
    'watchlists','watchlist_items','signals'
  ] loop
    execute format('drop policy if exists select_own on public.%I', t);
    execute format('drop policy if exists insert_own on public.%I', t);
    execute format('drop policy if exists update_own on public.%I', t);
    execute format('drop policy if exists delete_own on public.%I', t);
    execute format('create policy select_own on public.%I for select using (auth.uid() = user_id)', t);
    execute format('create policy insert_own on public.%I for insert with check (auth.uid() = user_id)', t);
    execute format('create policy update_own on public.%I for update using (auth.uid() = user_id)', t);
    execute format('create policy delete_own on public.%I for delete using (auth.uid() = user_id)', t);
  end loop;
end $$;

-- GDPR-style erasure: the client calls this from "Delete my cloud data".
create or replace function public.delete_my_data()
returns void language sql security definer set search_path = public as $$
    delete from public.profiles where user_id = auth.uid();
$$;
revoke all on function public.delete_my_data() from public;
grant execute on function public.delete_my_data() to authenticated;
