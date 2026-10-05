"""Persistent integrity and user-facing exit codes with entirely local fixtures."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest

from marketpipe.cli import main
from marketpipe.core import COLUMNS, collect
from marketpipe.providers import CSVProvider
from marketpipe.storage import export, persist

START = "2024-01-01"
END = "2024-01-04"


def raw(close=110):
    return pd.DataFrame(
        [
            ["2024-01-01", 100, 120, 90, close, 7],
            ["2024-01-03", 100, 120, 90, 115, 9],
        ],
        columns=COLUMNS,
    )


class Provider:
    name = "fake"

    def __init__(self, frame=None, failure=False, name="fake"):
        self.frame = raw() if frame is None else frame
        self.failure = failure
        self.name = name

    def fetch(self, symbol, start, end):
        if self.failure or symbol == "FAIL-USD":
            raise RuntimeError("source offline")
        return self.frame.copy()


def query(db, statement):
    with closing(sqlite3.connect(db)) as connection:
        return connection.execute(statement).fetchall()


def test_new_runs_upsert_idempotently_but_keep_each_run_report(tmp_path):
    db = tmp_path / "nested" / "market.sqlite"
    first = collect(Provider(), ["BTC-USD"], START, END)
    second = collect(Provider(raw(close=118)), ["BTC-USD"], START, END)
    assert first.report["run_id"] != second.report["run_id"]
    assert persist(first, db) == persist(second, db) == 2
    assert query(db, "SELECT COUNT(*) FROM candles") == [(2,)]
    assert query(db, "SELECT COUNT(*) FROM runs") == [(2,)]
    assert query(db, "SELECT close FROM candles ORDER BY timestamp") == [(118,), (115,)]


def test_sources_have_separate_keys_and_missing_rows_never_erase_existing_values(tmp_path):
    db = tmp_path / "market.sqlite"
    persist(collect(Provider(), ["BTC-USD"], START, END), db)
    persist(collect(Provider(raw(close=119), name="another"), ["BTC-USD"], START, END), db)
    empty = collect(Provider(pd.DataFrame(columns=COLUMNS)), ["BTC-USD"], START, END)
    assert persist(empty, db) == 0
    assert query(db, "SELECT COUNT(*) FROM candles") == [(4,)]
    assert query(db, "SELECT source, close FROM candles ORDER BY source, timestamp") == [
        ("another", 119),
        ("another", 115),
        ("fake", 110),
        ("fake", 115),
    ]


def test_report_insert_failure_rolls_back_all_candle_changes(tmp_path):
    db = tmp_path / "market.sqlite"
    first = collect(Provider(), ["BTC-USD"], START, END)
    persist(first, db)
    changed = collect(Provider(raw(close=118)), ["BTC-USD", "ETH-USD"], START, END)
    changed.report["run_id"] = first.report["run_id"]
    with pytest.raises(sqlite3.IntegrityError):
        persist(changed, db)
    assert query(db, "SELECT symbol, close FROM candles ORDER BY timestamp") == [
        ("BTC-USD", 110),
        ("BTC-USD", 115),
    ]
    assert query(db, "SELECT COUNT(*) FROM runs") == [(1,)]


def test_export_preserves_gaps_failed_symbols_and_valid_json(tmp_path):
    result = collect(Provider(), ["FAIL-USD", "BTC-USD"], START, END)
    export(result, tmp_path)
    closes = pd.read_csv(tmp_path / "closes.csv")
    aligned = pd.read_csv(tmp_path / "aligned.csv")
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert list(closes) == ["timestamp", "FAIL-USD", "BTC-USD"]
    assert closes["FAIL-USD"].isna().all()
    assert closes["BTC-USD"].iloc[[0, 2]].tolist() == [110, 115]
    assert pd.isna(closes["BTC-USD"].iloc[1])
    assert aligned["is_missing"].tolist() == [False, True, False]
    assert report == result.report
    assert all(value.endswith("+00:00") for value in closes["timestamp"])
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "aligned.csv",
        "closes.csv",
        "report.json",
    ]


def test_all_failed_run_can_be_persisted_and_exported(tmp_path):
    result = collect(Provider(failure=True), ["BTC-USD", "ETH-USD"], START, END)
    db = tmp_path / "market.sqlite"
    assert persist(result, db) == 0
    export(result, tmp_path / "export")
    closes = pd.read_csv(tmp_path / "export" / "closes.csv")
    assert len(closes) == 3
    assert closes[["BTC-USD", "ETH-USD"]].isna().all().all()
    assert pd.read_csv(tmp_path / "export" / "aligned.csv").empty
    assert query(db, "SELECT COUNT(*) FROM runs") == [(1,)]
    report = json.loads(query(db, "SELECT report_json FROM runs")[0][0])
    assert report["status"] == "failed"


@pytest.fixture
def cli_args(tmp_path):
    data_dir = tmp_path / "raw"
    data_dir.mkdir()
    raw().to_csv(data_dir / "BTC-USD.csv", index=False)
    return [
        "--provider",
        "csv",
        "--symbols",
        "BTC-USD",
        "--start",
        START,
        "--end",
        END,
        "--data-dir",
        str(data_dir),
        "--db",
        str(tmp_path / "db.sqlite"),
        "--output",
        str(tmp_path / "export"),
    ]


def test_cli_exit_zero_writes_usable_artifacts_even_with_reported_gaps(cli_args, tmp_path):
    assert main(cli_args) == 0
    report = json.loads((tmp_path / "export" / "report.json").read_text(encoding="utf-8"))
    assert report["has_quality_issues"]
    assert report["quality"]["BTC-USD"]["missing_rows"] == 1
    assert query(tmp_path / "db.sqlite", "SELECT COUNT(*) FROM candles") == [(2,)]


def test_cli_strict_quality_returns_three_after_saving_diagnostics(cli_args, tmp_path):
    assert main([*cli_args, "--strict-quality"]) == 3
    assert (tmp_path / "export" / "report.json").is_file()
    assert query(tmp_path / "db.sqlite", "SELECT COUNT(*) FROM runs") == [(1,)]


@pytest.mark.parametrize("symbols", [["BTC-USD", "FAIL-USD"], ["FAIL-USD"]])
def test_cli_any_source_failure_returns_two_even_in_strict_mode(cli_args, tmp_path, symbols):
    symbol_index = cli_args.index("BTC-USD")
    cli_args[symbol_index : symbol_index + 1] = symbols
    assert main([*cli_args, "--strict-quality"]) == 2
    report = json.loads((tmp_path / "export" / "report.json").read_text(encoding="utf-8"))
    assert "FAIL-USD" in report["failures"]


def test_cli_bad_inputs_return_one_without_output(cli_args, tmp_path):
    cli_args[cli_args.index(START)] = "not-a-date"
    assert main(cli_args) == 1
    assert not (tmp_path / "db.sqlite").exists()
    assert not (tmp_path / "export").exists()


def test_cli_storage_failure_returns_one(cli_args, tmp_path):
    cli_args[cli_args.index(str(tmp_path / "db.sqlite"))] = str(tmp_path)
    assert main(cli_args) == 1


def test_cli_help_and_argument_errors_have_documented_exit_codes(capsys):
    assert main(["--help"]) == 0
    assert "completed UTC daily candles" in capsys.readouterr().out
    assert main([]) == 1
    assert "required" in capsys.readouterr().err


def test_checked_in_demo_outputs_match_reproducible_fixture_run(tmp_path):
    examples = Path(__file__).resolve().parents[1] / "examples"
    expected_dir = examples / "demo-output"
    expected = json.loads((expected_dir / "quality.json").read_text(encoding="utf-8"))
    result = collect(
        CSVProvider(examples / "raw"),
        expected["symbols"],
        expected["start"],
        expected["end_exclusive"],
    )
    assert {key: result.report[key] for key in expected} == expected
    export(result, tmp_path)
    for filename in ["aligned.csv", "closes.csv"]:
        pd.testing.assert_frame_equal(
            pd.read_csv(tmp_path / filename), pd.read_csv(expected_dir / filename)
        )
