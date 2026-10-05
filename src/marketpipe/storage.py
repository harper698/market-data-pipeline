"""Single-writer SQLite transactions and per-run, atomic-file exports."""

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pandas as pd

from marketpipe.core import VALUES, Result

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    source TEXT NOT NULL, symbol TEXT NOT NULL, timestamp TEXT NOT NULL,
    open REAL NOT NULL CHECK (open > 0), high REAL NOT NULL CHECK (high > 0),
    low REAL NOT NULL CHECK (low > 0), close REAL NOT NULL CHECK (close > 0),
    volume REAL NOT NULL CHECK (volume >= 0),
    CHECK (low <= open AND low <= close AND high >= open AND high >= close AND high >= low),
    PRIMARY KEY (source, symbol, timestamp)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, report_json TEXT NOT NULL
);
"""


def persist(result: Result, db: Path) -> int:
    db.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for row in result.aligned.loc[~result.aligned["is_missing"].astype(bool)].itertuples():
        rows.append(
            (
                row.source,
                row.symbol,
                row.timestamp.isoformat(),
                *(float(getattr(row, v)) for v in VALUES),
            )
        )
    with closing(sqlite3.connect(db, timeout=30)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA)
        with connection:
            connection.executemany(
                """INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, symbol, timestamp) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume""",
                rows,
            )
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?)",
                (
                    result.report["run_id"],
                    result.report["created_at"],
                    json.dumps(result.report, allow_nan=False, sort_keys=True),
                ),
            )
    return len(rows)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export(result: Result, directory: Path) -> None:
    long = result.aligned.copy()
    if not long.empty:
        long["timestamp"] = long["timestamp"].map(pd.Timestamp.isoformat)
    closes = long.pivot(index="timestamp", columns="symbol", values="close")
    # Even failed symbols are visible as all-null columns in the requested UTC grid.
    grid = pd.date_range(
        result.report["start"],
        result.report["end_exclusive"],
        freq="D",
        inclusive="left",
        name="timestamp",
    ).map(pd.Timestamp.isoformat)
    closes = closes.reindex(index=grid, columns=result.report["symbols"])
    _atomic_text(directory / "aligned.csv", long.to_csv(index=False))
    _atomic_text(directory / "closes.csv", closes.to_csv())
    _atomic_text(
        directory / "report.json", json.dumps(result.report, indent=2, allow_nan=False) + "\n"
    )
