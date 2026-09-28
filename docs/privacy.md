# Privacy

## Local-first, always

Every scan runs on your machine. A fresh install with no keys and no account
is fully functional (the bundled demo market even works offline).

## What stays local by default

- Prompts you type, plans, run traces, and final reports
- Watchlists, settings, run history (`~/.signaldesk/signaldesk.db`)
- All your API keys, stored in the **OS keychain** (Windows Credential Manager,
  macOS Keychain, Secret Service on Linux) — never in files, never uploaded

## What goes to the cloud — only with consent

When you **create a Supabase account** AND turn on the consent toggle
(Settings → Privacy), SignalDesk syncs: prompts, runs, reports, and signals.
This is used for analytics and cross-device history. The sync engine
(`SyncEngine.push_all`) refuses to upload anything when consent is off —
this is enforced in code and covered by tests.

Turning consent off later stops all further syncing. History then lives
locally only.

## Delete my cloud data

Settings → "Delete my cloud data" calls a server-side RPC that cascades every
stored row bound to your user id (`data-model.sql` → `delete_my_data()`),
regardless of consent state. Local copies are untouched.

## PII

Email is the only personal identifier we store (Supabase Auth). No names,
no payment data, no broker credentials (there are never credentials — SignalDesk
is read-only).
