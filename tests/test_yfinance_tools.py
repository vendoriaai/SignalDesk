"""yfinance normalization: empty/odd frames must degrade, not raise KeyError."""
import pandas as pd

from signaldesk.tools.yfinance_tools import _normalize_yf


def test_empty_response_returns_an_empty_frame():
    """Yahoo throttling used to surface as KeyError('date') and drop the symbol."""
    for raw in (None, pd.DataFrame()):
        frame = _normalize_yf(raw)
        assert frame.empty
        assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume"]


def test_named_date_index_is_normalized():
    raw = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [10.0]},
        index=pd.DatetimeIndex(["2026-09-01"], name="Date"),
    )
    frame = _normalize_yf(raw)
    assert len(frame) == 1
    assert str(frame["date"].iloc[0].date()) == "2026-09-01"
    assert frame["close"].iloc[0] == 1.5


def test_unnamed_index_becomes_date():
    raw = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [None]},
        index=pd.DatetimeIndex(["2026-09-01 12:00"]),
    )
    frame = _normalize_yf(raw)
    assert not frame.empty and frame["volume"].fillna(-1).iloc[0] == 0.0


def test_missing_price_columns_return_empty():
    raw = pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex(["2026-09-01"]))
    assert _normalize_yf(raw).empty


def test_multiindex_columns_are_flattened():
    cols = pd.MultiIndex.from_product([["Close", "High", "Low", "Open", "Volume"], ["BTC-USD"]])
    raw = pd.DataFrame([[1.5, 2.0, 0.5, 1.0, 10.0]], columns=cols,
                       index=pd.DatetimeIndex(["2026-09-01"], name="Date"))
    frame = _normalize_yf(raw)
    assert len(frame) == 1 and frame["close"].iloc[0] == 1.5


def test_ohlcv_tool_raises_a_clear_error_when_yahoo_is_empty(tmp_path):
    import signaldesk.tools.yfinance_tools as yft
    from signaldesk.tools.yfinance_tools import YFinanceOHLCVTool

    tool = YFinanceOHLCVTool(tmp_path, market="crypto")
    calls: list[int] = []

    def fake_download(*args, **kwargs):
        calls.append(1)
        return pd.DataFrame()

    class _FakeYf:
        download = staticmethod(fake_download)

    import sys
    import time as _time

    real_time_sleep = _time.sleep
    sys.modules["yfinance"] = _FakeYf  # type: ignore[assignment]
    _time.sleep = lambda _s: None
    try:
        try:
            tool.run(symbol="BTCUSD")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "no OHLCV data for BTCUSD" in str(exc)
        assert len(calls) == 3  # two back-off retries for throttled/DNS-blip responses
    finally:
        del sys.modules["yfinance"]
        _time.sleep = real_time_sleep
    assert yft.__name__ == "signaldesk.tools.yfinance_tools"
