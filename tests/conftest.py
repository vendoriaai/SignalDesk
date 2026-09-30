"""Shared fixtures: hermetic LLM configuration.

The Phase 7.6 AI chart read resolves credentials from env/.env/keychain.
Tests must never hit a real LLM, so every test runs with the LLM keys and
model overrides stripped — `signaldesk.agent.vision.resolve_creds` returns
None unless a test explicitly monkeypatches it (or chart_entry_read itself).
"""
import pytest


@pytest.fixture(autouse=True)
def _no_llm_credentials(monkeypatch):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
                "SIGNALDESK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    from signaldesk import config as config_mod
    from signaldesk import userconfig

    monkeypatch.setattr(config_mod, "_load_dotenv", lambda: None)
    real_get_secret = userconfig.get_secret
    monkeypatch.setattr(
        userconfig, "get_secret",
        lambda key: None if key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                                    "OPENROUTER_API_KEY") else real_get_secret(key))
