# SignalDesk

An open-source, cross-platform **AI market research & signal agent**. Ask
"scan the crypto market and find the best pair to trade now" and get a ranked,
fully-cited report — entry, stop, targets, R:R — in minutes. Read-only by
design: no broker integration, no auto-trading, ever.

## Quickstart

=== "Desktop app"
    Download the installer for your OS from the
    [releases page](https://github.com/signaldesk/signaldesk/releases):
    Windows `.exe` (NSIS), macOS `.dmg`, Linux AppImage/`.deb`.
    The first-run wizard walks you through: pick an LLM (BYOK or local Ollama)
    → optional Supabase account → consent screen → demo scan.

=== "From source"
    ```bash
    pip install -e .          # python >= 3.11
    signaldesk serve          # browser UI at http://127.0.0.1:8787
    ```

=== "CLI only"
    ```bash
    signaldesk scan crypto               # live market scan
    signaldesk scan forex              # FX majors + macro
    signaldesk scan metals             # gold & silver
    signaldesk deepdive AAPL           # one-page cited dossier
    signaldesk scan crypto --demo      # offline, deterministic, no keys
    ```

## What makes it different

- **Every figure is cited.** Prices, RSI, MACD, ATR, win rates: each is a
  direct citation (tool → CSV row → column) or a derived citation (formula +
  inputs). The critic rejects number-free or number-invented text.
- **Local-first.** Runs, traces, and reports live in `~/.signaldesk/`
  (SQLite + CSV artifacts). Cloud sync to Supabase is opt-in, consent-gated,
  and revocable ("delete my cloud data" in Settings).
- **Read-only.** The tool registry rejects any write-side adapter. No API key
  you provide is ever sent anywhere except the provider it belongs to.

> **Not financial advice.** Signals are heuristic scores from versioned,
  inspectable presets — never predictions or guarantees. See the
  [privacy page](privacy.md).
