# Verification record

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
