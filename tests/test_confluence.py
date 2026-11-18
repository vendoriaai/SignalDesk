from datetime import UTC, datetime

import pandas as pd

from signaldesk.strategy.confluence import _closed_bars


def test_closed_bars_drops_forming_intraday_candle(tmp_path):
    path = tmp_path / "bars.csv"
    pd.DataFrame({
        "date": ["2026-11-18T10:00:00Z", "2026-11-18T12:30:00Z"],
        "open": [100, 101], "high": [102, 103],
        "low": [99, 100], "close": [101, 102],
    }).to_csv(path, index=False)

    frame, dropped, timestamped = _closed_bars(
        path, "1h", datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
    )

    assert timestamped is True
    assert dropped == 1
    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 101


def test_closed_bars_keeps_data_when_timestamp_missing(tmp_path):
    path = tmp_path / "bars.csv"
    pd.DataFrame({
        "open": [100, 101], "high": [102, 103],
        "low": [99, 100], "close": [101, 102],
    }).to_csv(path, index=False)

    frame, dropped, timestamped = _closed_bars(
        path, "15m", datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
    )

    assert timestamped is False
    assert dropped == 0
    assert len(frame) == 2


def test_closed_bars_does_not_assume_daily_session_is_24_hours(tmp_path):
    path = tmp_path / "bars.csv"
    pd.DataFrame({
        "date": ["2026-11-18T00:00:00Z"],
        "open": [100], "high": [102], "low": [99], "close": [101],
    }).to_csv(path, index=False)

    frame, dropped, timestamped = _closed_bars(
        path, "1d", datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
    )

    assert timestamped is True
    assert dropped == 0
    assert len(frame) == 1
