"""Public Coinbase candles and local CSV inputs behind one small interface."""

from __future__ import annotations

import logging
import math
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
MAX_RETRIES = 8
MAX_RETRY_DELAY = 30.0
LOG = logging.getLogger(__name__)


class ProviderError(Exception):
    """An input source could not supply a structurally valid response."""


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Bound both exponential backoff and untrusted Retry-After headers."""
    delay = min(0.5 * 2**attempt + random.uniform(0, 0.25), MAX_RETRY_DELAY)
    if retry_after:
        try:
            seconds = float(retry_after)
        except (ValueError, TypeError):
            try:
                deadline = parsedate_to_datetime(retry_after)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UTC)
                seconds = (deadline - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return delay
        if math.isfinite(seconds):
            delay = max(delay, min(max(seconds, 0), MAX_RETRY_DELAY))
    return delay


class CoinbaseProvider:
    """Fetch daily OHLCV without credentials, with a fresh session per fetch.

    ``retries`` is the number of additional attempts per request (0 through 8).
    Only connection failures, timeouts, HTTP 429, and HTTP 5xx are retried.
    Missing candles stay missing; the cleaning layer owns price validation.
    """

    name = "coinbase"
    base_url = "https://api.exchange.coinbase.com"

    def __init__(self, timeout: float = 10, retries: int = 3):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite number greater than zero")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite number greater than zero")
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise ValueError(f"retries must be an integer from 0 to {MAX_RETRIES}")
        if not 0 <= retries <= MAX_RETRIES:
            raise ValueError(f"retries must be an integer from 0 to {MAX_RETRIES}")
        self.timeout = float(timeout)
        self.retries = retries

    def fetch(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Fetch UTC daily candles in [start, end), retaining duplicate rows.

        The caller supplies UTC midnight boundaries and excludes today's open
        candle. Windows contain at most 300 days, per Coinbase's documented cap.
        """
        rows: list[list] = []
        cursor = start
        # A Session is never shared between worker threads.
        with requests.Session() as session:
            session.headers.update({"User-Agent": "marketpipe/1.0", "Accept": "application/json"})
            while cursor < end:
                boundary = min(cursor + pd.Timedelta(days=300), end)
                params = {
                    "granularity": 86400,
                    "start": cursor.isoformat(),
                    # Make the exclusive boundary safe for inclusive API ends.
                    "end": (boundary - pd.Timedelta(seconds=1)).isoformat(),
                }
                payload = self._request(session, symbol, params)
                rows.extend(self._parse(payload, symbol))
                cursor = boundary
        frame = pd.DataFrame(rows, columns=COLUMNS)
        if frame.empty:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
            return frame
        # Coinbase can return candles before the requested start. Leave order
        # and overlap visible so the cleaner can count and resolve duplicates.
        return frame.loc[(frame["timestamp"] >= start) & (frame["timestamp"] < end)].reset_index(
            drop=True
        )

    def _request(self, session: requests.Session, symbol: str, params: dict) -> object:
        url = f"{self.base_url}/products/{quote(symbol, safe='')}/candles"
        for attempt in range(self.retries + 1):
            try:
                response = session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == self.retries:
                    raise ProviderError(
                        f"{symbol}: network request failed after {attempt + 1} attempts "
                        f"({type(exc).__name__})"
                    ) from exc
                delay = _retry_delay(attempt)
                LOG.warning(
                    "%s: retry %d after %s; wait %.2fs",
                    symbol,
                    attempt + 1,
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)
                continue
            except requests.RequestException as exc:
                raise ProviderError(f"{symbol}: request failed ({type(exc).__name__})") from exc

            try:
                status = response.status_code
                if status == 429 or 500 <= status <= 599:
                    if attempt == self.retries:
                        raise ProviderError(f"{symbol}: HTTP {status} after {attempt + 1} attempts")
                    delay = _retry_delay(attempt, response.headers.get("Retry-After"))
                    LOG.warning(
                        "%s: retry %d after HTTP %d; wait %.2fs", symbol, attempt + 1, status, delay
                    )
                elif status != 200:
                    raise ProviderError(f"{symbol}: HTTP {status}; request will not be retried")
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ProviderError(f"{symbol}: response is not valid JSON") from exc
            finally:
                response.close()
            time.sleep(delay)
        raise AssertionError("retry loop must return or raise")

    @staticmethod
    def _parse(payload: object, symbol: str) -> list[list]:
        if not isinstance(payload, list):
            raise ProviderError(f"{symbol}: expected a JSON array of candles")
        rows = []
        for index, candle in enumerate(payload):
            if not isinstance(candle, list) or len(candle) != 6:
                raise ProviderError(f"{symbol}: candle {index} must contain six values")
            if any(isinstance(value, (dict, list)) for value in candle):
                raise ProviderError(f"{symbol}: candle {index} contains a non-scalar value")
            epoch = candle[0]
            if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
                raise ProviderError(f"{symbol}: candle {index} has an invalid Unix timestamp")
            try:
                if not math.isfinite(epoch) or epoch != int(epoch):
                    raise ValueError("timestamp is not a finite integer")
                timestamp = pd.to_datetime(epoch, unit="s", utc=True)
            except (ValueError, OverflowError) as exc:
                raise ProviderError(
                    f"{symbol}: candle {index} has an invalid Unix timestamp"
                ) from exc
            # Coinbase order: time, low, high, open, close, volume.
            rows.append([timestamp, candle[3], candle[2], candle[1], candle[4], candle[5]])
        return rows


class CSVProvider:
    """Load {symbol}.csv fixtures; the cleaner validates values and time range."""

    name = "csv"

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def fetch(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        # Symbol safety and date validation are enforced at the pipeline boundary.
        path = self.data_dir / f"{symbol}.csv"
        try:
            frame = pd.read_csv(path)
        except (OSError, UnicodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise ProviderError(f"{symbol}: cannot read CSV ({type(exc).__name__})") from exc
        missing = sorted(set(COLUMNS) - set(frame.columns))
        if missing:
            raise ProviderError(f"{symbol}: CSV is missing columns: {', '.join(missing)}")
        return frame[COLUMNS].copy()
