"""Normalize, align and collect candles. Missing observations stay missing."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

import numpy as np
import pandas as pd

VALUES = ["open", "high", "low", "close", "volume"]
COLUMNS = ["timestamp", *VALUES]
LOG = logging.getLogger(__name__)


class Provider(Protocol):
    name: str

    def fetch(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame: ...


@dataclass
class Result:
    aligned: pd.DataFrame
    report: dict

    @property
    def has_quality_issues(self) -> bool:
        return any(
            q["missing_rows"] or q["rejected_rows"] or q["conflicting_timestamps"]
            for q in self.report["quality"].values()
        )


def utc_day(value: str | pd.Timestamp) -> pd.Timestamp:
    """Naive dates mean UTC; timezone-aware inputs are converted before validation."""
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("Dates must not be empty or NaT")
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    if stamp != stamp.normalize():
        raise ValueError("Dates must fall on midnight UTC (use YYYY-MM-DD)")
    return stamp


def clean(raw: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, dict]:
    """Reject invalid rows before deduplication, then expose every expected UTC day."""
    missing_columns = set(COLUMNS) - set(raw.columns)
    if missing_columns:
        raise ValueError(f"Missing columns: {', '.join(sorted(missing_columns))}")
    frame = raw.loc[:, COLUMNS].copy().reset_index(drop=True)
    # Numeric dates have ambiguous units in CSV. The API adapter converts epoch seconds itself.
    numeric_dates = frame["timestamp"].map(
        lambda value: isinstance(value, (int, float, np.number, bool))
    )
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"].mask(numeric_dates), utc=True, errors="coerce", format="mixed"
    )
    bad_time = frame["timestamp"].isna()
    off_grid = ~bad_time & frame["timestamp"].ne(frame["timestamp"].dt.normalize())
    outside = ~bad_time & ~off_grid & ~frame["timestamp"].between(start, end, inclusive="left")
    candidates = ~(bad_time | off_grid | outside)
    for column in VALUES:
        bool_values = frame[column].map(lambda value: isinstance(value, (bool, np.bool_)))
        frame[column] = pd.to_numeric(frame[column].mask(bool_values), errors="coerce")
    numbers = frame[VALUES].to_numpy(dtype=float, na_value=np.nan)
    valid_values = pd.Series(np.isfinite(numbers).all(axis=1), index=frame.index)
    valid_values &= (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
    valid_values &= frame["volume"] >= 0
    valid_values &= frame["high"] >= frame[["open", "low", "close"]].max(axis=1)
    valid_values &= frame["low"] <= frame[["open", "high", "close"]].min(axis=1)
    invalid_values = candidates & ~valid_values
    valid = frame.loc[candidates & valid_values]
    conflicting = valid.groupby("timestamp")[VALUES].nunique().gt(1).any(axis=1).sum()
    deduplicated = valid.drop_duplicates("timestamp", keep="last")
    grid = pd.date_range(start, end, freq="D", inclusive="left", name="timestamp")
    aligned = deduplicated.set_index("timestamp").reindex(grid)
    aligned["is_missing"] = aligned["close"].isna()
    quality = {
        "input_rows": len(raw),
        "expected_rows": len(grid),
        "valid_rows": len(deduplicated),
        "missing_rows": int(aligned["is_missing"].sum()),
        "rejected_rows": int((bad_time | off_grid | invalid_values).sum()),
        "invalid_timestamps": int(bad_time.sum()),
        "off_grid_rows": int(off_grid.sum()),
        "invalid_values": int(invalid_values.sum()),
        "out_of_range_rows": int(outside.sum()),
        "duplicate_rows": len(valid) - len(deduplicated),
        "conflicting_timestamps": int(conflicting),
    }
    return aligned.reset_index(), quality


def collect(
    provider: Provider,
    symbols: list[str],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    workers: int = 4,
) -> Result:
    start, end = utc_day(start), utc_day(end)
    symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    if not symbols or len(symbols) > 50:
        raise ValueError("Supply 1 to 50 unique symbols")
    if any(not re.fullmatch(r"[A-Z0-9]{1,15}-[A-Z0-9]{1,15}", s) for s in symbols):
        raise ValueError("Symbols must be BASE-QUOTE pairs, such as BTC-USD")
    if not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    if end <= start or (end - start).days > 3660:
        raise ValueError("Date range must contain 1 to 3660 days; end is exclusive")
    if end > pd.Timestamp.now(tz="UTC").normalize():
        raise ValueError("Only completed UTC daily candles are supported; end cannot exceed today")
    report = {
        "run_id": str(uuid4()),
        "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": provider.name,
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "symbols": symbols,
        "quality": {},
        "failures": {},
    }
    frames = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as executor:
        futures = {executor.submit(provider.fetch, s, start, end): s for s in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                frame, quality = clean(future.result(), start, end)
                frame.insert(0, "symbol", symbol)
                frame.insert(0, "source", provider.name)
                frames[symbol] = frame
                report["quality"][symbol] = quality
                LOG.info(
                    "%s: %s valid, %s missing, %s rejected",
                    symbol,
                    quality["valid_rows"],
                    quality["missing_rows"],
                    quality["rejected_rows"],
                )
            except Exception as exc:
                report["failures"][symbol] = f"{type(exc).__name__}: {exc}"
                LOG.error("%s failed: %s", symbol, exc)
    # Concurrent completion order never changes the export order.
    report["quality"] = {s: report["quality"][s] for s in symbols if s in frames}
    report["failures"] = {s: report["failures"][s] for s in symbols if s not in frames}
    aligned = (
        pd.concat([frames[s] for s in symbols if s in frames], ignore_index=True)
        if frames
        else pd.DataFrame(columns=["source", "symbol", *COLUMNS, "is_missing"])
    )
    report["status"] = "failed" if not frames else "partial" if report["failures"] else "complete"
    result = Result(aligned, report)
    report["has_quality_issues"] = result.has_quality_issues
    return result
