"""Read-only QA for Phase 5 market data."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.utils.db import PROJECT_ROOT, get_connection, table_exists

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "market_data"


@dataclass
class MarketDataCheck:
    check_id: str
    check_name: str
    severity: str
    metric_value: str
    threshold: str
    description: str
    recommended_action: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def scalar(conn: sqlite3.Connection, sql: str) -> Any:
    return conn.execute(sql).fetchone()[0]


def export_csv(df: pd.DataFrame, filename: str, export_dir: Path) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(export_dir / filename, index=False)
    logger.info("exported %s (%s rows)", filename, len(df))


def severity(count: int, *, fail: bool = True) -> str:
    if count > 0:
        return "FAIL" if fail else "WARN"
    return "PASS"


def run_validation(conn: sqlite3.Connection, export_dir: Path = EXPORT_DIR) -> pd.DataFrame:
    checks: list[MarketDataCheck] = []
    required = ["market_prices_daily", "market_data_download_status", "transaction_market_mapping"]
    missing = [name for name in required if not table_exists(conn, name)]
    checks.append(
        MarketDataCheck(
            "D00",
            "market_data_tables_exist",
            "FAIL" if missing else "PASS",
            ",".join(missing) or "none",
            "none missing",
            "Market-data tables must exist",
            "Run market-data schema setup/downloader",
        )
    )
    if missing:
        summary = pd.DataFrame([c.to_dict() for c in checks])
        export_csv(summary, "market_data_validation_summary.csv", export_dir)
        return summary

    sql_checks = [
        ("D01", "duplicate_symbol_date_provider", "SELECT COUNT(*) FROM (SELECT market_data_symbol, trading_date, provider FROM market_prices_daily GROUP BY market_data_symbol, trading_date, provider HAVING COUNT(*) > 1)", "No duplicate price rows"),
        ("D02", "invalid_ohlc_relationships", "SELECT COUNT(*) FROM market_prices_daily WHERE low IS NOT NULL AND high IS NOT NULL AND (low > high OR (open IS NOT NULL AND (open < low OR open > high)) OR (close IS NOT NULL AND (close < low OR close > high)))", "OHLC must be internally consistent"),
        ("D03", "non_positive_adjusted_close", "SELECT COUNT(*) FROM market_prices_daily WHERE adjusted_close <= 0", "Adjusted close must be positive"),
        ("D04", "non_positive_raw_close", "SELECT COUNT(*) FROM market_prices_daily WHERE close IS NOT NULL AND close <= 0", "Raw close must be positive where supplied"),
        ("D05", "negative_volume", "SELECT COUNT(*) FROM market_prices_daily WHERE volume IS NOT NULL AND volume < 0", "Volume cannot be negative"),
    ]
    for check_id, name, sql, desc in sql_checks:
        count = int(scalar(conn, sql) or 0)
        checks.append(MarketDataCheck(check_id, name, severity(count), str(count), "0", desc, "Inspect market-data exports"))

    summary_df = pd.read_sql_query(
        """
        SELECT provider, status, COUNT(*) AS symbol_count, SUM(row_count) AS price_rows
        FROM market_data_download_status
        GROUP BY provider, status
        ORDER BY provider, status
        """,
        conn,
    )
    export_csv(summary_df, "market_data_download_summary.csv", export_dir)
    symbol_coverage = pd.read_sql_query(
        """
        SELECT tm.market_data_symbol,
               COUNT(*) AS mapped_mvp_transactions,
               CASE WHEN p.market_data_symbol IS NOT NULL THEN 1 ELSE 0 END AS has_any_price_data
        FROM transaction_market_mapping tm
        JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
        LEFT JOIN (SELECT DISTINCT market_data_symbol FROM market_prices_daily) p
          ON tm.market_data_symbol = p.market_data_symbol
        WHERE t.include_mvp_alpha = 1 AND tm.is_mapped = 1
        GROUP BY tm.market_data_symbol, has_any_price_data
        ORDER BY mapped_mvp_transactions DESC
        """,
        conn,
    )
    export_csv(symbol_coverage, "market_data_symbol_coverage.csv", export_dir)
    date_coverage = pd.read_sql_query(
        """
        SELECT market_data_symbol, provider, MIN(trading_date) AS earliest_market_date,
               MAX(trading_date) AS latest_market_date, COUNT(*) AS row_count
        FROM market_prices_daily
        GROUP BY market_data_symbol, provider
        """,
        conn,
    )
    export_csv(date_coverage, "market_data_date_coverage.csv", export_dir)
    export_csv(
        pd.read_sql_query("SELECT * FROM market_data_download_status WHERE status = 'FAILED'", conn),
        "market_data_failed_symbols.csv",
        export_dir,
    )
    export_csv(
        pd.read_sql_query("SELECT * FROM market_data_download_status WHERE status = 'NO_DATA'", conn),
        "market_data_no_data_symbols.csv",
        export_dir,
    )
    readiness = pd.read_sql_query(
        """
        SELECT tm.transaction_id, tm.market_data_symbol, t.filing_date,
               CASE WHEN p0.trading_date IS NOT NULL THEN 1 ELSE 0 END AS has_signal_date_price_context,
               0 AS has_5d_forward_coverage,
               0 AS has_20d_forward_coverage,
               0 AS has_60d_forward_coverage,
               0 AS has_120d_forward_coverage,
               0 AS has_252d_forward_coverage
        FROM transaction_market_mapping tm
        JOIN clean_insider_transactions t ON tm.transaction_id = t.transaction_id
        LEFT JOIN market_prices_daily p0
          ON tm.market_data_symbol = p0.market_data_symbol
         AND t.filing_date = p0.trading_date
        WHERE t.include_mvp_alpha = 1 AND tm.is_mapped = 1
        LIMIT 100000
        """,
        conn,
    )
    export_csv(readiness, "mvp_market_data_readiness.csv", export_dir)
    delisted = pd.read_sql_query(
        """
        SELECT status, COUNT(*) AS symbol_count
        FROM market_data_download_status
        WHERE status IN ('NO_DATA', 'FAILED', 'PARTIAL')
        GROUP BY status
        """,
        conn,
    )
    export_csv(delisted, "delisted_coverage_summary.csv", export_dir)
    checks.append(
        MarketDataCheck(
            "D06",
            "market_data_rows_present",
            "PASS" if int(scalar(conn, "SELECT COUNT(*) FROM market_prices_daily") or 0) > 0 else "WARN",
            str(int(scalar(conn, "SELECT COUNT(*) FROM market_prices_daily") or 0)),
            "> 0",
            "Market-data infrastructure is valid, but production acceptance needs downloaded prices",
            "Configure provider and run downloader",
        )
    )
    summary = pd.DataFrame([c.to_dict() for c in checks])
    export_csv(summary, "market_data_validation_summary.csv", export_dir)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Phase 5 market data.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out-dir", type=Path, default=EXPORT_DIR)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    conn = get_connection(args.db_path)
    try:
        summary = run_validation(conn, args.out_dir)
    finally:
        conn.close()
    return 1 if (summary["severity"] == "FAIL").any() else 0


if __name__ == "__main__":
    sys.exit(main())
