"""Behavioral checks for normalization, quality accounting, and collection."""

from threading import Barrier

import numpy as np
import pandas as pd
import pytest

from marketpipe.core import COLUMNS, VALUES, clean, collect, utc_day

START = pd.Timestamp("2024-01-01", tz="UTC")
END = pd.Timestamp("2024-01-04", tz="UTC")


def row(timestamp="2024-01-01", **values):
    return {
        "timestamp": timestamp,
        "open": 100,
        "high": 120,
        "low": 90,
        "close": 110,
        "volume": 7,
        **values,
    }


class FakeProvider:
    name = "fake"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        response = self.responses[symbol]
        if isinstance(response, Exception):
            raise response
        return response.copy()


@pytest.mark.parametrize(
    "changes",
    [
        {"open": 0},
        {"close": -1},
        {"high": 105},
        {"low": 105},
        {"volume": -1},
        {"open": "bad"},
        {"close": np.nan},
        {"volume": float("inf")},
        {"low": float("-inf")},
        {"close": True},
        {"volume": False},
    ],
)
def test_rejects_invalid_candle_values_without_filling(changes):
    aligned, quality = clean(pd.DataFrame([row(**changes)]), START, END)
    assert quality["invalid_values"] == quality["rejected_rows"] == 1
    assert quality["valid_rows"] == 0
    assert quality["missing_rows"] == 3
    assert aligned[VALUES].isna().all().all()
    assert aligned["is_missing"].all()


def test_accepts_numeric_strings_and_zero_volume():
    aligned, quality = clean(
        pd.DataFrame([row(open="100", close="110", volume="0")]),
        START,
        START + pd.Timedelta(days=1),
    )
    assert quality["rejected_rows"] == quality["missing_rows"] == 0
    assert aligned.iloc[0]["close"] == 110
    assert aligned.iloc[0]["volume"] == 0


def test_dates_are_coerced_to_utc_with_exclusive_end_and_separate_rejections():
    raw = pd.DataFrame(
        [
            row("2023-12-31T19:00:00-05:00"),  # Same instant as Jan 1 midnight UTC.
            row("2024-01-02T01:00:00Z"),
            row("not-a-date"),
            row(1704240000),  # CSV epoch units are ambiguous; never silently infer.
            row("2023-12-31"),
            row("2024-01-04"),
        ]
    )
    aligned, quality = clean(raw, START, END)
    assert aligned["timestamp"].tolist() == list(pd.date_range(START, periods=3))
    assert str(aligned["timestamp"].dt.tz) == "UTC"
    assert quality["valid_rows"] == 1
    assert quality["invalid_timestamps"] == 2
    assert quality["off_grid_rows"] == 1
    assert quality["out_of_range_rows"] == 2
    assert quality["rejected_rows"] == 3


def test_last_valid_duplicate_wins_and_invalid_later_copy_cannot_erase_it():
    raw = pd.DataFrame(
        [
            row(close=105),
            row(close=105),
            row(close=115),
            row(close=float("nan")),
            row("2024-01-02"),
            row("2024-01-02"),
        ]
    )
    aligned, quality = clean(raw, START, END)
    assert aligned.iloc[0]["close"] == 115
    assert quality["duplicate_rows"] == 3
    assert quality["conflicting_timestamps"] == 1
    assert quality["invalid_values"] == 1
    assert quality["valid_rows"] == 2


def test_alignment_exposes_internal_gap_without_forward_fill():
    raw = pd.DataFrame([row(), row("2024-01-03", close=118)])
    aligned, quality = clean(raw, START, END)
    assert aligned["is_missing"].tolist() == [False, True, False]
    assert aligned.loc[1, VALUES].isna().all()
    assert quality["expected_rows"] == 3
    assert quality["missing_rows"] == 1


def test_empty_source_still_emits_expected_calendar():
    aligned, quality = clean(pd.DataFrame(columns=COLUMNS), START, END)
    assert len(aligned) == 3
    assert quality["input_rows"] == quality["valid_rows"] == 0
    assert quality["missing_rows"] == 3


def test_missing_required_columns_fail_instead_of_guessing():
    with pytest.raises(ValueError, match="Missing columns"):
        clean(pd.DataFrame({"timestamp": [START], "close": [110]}), START, END)


@pytest.mark.parametrize("date", ["NaT", "", "not-a-date", "2024-01-01T01:00:00Z"])
def test_invalid_request_dates_fail(date):
    with pytest.raises(ValueError):
        utc_day(date)


def test_request_date_converts_timezone_before_midnight_validation():
    assert utc_day("2023-12-31T19:00:00-05:00") == START
    assert utc_day("2024-01-01") == START
    with pytest.raises(ValueError, match="midnight UTC"):
        utc_day("2024-01-01T00:00:00-05:00")


@pytest.mark.parametrize(
    "symbols",
    [
        [],
        ["../BTC-USD"],
        ["BTC/USD"],
        [" BTC-USD"],
        ["BTC-USD.csv"],
        ["BTC-USD/../../"],
        [""],
        [f"ASSET{i}-USD" for i in range(51)],
    ],
)
def test_unsafe_or_invalid_symbols_rejected_before_provider_is_called(symbols):
    provider = FakeProvider({})
    with pytest.raises(ValueError):
        collect(provider, symbols, START, END)
    assert provider.calls == []


@pytest.mark.parametrize("workers", [0, 9, -1])
def test_invalid_worker_count_rejected(workers):
    with pytest.raises(ValueError, match="workers"):
        collect(FakeProvider({}), ["BTC-USD"], START, END, workers)


@pytest.mark.parametrize(
    "start,end",
    [
        ("2024-01-01", "2024-01-01"),
        ("2024-01-02", "2024-01-01"),
        ("2000-01-01", "2024-01-01"),
    ],
)
def test_invalid_date_ranges_rejected(start, end):
    with pytest.raises(ValueError, match="Date range"):
        collect(FakeProvider({}), ["BTC-USD"], start, end)


def test_only_completed_daily_candles_are_requested():
    today = pd.Timestamp.now(tz="UTC").normalize()
    yesterday = today - pd.Timedelta(days=1)
    provider = FakeProvider({"BTC-USD": pd.DataFrame([row(yesterday)])})
    result = collect(provider, ["BTC-USD"], yesterday, today)
    assert result.report["status"] == "complete"
    assert result.aligned["timestamp"].tolist() == [yesterday]
    with pytest.raises(ValueError, match="completed UTC daily candles"):
        collect(provider, ["BTC-USD"], yesterday, today + pd.Timedelta(days=1))
    assert len(provider.calls) == 1


def test_symbols_are_uppercased_deduplicated_and_ordered():
    provider = FakeProvider({"BTC-USD": pd.DataFrame([row()]), "ETH-USD": pd.DataFrame([row()])})
    result = collect(provider, ["eth-usd", "BTC-USD", "ETH-USD"], START, END)
    assert result.report["symbols"] == ["ETH-USD", "BTC-USD"]
    assert list(result.report["quality"]) == ["ETH-USD", "BTC-USD"]
    assert result.aligned["symbol"].tolist() == ["ETH-USD"] * 3 + ["BTC-USD"] * 3
    assert len(provider.calls) == 2
    assert result.has_quality_issues


def test_workers_run_concurrently_and_one_failure_does_not_discard_success():
    rendezvous = Barrier(2, timeout=10)

    class ConcurrentProvider:
        name = "concurrent"

        def fetch(self, symbol, start, end):
            rendezvous.wait()
            if symbol == "ETH-USD":
                raise RuntimeError("source unavailable")
            return pd.DataFrame([row()])

    result = collect(ConcurrentProvider(), ["ETH-USD", "BTC-USD"], START, END, workers=2)
    assert result.report["status"] == "partial"
    assert result.report["failures"] == {"ETH-USD": "RuntimeError: source unavailable"}
    assert result.aligned["symbol"].unique().tolist() == ["BTC-USD"]
    assert result.report["quality"]["BTC-USD"]["valid_rows"] == 1


def test_all_failures_return_structured_empty_result():
    provider = FakeProvider(
        {"BTC-USD": RuntimeError("offline"), "ETH-USD": pd.DataFrame({"unexpected": [1]})}
    )
    result = collect(provider, ["BTC-USD", "ETH-USD"], START, END)
    assert result.report["status"] == "failed"
    assert list(result.report["failures"]) == ["BTC-USD", "ETH-USD"]
    assert result.report["quality"] == {}
    assert result.aligned.empty
    assert list(result.aligned) == ["source", "symbol", *COLUMNS, "is_missing"]
