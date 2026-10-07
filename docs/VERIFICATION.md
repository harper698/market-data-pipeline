# Verification record

## October 7, 2026 verification

Verified on **October 7, 2026 (America/New_York)** against commit
`aeb5c065673ed2a7027e0dd19e4181113f4264bf`. The initial working tree was clean;
`git diff` was empty and `git diff --check` passed. No reproducible application
defect was found, so this round changes only this verification record, with no
source, test, or dependency edits.

The local Windows environment used Python 3.12.10, pandas 2.3.3, NumPy 2.3.5,
requests 2.34.2, pytest 9.1.1, Ruff 0.16.10, and build 1.6.1.

| Check | Exit code | Observed result |
| --- | ---: | --- |
| `python -m pip check` | 0 | No broken requirements |
| `python -m ruff check .` | 0 | All checks passed |
| `python -m ruff format --check .` | 0 | 12 files already formatted |
| `python -m pytest -q` with JUnit output | 0 | 101 passed in 1.38 seconds |
| `python -m build --outdir output/c2c-verification-20261007/packages` | 0 | Built both the source distribution and wheel using the declared isolated backend |
| CSV demo, first and second ingestion | 0 / 0 | 14 aligned rows; 12 stored candles after each run; audit rows increased from 1 to 2 |
| CSV artifact and database assertions | 0 | Identical SHA-256 hashes for both CSV exports across runs; quality counts unchanged |
| CSV demo with `--strict-quality` | 3, expected | Reported the deliberately incomplete fixture after saving diagnostics |
| Fresh public Coinbase smoke test | 0 | BTC-USD and ETH-USD each returned 31 valid daily candles for January 2025 |
| Live artifact and database assertions | 0 | 62 stored/aligned candles; 31 rows in `closes.csv`; correct inclusive start and exclusive end |

An initial version-inspection command and `build --no-isolation` both exited 1
because setuptools was not installed in the active virtual environment. The
version inspection was corrected to report absent optional tooling, and the
normal isolated build succeeded with setuptools 84.0.0. This required no change
to project dependencies. Both failed attempts and their successful follow-ups
remain in the evidence log. The isolated build uses network access to provision
its declared backend; the test suite and CSV checks use local fixtures and mocks.

### Data and failure-boundary review

- Existing tests and source inspection cover conversion to UTC midnight,
  `[start, end)` filtering, rejecting the current incomplete UTC day, and a
  601-day request split across three windows of at most 300 days.
- Cleaning rejects invalid timestamps, off-grid rows, boolean or non-finite
  values, nonpositive prices, negative volume, and impossible OHLC relationships.
  Invalid later duplicates cannot erase a valid candle. Conflicting valid
  duplicates are counted, the last valid input wins, and missing days remain null.
- Retry limits, capped backoff/Retry-After, permanent HTTP failures, malformed
  responses, and exhaustion are covered by mocked transport tests. Source review
  confirms a session is created and closed inside each fetch, and responses are
  closed in a `finally` block before retries or error propagation.
- Symbol validation rejects traversal-like paths before invoking a provider.
  Candle writes and the run audit insert share a SQLite transaction; the existing
  rollback test verifies that an audit insert failure also undoes candle changes.
- Exports preserve failed-symbol columns and gaps. Individual files are replaced
  atomically; the database and the export directory are not one transaction, as
  already documented in the README.

The synthetic seven-day fixture produced BTC-USD: 5 valid, 2 missing, 1 rejected,
1 duplicate, and 1 conflicting timestamp; ETH-USD: 7 valid with no quality issues.
The fresh network request covered `2025-01-01 <= timestamp < 2025-02-01` and
produced 31 valid candles per symbol, with zero missing, rejected, duplicate, or
conflicting rows. It verifies endpoint access and internal validation at the time
of this run; it does not independently verify market-price accuracy.

### Evidence and external review

Local artifacts are under `output/c2c-verification-20261007/`: `evidence.log`
contains command arguments, stdout, stderr, timing, and exit codes; `summary.json`
contains the machine-readable summary; `pytest.xml` records the test results.
`demo/closes.csv` and `live/closes.csv` are available for the cross-project checks.
The output directory is intentionally ignored by Git; these are local evidence
files, not checked-in market data.

The root agent checked the existing [GitHub Actions run 37255387642](https://github.com/harper698/market-data-pipeline/actions/runs/37255387642)
on October 7 and confirmed Python 3.11, 3.12, and 3.13 jobs succeeded for this
commit. This was a check of an existing hosted run, not a newly triggered run.
The root agent retains the hosted evidence in the workspace's
`verification-20261007/hosted-ci.json`.

### Current cross-project checks

The root agent regenerated the synthetic pipeline output and converted the
complete ETH-USD series to the backtester's `timestamp,close` schema. Candle-start
timestamps were advanced exactly one UTC day to represent the close observation.
SMA(2,3) on seven prices produced six return intervals: the signal at close 24
filled at the following close 26, incurred 15 units of entry costs, and ended at
`9985 * 30 / 26 = 11521.153846153846`, matching the independent hand calculation.
The BTC-USD fixture retained its two missing prices; the backtester rejected it
with exit code 2 and did not create a report.

A second integration used the fresh public Coinbase BTC-USD output described
above: 31 prices, 30 return intervals, and close-observation timestamps from
January 2 through February 1, 2025. JSON values were finite, every fill followed
its signal by one day, and all five report artifacts were generated. These
checks were repeated against the current backtest CAGR correction.

The workspace contains the reproduction script
`verification-20261007/check_integration.py`; the backtest repository retains
the ignored `output/c2c-integration-20261007/summary.json` and `evidence.log`.
An initial verification-script console encoding error occurred after its
assertions passed; the script's output encoding was corrected and the final
checks exited 0. This was not an application defect.

**Independent ChatGPT final review passed on October 7, 2026 (task `c2c_a490`).**
The reviewer read all 19 execution-evidence parts across the three repositories
and independently inspected the five changed files. No further implementation
change was requested. This review does not extend the public API or CI evidence
beyond the checks and commits explicitly recorded here.

## October 4, 2026 verification (historical)

Verified locally on **October 4, 2026 (America/New_York)** with Python 3.12.10 on Windows,
pandas 2.3.3, NumPy 2.3.5, and requests 2.34.2.

| Check | Observed result |
| --- | --- |
| `python -m pytest -q` | 101 passed |
| `python -m ruff check .` | All checks passed |
| `python -m ruff format --check .` | All 11 Python files formatted |
| `python -m pip check` | No broken requirements |
| Offline seven-day demo | 14 aligned rows; 12 valid candles stored |
| Live Coinbase smoke test | BTC-USD and ETH-USD, January 2025: 31 candles each, no gaps or rejected rows |

The network smoke test was run separately from the deterministic test suite. It verifies
endpoint access at that time; it is not an availability guarantee or a price-accuracy audit.
Synthetic fixtures are the only market-like data committed to this repository.

## Covered failure modes

- Network timeouts, retry exhaustion, rate limiting, bounded Retry-After delays, permanent
  HTTP errors, invalid JSON, schema errors, paging, and response ordering.
- Invalid timestamps, timezones, off-grid observations, missing prices, infinities,
  impossible OHLC relationships, negative volume, and conflicting duplicates.
- Empty responses, independent symbol failures, missing CSV files, complete UTC-day
  boundaries, unsafe symbol paths, and input limits.
- Repeat ingestion, source isolation, preserving valid history across later gaps, and
  transaction rollback when the run audit cannot be inserted.
- All-failed exports, explicit exit codes, and matching the checked-in demo output.

## Reproduce

From the repository root, activate a virtual environment and run:

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m build
```

For the locally verified runtime versions, use
`python -m pip install -r requirements-lock.txt`. This file pins runtime packages;
development tools and the build backend are managed by `pyproject.toml`.
The GitHub Actions workflow independently exercises Python 3.11, 3.12, and 3.13.
See the workflow's actual status for hosted results.
