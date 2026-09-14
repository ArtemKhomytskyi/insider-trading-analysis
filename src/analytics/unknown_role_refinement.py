"""Export UNKNOWN role titles for iterative officer_title_map refinement."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pandas as pd

from src.utils.db import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_VALIDATION_DIR = PROJECT_ROOT / "data" / "exports" / "validation"


def analyze_unknown_roles_for_refinement(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    *,
    top_titles_limit: int = 300,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Export UNKNOWN MVP role titles grouped for taxonomy improvement.

    Returns (full_candidates_df, top_titles_df).
    """
    out = export_dir or DEFAULT_VALIDATION_DIR
    out.mkdir(parents=True, exist_ok=True)

    sql = """
    SELECT
        raw_role_title,
        raw_relationship,
        role_group,
        COUNT(*) AS row_count,
        COUNT(DISTINCT insider_cik) AS distinct_insiders,
        COUNT(DISTINCT ticker_clean) AS distinct_tickers,
        SUM(transaction_value) AS total_transaction_value,
        GROUP_CONCAT(DISTINCT insider_name) AS example_insider_names
    FROM clean_insider_transactions
    WHERE include_mvp_alpha = 1 AND normalized_role = 'UNKNOWN'
    GROUP BY raw_role_title, raw_relationship, role_group
    ORDER BY row_count DESC
    """
    candidates = pd.read_sql_query(sql, conn)
    if "example_insider_names" in candidates.columns:
        candidates["example_insider_names"] = candidates["example_insider_names"].apply(
            lambda x: "; ".join(str(x).split(",")[:5]) if pd.notna(x) else ""
        )

    top_titles = (
        candidates.groupby("raw_role_title", as_index=False)
        .agg(
            row_count=("row_count", "sum"),
            distinct_insiders=("distinct_insiders", "sum"),
            distinct_tickers=("distinct_tickers", "sum"),
            total_transaction_value=("total_transaction_value", "sum"),
        )
        .sort_values("row_count", ascending=False)
        .head(top_titles_limit)
    )

    candidates_path = out / "unknown_role_refinement_candidates.csv"
    top_path = out / "top_unknown_roles_for_mapping.csv"
    candidates.to_csv(candidates_path, index=False)
    top_titles.to_csv(top_path, index=False)
    logger.info(
        "Exported unknown role refinement: %s (%s rows), %s (%s titles)",
        candidates_path.name,
        len(candidates),
        top_path.name,
        len(top_titles),
    )
    return candidates, top_titles


def export_pre_source_transaction_date_sample(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    *,
    limit: int = 5000,
) -> pd.DataFrame:
    """Export sample rows where transaction year precedes source folder year."""
    out = export_dir or (PROJECT_ROOT / "data" / "exports" / "cleaning")
    out.mkdir(parents=True, exist_ok=True)

    sql = f"""
    SELECT
        accession_number,
        ticker_clean,
        document_type,
        transaction_date,
        filing_date,
        source_year,
        transaction_year,
        transaction_year_source_gap,
        transaction_code,
        transaction_value,
        filing_lag_days
    FROM clean_insider_transactions
    WHERE is_pre_source_transaction_date = 1
    ORDER BY transaction_year_source_gap DESC, transaction_value DESC
    LIMIT {int(limit)}
    """
    df = pd.read_sql_query(sql, conn)
    path = out / "pre_source_transaction_date_sample.csv"
    df.to_csv(path, index=False)
    logger.info("Exported %s (%s rows)", path.name, len(df))
    return df
