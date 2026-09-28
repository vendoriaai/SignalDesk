"""User settings + OS-keychain secrets (TAD 5; roadmap 3.19).

Secrets (LLM/search/data keys, Supabase tokens) live only in the OS keychain
via `keyring`. Non-secret preferences (consent toggle, provider choice, theme)
live in a plain JSON settings file in the data dir.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# secret keys this app manages
SECRET_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "TAVILY_API_KEY",
    "FRED_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "SUPABASE_REFRESH_TOKEN",
)

_SERVICE = "signaldesk"


def set_secret(key: str, value: str) -> None:
    if key not in SECRET_KEYS:
        raise ValueError(f"unknown secret '{key}' (allowed: {', '.join(SECRET_KEYS)})")
    import keyring

    keyring.set_password(_SERVICE, key, value)


def get_secret(key: str) -> str | None:
    if key not in SECRET_KEYS:
        raise ValueError(f"unknown secret '{key}'")
    import keyring

    return keyring.get_password(_SERVICE, key)


def delete_secret(key: str) -> None:
    import keyring

    if get_secret(key):
        keyring.delete_password(_SERVICE, key)


def masked_secret(key: str) -> str | None:
    v = get_secret(key)
    if not v:
        return None
    return v[:3] + "…" + v[-2:] if len(v) > 6 else "***"


class Settings:
    """JSON-backed non-secret preferences."""

    DEFAULTS = {
        "consent_prompts": False,
        "llm_provider": "openai",
        "llm_model": "",          # optional override, e.g. "openrouter/anthropic/claude-sonnet-4.5"
        "theme": "dark",
        "entry_refinement": True,  # Phase 7.5: intraday (30m/15m/5m/1m) entry plans
        "supabase_configured": False,
        "onboarding_done": False,
        "update_check_done_today": "",
    }

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "settings.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = dict(self.DEFAULTS)
        if self.path.exists():
            try:
                self._data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except Exception:
                pass

    def get(self, key: str):
        return self._data.get(key, self.DEFAULTS.get(key))

    def set(self, **kwargs) -> None:
        self._data.update(kwargs)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def as_dict(self) -> dict:
        return dict(self._data)

    def apply_env(self) -> None:
        """Push keychain secrets into os.environ for the current process."""
        for key in SECRET_KEYS:
            if key.endswith("_REFRESH_TOKEN"):
                continue
            try:
                value = get_secret(key)
            except Exception:
                value = None
            if value:
                os.environ.setdefault(key, value)
