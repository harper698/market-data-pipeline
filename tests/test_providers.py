"""Transport tests use fake sessions only; the suite never contacts Coinbase."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests

from marketpipe import providers
from marketpipe.providers import COLUMNS, CoinbaseProvider, CSVProvider, ProviderError

START = pd.Timestamp("2024-01-01", tz="UTC")
END = START + pd.Timedelta(days=3)


def candle(day=0, **changes):
    values = [int((START + pd.Timedelta(days=day)).timestamp()), 90, 120, 100, 110, 7]
    for key, value in changes.items():
        values[int(key)] = value
    return values


def response(payload=None, status=200, headers=None):
    result = MagicMock()
    result.status_code = status
    result.headers = headers or {}
    result.json.return_value = [] if payload is None else payload
    return result


@pytest.fixture
def transport(monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    factory = MagicMock(return_value=session)
    sleeps = MagicMock()
    monkeypatch.setattr(providers.requests, "Session", factory)
    monkeypatch.setattr(providers.time, "sleep", sleeps)
    monkeypatch.setattr(providers.random, "uniform", lambda lower, upper: 0.0)
    return session, factory, sleeps


def test_maps_candle_order_keeps_duplicates_and_filters_range(transport):
    session, _, _ = transport
    session.get.return_value = response([candle(2), candle(-1), candle(0), candle(0), candle(3)])
    frame = CoinbaseProvider(timeout=4).fetch("BTC-USD", START, END)
    assert list(frame.columns) == COLUMNS
    assert frame["timestamp"].tolist() == [START + pd.Timedelta(days=2), START, START]
    assert frame.iloc[0][["open", "high", "low", "close", "volume"]].tolist() == [
        100,
        120,
        90,
        110,
        7,
    ]
    assert str(frame["timestamp"].dt.tz) == "UTC"
    assert session.get.call_args.kwargs["timeout"] == 4
    session.get.return_value.close.assert_called_once()


def test_chunks_more_than_300_days_without_skipping_a_boundary(transport):
    session, _, _ = transport
    session.get.side_effect = [
        response([candle(299)]),
        response([candle(300), candle(599)]),
        response([candle(600)]),
    ]
    frame = CoinbaseProvider().fetch("ETH-USD", START, START + pd.Timedelta(days=601))
    calls = session.get.call_args_list
    assert len(calls) == 3
    assert len(frame) == 4
    for index, call in enumerate(calls):
        params = call.kwargs["params"]
        begin, finish = pd.Timestamp(params["start"]), pd.Timestamp(params["end"])
        assert begin == START + pd.Timedelta(days=index * 300)
        assert pd.Timedelta(0) < finish - begin < pd.Timedelta(days=300)
        assert params["granularity"] == 86400
    assert pd.Timestamp(calls[-1].kwargs["params"]["end"]) + pd.Timedelta(
        seconds=1
    ) == START + pd.Timedelta(days=601)


def test_empty_response_has_schema_and_utc_dtype(transport):
    session, _, _ = transport
    session.get.return_value = response([])
    frame = CoinbaseProvider().fetch("BTC-USD", START, END)
    assert frame.empty
    assert list(frame.columns) == COLUMNS
    assert str(frame["timestamp"].dt.tz) == "UTC"


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.ConnectionError(), 429, 500, 503])
def test_retries_transient_failures_then_returns_result(transport, failure):
    session, _, sleeps = transport
    first = response(status=failure) if isinstance(failure, int) else failure
    session.get.side_effect = [first, response([candle()])]
    frame = CoinbaseProvider(retries=1).fetch("BTC-USD", START, END)
    assert len(frame) == 1
    assert session.get.call_count == 2
    sleeps.assert_called_once_with(0.5)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 422])
def test_does_not_retry_permanent_http_errors(transport, status):
    session, _, sleeps = transport
    session.get.return_value = response(status=status)
    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        CoinbaseProvider().fetch("BTC-USD", START, END)
    assert session.get.call_count == 1
    sleeps.assert_not_called()
    session.get.return_value.close.assert_called_once()


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.ConnectionError(), 429, 503])
def test_retry_budget_means_additional_attempts(transport, failure):
    session, _, sleeps = transport
    if isinstance(failure, int):
        session.get.return_value = response(status=failure)
    else:
        session.get.side_effect = failure
    with pytest.raises(ProviderError, match="after 3 attempts"):
        CoinbaseProvider(retries=2).fetch("BTC-USD", START, END)
    assert session.get.call_count == 3
    assert [call.args[0] for call in sleeps.call_args_list] == [0.5, 1.0]


def test_does_not_retry_non_network_request_exception(transport):
    session, _, sleeps = transport
    session.get.side_effect = requests.exceptions.InvalidURL("invalid")
    with pytest.raises(ProviderError, match="InvalidURL"):
        CoinbaseProvider().fetch("BTC-USD", START, END)
    assert session.get.call_count == 1
    sleeps.assert_not_called()


def test_malformed_json_is_classified_and_not_retried(transport):
    session, _, sleeps = transport
    session.get.return_value = response()
    session.get.return_value.json.side_effect = ValueError("bad JSON")
    with pytest.raises(ProviderError, match="not valid JSON"):
        CoinbaseProvider().fetch("BTC-USD", START, END)
    assert session.get.call_count == 1
    sleeps.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "oops"},
        None,
        [1],
        [[1, 2, 3]],
        [candle(**{"0": None})],
        [candle(**{"0": True})],
        [candle(**{"0": "bad"})],
        [candle(**{"0": float("inf")})],
        [candle(**{"0": 1.5})],
        [candle(**{"0": 10**30})],
        [candle(**{"2": {"value": 100}})],
    ],
)
def test_malformed_response_shape_fails_the_symbol(transport, payload):
    session, _, sleeps = transport
    result = response()
    result.json.return_value = payload
    session.get.return_value = result
    with pytest.raises(ProviderError):
        CoinbaseProvider().fetch("BTC-USD", START, END)
    assert session.get.call_count == 1
    sleeps.assert_not_called()


def test_invalid_price_cells_are_preserved_for_quality_rejection(transport):
    session, _, _ = transport
    session.get.return_value = response([candle(**{"3": "broken", "4": None, "5": -1})])
    frame = CoinbaseProvider().fetch("BTC-USD", START, END)
    assert frame.iloc[0]["open"] == "broken"
    assert frame.iloc[0]["close"] is None
    assert frame.iloc[0]["volume"] == -1


def test_retry_after_is_respected_and_capped(transport):
    session, _, sleeps = transport
    session.get.side_effect = [
        response(status=429, headers={"Retry-After": "999999"}),
        response([candle()]),
    ]
    CoinbaseProvider().fetch("BTC-USD", START, END)
    sleeps.assert_called_once_with(30.0)
    assert providers._retry_delay(0, "2") == 2.0
    assert providers._retry_delay(0, "-1") == 0.5
    assert providers._retry_delay(0, "invalid") == 0.5
    assert providers._retry_delay(0, "inf") == 0.5
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=60))
    assert providers._retry_delay(0, future) == 30.0
    assert providers._retry_delay(8) == 30.0


def test_fetch_uses_a_new_session_each_time(transport):
    session, factory, _ = transport
    session.get.return_value = response([])
    provider = CoinbaseProvider()
    provider.fetch("BTC-USD", START, END)
    provider.fetch("ETH-USD", START, END)
    assert factory.call_count == 2
    assert session.__exit__.call_count == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": True},
        {"timeout": "10"},
        {"retries": -1},
        {"retries": 9},
        {"retries": 1.5},
        {"retries": True},
    ],
)
def test_invalid_transport_settings_fail_early(kwargs):
    with pytest.raises(ValueError):
        CoinbaseProvider(**kwargs)


def test_csv_preserves_invalid_rows_for_cleaner(tmp_path):
    (tmp_path / "BTC-USD.csv").write_text(
        "timestamp,open,high,low,close,volume,extra\ninvalid,broken,120,90,110,7,ignored\n",
        encoding="utf-8",
    )
    frame = CSVProvider(tmp_path).fetch("BTC-USD", START, END)
    assert list(frame.columns) == COLUMNS
    assert frame.iloc[0]["timestamp"] == "invalid"
    assert frame.iloc[0]["open"] == "broken"


def test_csv_missing_columns_fail(tmp_path):
    (tmp_path / "BTC-USD.csv").write_text("timestamp,open\n2024-01-01,1\n", encoding="utf-8")
    with pytest.raises(ProviderError, match="missing columns: close, high, low, volume"):
        CSVProvider(tmp_path).fetch("BTC-USD", START, END)


def test_csv_missing_file_is_classified(tmp_path):
    with pytest.raises(ProviderError, match="FileNotFoundError"):
        CSVProvider(tmp_path).fetch("BTC-USD", START, END)


def test_csv_empty_file_is_classified(tmp_path):
    (tmp_path / "BTC-USD.csv").write_text("", encoding="utf-8")
    with pytest.raises(ProviderError, match="EmptyDataError"):
        CSVProvider(tmp_path).fetch("BTC-USD", START, END)
