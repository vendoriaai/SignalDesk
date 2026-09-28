"""Runtime configuration: env vars + data directory.

OS-keychain secret storage and Supabase settings arrive with M3 (sync layer);
the headless core only needs optional search/LLM keys and a writable run dir.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    override = os.environ.get("SIGNALDESK_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".signaldesk"


def _load_dotenv() -> None:
    dotenv = Path(".env")
    if not dotenv.is_file():
        return
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass
class Config:
    data_dir: Path = field(default_factory=_default_data_dir)
    tavily_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    fred_api_key: str | None = None
    alphavantage_api_key: str | None = None
    llm_model: str | None = None  # e.g. "openrouter/auto", overrides per-provider default

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        cfg = cls()
        cfg.tavily_api_key = os.environ.get("TAVILY_API_KEY") or None
        cfg.openai_api_key = os.environ.get("OPENAI_API_KEY") or None
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY") or None
        cfg.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY") or None
        cfg.fred_api_key = os.environ.get("FRED_API_KEY") or None
        cfg.alphavantage_api_key = os.environ.get("ALPHAVANTAGE_API_KEY") or None
        cfg.llm_model = os.environ.get("SIGNALDESK_MODEL") or None
        return cfg

    def run_dir(self, run_id: str) -> Path:
        path = self.data_dir / "runs" / run_id
        (path / "artifacts").mkdir(parents=True, exist_ok=True)
        (path / "output").mkdir(parents=True, exist_ok=True)
        return path
