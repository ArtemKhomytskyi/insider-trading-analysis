"""Standalone runner for filing lag audit (Phase 2.5 fix)."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from src.analytics.raw_data_audit import EXPORT_DIR, compute_filing_lag, configure_logging
from src.utils.db import get_connection


def main() -> None:
    parser = argparse.ArgumentParser(description="Run filing lag audit on raw SEC data.")
    parser.add_argument("--export-dir", type=Path, default=EXPORT_DIR)
    parser.add_argument("--chunk-size", type=int, default=100_000)
    parser.add_argument("--sample-mode", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=500_000)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)

    conn = get_connection()
    try:
        results = compute_filing_lag(
            conn,
            export_dir=args.export_dir,
            chunk_size=args.chunk_size,
            sample_mode=args.sample_mode,
            sample_limit=args.sample_limit,
        )
        stats = results["lag_stats"].iloc[0].to_dict()
        print("\nFiling lag summary:")
        for key, value in stats.items():
            print(f"  {key}: {value}")
        print(f"\nExports: {args.export_dir.resolve()}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
