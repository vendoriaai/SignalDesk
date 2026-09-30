"""Phase 7.6 vision chart read: parsing, message construction, degradation."""
import base64
import json
from types import SimpleNamespace

from signaldesk.agent import vision


# 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _png(tmp_path, name="chart.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


def _read_json(payload):
    return json.dumps(payload)


def test_parse_read_valid():
    read = vision.parse_read(_read_json({
        "reads": {"1d": {"trend": "up", "note": "above SMA20"},
                  "1m": {"trend": "range", "note": ""}},
        "entry": 100.5, "stop": 98.0, "rationale": "pullback to EMA21",
        "confidence": 0.8,
    }))
    assert read is not None
    assert read["entry"] == 100.5
    assert read["stop"] == 98.0
    assert read["reads"]["1d"]["trend"] == "up"
    assert read["confidence"] == 0.8


def test_parse_read_junk_and_bad_entries():
    assert vision.parse_read("not json at all") is None
    assert vision.parse_read('{"entry": "abc"}') is None
    assert vision.parse_read('{"entry": -1}') is None
    assert vision.parse_read('{"entry": null}') is None
    # bad stop degrades to None stop, not a failed read
    read = vision.parse_read('{"entry": 5, "stop": "x", "reads": [], "confidence": 2}')
    assert read is not None
    assert read["stop"] is None
    assert read["reads"] == {}
    assert read["confidence"] == 1.0


def test_model_id_defaults_and_override():
    assert vision.model_id("openai", None) == "gpt-4o-mini"
    assert vision.model_id("openrouter", "openrouter/auto") == "openrouter/auto"


def test_model_id_normalizes_bare_openrouter_ids():
    # a Settings value like "deepseek/deepseek-v4.1-flash" must route through
    # OpenRouter, not the native DeepSeek provider (which would reject the key)
    assert vision.model_id("openrouter", "deepseek/deepseek-v4.1-flash") == \
        "openrouter/deepseek/deepseek-v4.1-flash"
    assert vision.model_id("openrouter", None) == "openrouter/openai/gpt-4o-mini"
    # non-openrouter providers pass through untouched
    assert vision.model_id("openai", "gpt-4o-mini") == "gpt-4o-mini"
    assert vision.model_id("anthropic", None) == "claude-haiku-4-5"


def test_chart_entry_read_builds_multimodal_message(tmp_path, monkeypatch):
    charts = [("1d", _png(tmp_path, "a.png")), ("1m", _png(tmp_path, "b.png"))]
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=_read_json({"entry": 10.0, "stop": None,
                                "rationale": "r", "confidence": 0.5})))])

    import litellm
    monkeypatch.setattr(litellm, "completion", fake_completion)
    read = vision.chart_entry_read(
        "BTCUSD", "LONG", chart_pngs=charts, brief="draft plan here",
        provider="openai", key="sk-test")
    assert read is not None and read["entry"] == 10.0
    assert captured["api_key"] == "sk-test"
    assert captured["model"] == "gpt-4o-mini"
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert user[0]["type"] == "text" and "draft plan here" in user[0]["text"]
    images = [part for part in user if part["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_chart_entry_read_api_failure_returns_none(tmp_path, monkeypatch):
    import litellm

    def boom(**kwargs):
        raise RuntimeError("model is not vision-capable")

    monkeypatch.setattr(litellm, "completion", boom)
    read = vision.chart_entry_read(
        "BTCUSD", "LONG", chart_pngs=[("1d", _png(tmp_path))], brief="b",
        provider="openai", key="sk-test")
    assert read is None


def test_chart_entry_read_unreadable_png_returns_none(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png but readable")   # read succeeds; b64 fine either way
    missing = tmp_path / "missing.png"
    read = vision.chart_entry_read(
        "BTCUSD", "LONG", chart_pngs=[("1d", missing)], brief="b",
        provider="openai", key="k")
    assert read is None


def test_resolve_creds_env_then_none(monkeypatch, tmp_path):
    fake = SimpleNamespace(openai_api_key="sk-env", anthropic_api_key=None,
                           openrouter_api_key=None, llm_model=None,
                           data_dir=tmp_path)
    monkeypatch.setattr(vision, "_resolve_cfg", lambda: fake)
    creds = vision.resolve_creds()
    assert creds == ("openai", "sk-env", None)


def test_resolve_creds_keychain_fallback(monkeypatch, tmp_path):
    fake = SimpleNamespace(openai_api_key=None, anthropic_api_key=None,
                           openrouter_api_key="sk-or", llm_model="openrouter/auto",
                           data_dir=tmp_path)
    monkeypatch.setattr(vision, "_resolve_cfg", lambda: fake)
    creds = vision.resolve_creds()
    assert creds == ("openrouter", "sk-or", "openrouter/auto")


def test_resolve_creds_none(monkeypatch, tmp_path):
    fake = SimpleNamespace(openai_api_key=None, anthropic_api_key=None,
                           openrouter_api_key=None, llm_model=None,
                           data_dir=tmp_path)
    monkeypatch.setattr(vision, "_resolve_cfg", lambda: fake)
    assert vision.resolve_creds() is None
