"""A small command-line interface suitable for scheduled batch jobs."""

import argparse
import logging
from pathlib import Path

from marketpipe.core import collect
from marketpipe.providers import CoinbaseProvider, CSVProvider
from marketpipe.storage import export, persist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch, validate and store completed UTC daily candles"
    )
    parser.add_argument("--provider", choices=["coinbase", "csv"], default="coinbase")
    parser.add_argument(
        "--symbols", nargs="+", required=True, help="BASE-QUOTE, e.g. BTC-USD ETH-USD"
    )
    parser.add_argument("--start", required=True, help="Inclusive UTC date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Exclusive UTC date, YYYY-MM-DD")
    parser.add_argument("--data-dir", type=Path, default=Path("examples/raw"))
    parser.add_argument("--db", type=Path, default=Path("output/market.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("output/latest"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--retries", type=int, default=3, help="Additional attempts per request")
    parser.add_argument("--strict-quality", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        provider = (
            CSVProvider(args.data_dir)
            if args.provider == "csv"
            else CoinbaseProvider(timeout=args.timeout, retries=args.retries)
        )
        result = collect(provider, args.symbols, args.start, args.end, args.workers)
        written = persist(result, args.db)
        export(result, args.output)
        logging.info(
            "%s: upserted %d candles; report: %s",
            result.report["status"],
            written,
            args.output / "report.json",
        )
        if result.report["failures"]:
            return 2
        return 3 if args.strict_quality and result.has_quality_issues else 0
    except Exception as exc:
        logging.error("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
