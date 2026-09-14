"""
Phase 2: exploratory profiling of raw SEC insider SQLite tables.

Read-only audit — no cleaning, filtering, or scoring.
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from src.ingestion.sec_ingestion import RAW_TABLES
from src.utils.db import PROJECT_ROOT, get_connection, get_database_path
from src.utils.sec_parsing import parse_sec_date, parse_sec_number
from src.utils.sec_taxonomy import load_transaction_code_map
from src.utils.role_taxonomy import classify_role, load_role_taxonomy, parse_relationship_flags
from src.utils.security_taxonomy import classify_security_title, load_security_title_map

logger = logging.getLogger(__name__)

EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "raw_data_audit"
CHART_DIR = EXPORT_DIR / "charts"

FOCUS_TRANS_CODES: tuple[str, ...] = ("P", "S", "A", "M", "F", "D", "G", "C", "X")

MISSINGNESS_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("raw_submission", "ISSUERTRADINGSYMBOL", "ticker"),
    ("raw_reporting_owner", "RPTOWNERNAME", "rptownername"),
    ("raw_reporting_owner", "RPTOWNERCIK", "rptownercik"),
    ("raw_nonderiv_trans", "TRANS_DATE", "transactiondate"),
    ("raw_nonderiv_trans", "TRANS_CODE", "transactioncode"),
    ("raw_nonderiv_trans", "TRANS_SHARES", "transactionshares"),
    ("raw_nonderiv_trans", "TRANS_PRICEPERSHARE", "transactionpricepershare"),
    ("raw_nonderiv_trans", "SECURITY_TITLE", "securitytitle"),
)

DATE_COLUMNS: dict[str, str] = {
    "raw_submission": "FILING_DATE",
    "raw_nonderiv_trans": "TRANS_DATE",
    "raw_deriv_trans": "TRANS_DATE",
}


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def ensure_export_dirs(export_dir: Path | None = None) -> Path:
    root = export_dir or EXPORT_DIR
    (root / "charts").mkdir(parents=True, exist_ok=True)
    return root


def export_csv(df: pd.DataFrame, filename: str, export_dir: Path) -> Path:
    path = export_dir / filename
    df.to_csv(path, index=False)
    logger.info("exported %s (%s rows)", path.name, len(df))
    return path


def save_chart(fig: plt.Figure, filename: str, export_dir: Path) -> Path:
    chart_dir = export_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    path = chart_dir / filename
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    logger.info("saved chart %s", path.name)
    return path


def read_sql(conn: sqlite3.Connection, query: str) -> pd.DataFrame:
    return pd.read_sql_query(query, conn)


def sec_date_expr(column: str) -> str:
    """SQLite expression to normalize SEC YYYYMMDD or YYYY-MM-DD text dates."""
    col = f"TRIM({column})"
    return f"""
        CASE
            WHEN {column} IS NULL OR {col} = '' THEN NULL
            WHEN LENGTH({col}) = 8 AND {col} GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
                THEN SUBSTR({col}, 1, 4) || '-' || SUBSTR({col}, 5, 2) || '-' || SUBSTR({col}, 7, 2)
            WHEN {col} GLOB '????-??-??' THEN {col}
            ELSE NULL
        END
    """


def is_blank_expr(column: str) -> str:
    return f"({column} IS NULL OR TRIM({column}) = '')"


def numeric_expr(column: str) -> str:
    blank = is_blank_expr(column)
    return f"""
        CASE
            WHEN {blank} THEN NULL
            WHEN TRIM({column}) GLOB '*[^0-9.+-]*' THEN NULL
            ELSE CAST(TRIM({column}) AS REAL)
        END
    """


def get_db_size_mb(db_path: Path | None = None) -> float:
    path = db_path or get_database_path()
    if not path.exists():
        return 0.0
    return path.stat().st_size / (1024 * 1024)


def list_all_tables(conn: sqlite3.Connection) -> pd.DataFrame:
    return read_sql(
        conn,
        """
        SELECT name AS table_name, type
        FROM sqlite_master
        WHERE type IN ('table', 'view')
        ORDER BY name
        """,
    )


def get_table_counts(conn: sqlite3.Connection) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    tables = read_sql(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    )
    for table_name in tables["name"]:
        count = read_sql(conn, f"SELECT COUNT(*) AS row_count FROM {table_name}").iloc[0, 0]
        rows.append({"table_name": table_name, "row_count": int(count)})
    return pd.DataFrame(rows)


def get_database_overview(conn: sqlite3.Connection, db_path: Path | None = None) -> pd.DataFrame:
    """Section 1: table overview with row counts and date coverage."""
    path = db_path or get_database_path()
    overview_rows: list[dict[str, Any]] = []

    for table in RAW_TABLES:
        row = {"table_name": table, "row_count": 0, "min_date": None, "max_date": None}
        try:
            row["row_count"] = int(
                read_sql(conn, f"SELECT COUNT(*) AS n FROM {table}").iloc[0, 0]
            )
        except Exception as exc:
            logger.warning("could not count %s: %s", table, exc)
            overview_rows.append(row)
            continue

        date_col = DATE_COLUMNS.get(table)
        if date_col:
            date_sql = sec_date_expr(date_col)
            dates = read_sql(
                conn,
                f"""
                SELECT MIN({date_sql}) AS min_date, MAX({date_sql}) AS max_date
                FROM {table}
                WHERE {date_sql} IS NOT NULL
                """,
            )
            row["min_date"] = dates.iloc[0]["min_date"]
            row["max_date"] = dates.iloc[0]["max_date"]

        overview_rows.append(row)

    df = pd.DataFrame(overview_rows)
    df["db_size_mb"] = round(get_db_size_mb(path), 2)

    year_cov = read_sql(
        conn,
        """
        SELECT MIN(source_year) AS min_source_year,
               MAX(source_year) AS max_source_year,
               COUNT(DISTINCT source_year) AS distinct_years
        FROM raw_submission
        WHERE source_year IS NOT NULL
        """,
    )
    for col in year_cov.columns:
        df[col] = year_cov.iloc[0][col]

    return df


def analyze_filing_types(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    """Section 2: document type and filing cadence."""
    filing_sql = sec_date_expr("FILING_DATE")
    doc_dist = read_sql(
        conn,
        """
        SELECT
            COALESCE(NULLIF(TRIM(DOCUMENT_TYPE), ''), '(blank)') AS document_type,
            COUNT(*) AS row_count,
            ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM raw_submission), 4) AS pct
        FROM raw_submission
        GROUP BY 1
        ORDER BY row_count DESC
        """,
    )

    filings_by_year = read_sql(
        conn,
        f"""
        SELECT
            CAST(strftime('%Y', {filing_sql}) AS INTEGER) AS filing_year,
            COUNT(*) AS filing_count
        FROM raw_submission
        WHERE {filing_sql} IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """,
    )

    filings_by_quarter = read_sql(
        conn,
        """
        SELECT source_year, source_quarter, COUNT(*) AS filing_count
        FROM raw_submission
        WHERE source_year IS NOT NULL AND source_quarter IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
    )

    export_parts = [
        doc_dist.assign(metric_type="document_type"),
        filings_by_year.assign(metric_type="filings_by_year"),
        filings_by_quarter.assign(metric_type="filings_by_quarter"),
    ]
    filing_export = pd.concat(export_parts, ignore_index=True, sort=False)

    return {
        "document_type_distribution": doc_dist,
        "filings_by_year": filings_by_year,
        "filings_by_quarter": filings_by_quarter,
        "filing_type_distribution": filing_export,
    }


def _trans_union_code_query(extra_where: str = "") -> str:
    return f"""
        SELECT TRANS_CODE, source_year, 'nonderiv' AS source_table
        FROM raw_nonderiv_trans
        WHERE NOT {is_blank_expr("TRANS_CODE")} {extra_where}
        UNION ALL
        SELECT TRANS_CODE, source_year, 'deriv' AS source_table
        FROM raw_deriv_trans
        WHERE NOT {is_blank_expr("TRANS_CODE")} {extra_where}
    """


def analyze_transaction_codes(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    """Section 3: transaction code frequencies and trends."""
    union = _trans_union_code_query()
    code_dist = read_sql(
        conn,
        f"""
        SELECT
            TRIM(TRANS_CODE) AS transaction_code,
            COUNT(*) AS row_count,
            ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM ({union})), 4) AS pct
        FROM ({union})
        GROUP BY 1
        ORDER BY row_count DESC
        """,
    )

    yearly = read_sql(
        conn,
        f"""
        SELECT
            source_year,
            TRIM(TRANS_CODE) AS transaction_code,
            COUNT(*) AS row_count
        FROM ({union})
        WHERE source_year IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 1, row_count DESC
        """,
    )

    focus = code_dist[code_dist["transaction_code"].isin(FOCUS_TRANS_CODES)].copy()
    yearly_focus = yearly[yearly["transaction_code"].isin(FOCUS_TRANS_CODES)].copy()

    export_df = code_dist.copy()
    export_df["is_focus_code"] = export_df["transaction_code"].isin(FOCUS_TRANS_CODES)

    return {
        "transaction_code_distribution": export_df,
        "transaction_code_yearly": yearly,
        "focus_codes": focus,
        "focus_codes_yearly": yearly_focus,
    }


def _nonderiv_trans_code_counts(conn: sqlite3.Connection) -> pd.DataFrame:
    return read_sql(
        conn,
        f"""
        SELECT
            TRIM(TRANS_CODE) AS transaction_code,
            COUNT(*) AS row_count
        FROM raw_nonderiv_trans
        WHERE NOT {is_blank_expr("TRANS_CODE")}
        GROUP BY 1
        ORDER BY row_count DESC
        """,
    )


def analyze_transaction_code_taxonomy(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    map_path: Path | None = None,
) -> dict[str, Any]:
    """
    Compare raw_nonderiv_trans codes against config-driven taxonomy.

    Exports coverage report and category/signal-quality charts.
    """
    out = ensure_export_dirs(export_dir)
    taxonomy_path = map_path.resolve() if map_path else None
    taxonomy = load_transaction_code_map(
        str(taxonomy_path) if taxonomy_path else None
    )
    code_counts = _nonderiv_trans_code_counts(conn)

    total_rows = int(code_counts["row_count"].sum()) if not code_counts.empty else 0
    taxonomy = taxonomy.copy()
    taxonomy["transaction_code"] = taxonomy["transaction_code"].str.upper()

    coverage = code_counts.merge(
        taxonomy,
        on="transaction_code",
        how="left",
        indicator=True,
    )
    coverage["pct_total"] = (
        (100.0 * coverage["row_count"] / total_rows).round(4) if total_rows else 0.0
    )
    coverage["mapped"] = coverage["_merge"] == "both"
    coverage = coverage.drop(columns=["_merge"])

    mapped_rows = int(coverage.loc[coverage["mapped"], "row_count"].sum())
    unmapped_rows = total_rows - mapped_rows
    coverage_pct = round(100.0 * mapped_rows / total_rows, 4) if total_rows else 0.0

    export_cols = [
        "transaction_code",
        "row_count",
        "pct_total",
        "mapped",
        "category",
        "signal_quality",
        "include_clean_mvp",
        "meaning",
        "signal_direction",
    ]
    export_df = coverage[[c for c in export_cols if c in coverage.columns]].copy()
    export_csv(export_df, "transaction_code_taxonomy_coverage.csv", out)

    unmapped = export_df.loc[~export_df["mapped"]].copy()
    mapped = export_df.loc[export_df["mapped"]].copy()

    summary = {
        "total_rows": total_rows,
        "distinct_codes": int(len(code_counts)),
        "mapped_codes": int(mapped["transaction_code"].nunique()),
        "unmapped_codes": int(unmapped["transaction_code"].nunique()),
        "rows_mapped": mapped_rows,
        "rows_unmapped": unmapped_rows,
        "coverage_pct": coverage_pct,
    }

    plot_transaction_code_taxonomy_charts(mapped, out)

    logger.info(
        "taxonomy coverage: %.2f%% (%s / %s rows), %s unmapped codes",
        coverage_pct,
        f"{mapped_rows:,}",
        f"{total_rows:,}",
        summary["unmapped_codes"],
    )

    return {
        "taxonomy": taxonomy,
        "code_counts": code_counts,
        "coverage": export_df,
        "mapped": mapped,
        "unmapped": unmapped,
        "summary": summary,
    }


def plot_transaction_code_taxonomy_charts(
    mapped_df: pd.DataFrame,
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    if mapped_df.empty:
        return paths

    by_category = (
        mapped_df.groupby("category", dropna=False)["row_count"]
        .sum()
        .sort_values(ascending=False)
    )
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(by_category.index.astype(str), by_category.values, color="steelblue")
    ax.set_title("Transaction Rows by Taxonomy Category")
    ax.set_xlabel("Category")
    ax.set_ylabel("Row count")
    plt.xticks(rotation=30, ha="right")
    fig.tight_layout()
    paths.append(save_chart(fig, "transaction_code_categories.png", export_dir))

    by_quality = (
        mapped_df.groupby("signal_quality", dropna=False)["row_count"]
        .sum()
        .sort_values(ascending=False)
    )
    quality_order = ["HIGH", "MEDIUM", "LOW", "VERY_LOW"]
    by_quality = by_quality.reindex(
        [q for q in quality_order if q in by_quality.index]
        + [q for q in by_quality.index if q not in quality_order]
    )
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(by_quality.index.astype(str), by_quality.values, color="coral")
    ax.set_title("Transaction Rows by Signal Quality")
    ax.set_xlabel("Signal quality")
    ax.set_ylabel("Row count")
    fig.tight_layout()
    paths.append(save_chart(fig, "signal_quality_distribution.png", export_dir))
    return paths


def _nonderiv_security_title_counts(conn: sqlite3.Connection) -> pd.DataFrame:
    return read_sql(
        conn,
        f"""
        SELECT
            TRIM(SECURITY_TITLE) AS security_title,
            COUNT(*) AS row_count
        FROM raw_nonderiv_trans
        WHERE NOT {is_blank_expr("SECURITY_TITLE")}
        GROUP BY 1
        ORDER BY row_count DESC
        """,
    )


def analyze_security_type_taxonomy(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    map_path: Path | None = None,
) -> dict[str, Any]:
    """
    Classify raw_nonderiv_trans SECURITY_TITLE values using config-driven taxonomy.
    """
    out = ensure_export_dirs(export_dir)
    mapping_path = map_path.resolve() if map_path else None
    mapping_df = load_security_title_map(
        str(mapping_path) if mapping_path else None
    )

    title_counts = _nonderiv_security_title_counts(conn)
    total_rows = int(title_counts["row_count"].sum()) if not title_counts.empty else 0
    distinct_titles = len(title_counts)

    classified_rows: list[dict[str, Any]] = []
    for record in title_counts.to_dict(orient="records"):
        raw_title = record["security_title"]
        meta = classify_security_title(raw_title, mapping_df=mapping_df)
        classified_rows.append(
            {
                "security_title": raw_title,
                "normalized_title": meta["normalized_title"],
                "row_count": record["row_count"],
                "normalized_security_type": meta["normalized_security_type"],
                "include_clean_mvp": meta["include_clean_mvp"],
                "confidence": meta["confidence"],
                "requires_review": meta["requires_review"],
                "matched_pattern": meta["matched_pattern"],
            }
        )

    coverage = pd.DataFrame(classified_rows)
    coverage["pct_total"] = (
        (100.0 * coverage["row_count"] / total_rows).round(4) if total_rows else 0.0
    )
    coverage = coverage.sort_values("row_count", ascending=False)

    mapped_mask = ~coverage["normalized_security_type"].isin(["UNKNOWN", "OTHER"])
    mapped_titles = int(coverage.loc[mapped_mask, "security_title"].nunique())
    unmapped_titles = distinct_titles - mapped_titles
    mapped_rows = int(coverage.loc[mapped_mask, "row_count"].sum())
    mvp_rows = int(coverage.loc[coverage["include_clean_mvp"], "row_count"].sum())
    review_rows = int(coverage.loc[coverage["requires_review"], "row_count"].sum())

    export_csv(coverage, "security_type_taxonomy_coverage.csv", out)

    summary = (
        coverage.groupby(
            [
                "normalized_security_type",
                "include_clean_mvp",
                "requires_review",
            ],
            dropna=False,
        )
        .agg(
            row_count=("row_count", "sum"),
            distinct_titles=("security_title", "nunique"),
        )
        .reset_index()
    )
    summary["pct_total"] = (
        (100.0 * summary["row_count"] / total_rows).round(4) if total_rows else 0.0
    )
    summary = summary.sort_values("row_count", ascending=False)
    export_csv(summary, "security_type_taxonomy_summary.csv", out)

    review_export = coverage[
        (coverage["normalized_security_type"].isin(["UNKNOWN", "OTHER"]))
        | (coverage["requires_review"])
        | (coverage["confidence"].str.upper() == "LOW")
    ].copy()
    export_csv(review_export, "security_type_unmapped_or_review.csv", out)

    plot_security_type_taxonomy_charts(summary, mvp_rows, total_rows - mvp_rows, out)

    summary_stats = {
        "total_rows": total_rows,
        "distinct_security_titles": distinct_titles,
        "mapped_titles": mapped_titles,
        "unmapped_titles": unmapped_titles,
        "mapped_row_coverage_pct": round(100.0 * mapped_rows / total_rows, 4)
        if total_rows
        else 0.0,
        "include_clean_mvp_rows": mvp_rows,
        "include_clean_mvp_pct": round(100.0 * mvp_rows / total_rows, 4)
        if total_rows
        else 0.0,
        "requires_review_rows": review_rows,
    }

    logger.info(
        "security taxonomy: %s mapped titles, MVP rows %.2f%% (%s / %s)",
        f"{mapped_titles}/{distinct_titles}",
        summary_stats["include_clean_mvp_pct"],
        f"{mvp_rows:,}",
        f"{total_rows:,}",
    )

    return {
        "coverage": coverage,
        "summary": summary,
        "review": review_export,
        "summary_stats": summary_stats,
        "mapping": mapping_df,
    }


def plot_security_type_taxonomy_charts(
    summary_df: pd.DataFrame,
    mvp_rows: int,
    excluded_rows: int,
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    if not summary_df.empty:
        by_type = summary_df.groupby("normalized_security_type")["row_count"].sum()
        by_type = by_type.sort_values(ascending=False)
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.bar(by_type.index.astype(str), by_type.values, color="steelblue")
        ax.set_title("Non-Derivative Rows by Security Type Taxonomy")
        ax.set_xlabel("Normalized security type")
        ax.set_ylabel("Row count")
        plt.xticks(rotation=35, ha="right")
        fig.tight_layout()
        paths.append(save_chart(fig, "security_type_taxonomy_distribution.png", export_dir))

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(
        ["include_clean_mvp", "excluded_from_mvp"],
        [mvp_rows, excluded_rows],
        color=["seagreen", "lightgray"],
    )
    ax.set_title("MVP Common Equity vs Excluded Security Types")
    ax.set_ylabel("Row count")
    fig.tight_layout()
    paths.append(save_chart(fig, "security_type_mvp_vs_excluded.png", export_dir))
    return paths


def _reporting_owner_title_counts(conn: sqlite3.Connection) -> pd.DataFrame:
    return read_sql(
        conn,
        f"""
        SELECT
            TRIM(RPTOWNER_TITLE) AS raw_role_title,
            COUNT(*) AS row_count
        FROM raw_reporting_owner
        WHERE NOT {is_blank_expr("RPTOWNER_TITLE")}
        GROUP BY 1
        ORDER BY row_count DESC
        """,
    )


def _reporting_owner_relationship_counts(conn: sqlite3.Connection) -> pd.DataFrame:
    return read_sql(
        conn,
        f"""
        SELECT
            TRIM(RPTOWNER_RELATIONSHIP) AS raw_relationship,
            COUNT(*) AS row_count
        FROM raw_reporting_owner
        WHERE NOT {is_blank_expr("RPTOWNER_RELATIONSHIP")}
        GROUP BY 1
        ORDER BY row_count DESC
        """,
    )


def analyze_role_taxonomy(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    map_path: Path | None = None,
) -> dict[str, Any]:
    """Classify insider roles from RPTOWNER_TITLE and RPTOWNER_RELATIONSHIP."""
    out = ensure_export_dirs(export_dir)
    mapping_path = map_path.resolve() if map_path else None
    mapping_df = load_role_taxonomy(str(mapping_path) if mapping_path else None)

    title_counts = _reporting_owner_title_counts(conn)
    total_rows = int(
        read_sql(conn, "SELECT COUNT(*) AS n FROM raw_reporting_owner").iloc[0, 0]
    )
    title_row_total = int(title_counts["row_count"].sum()) if not title_counts.empty else 0

    classified_rows: list[dict[str, Any]] = []
    for record in title_counts.to_dict(orient="records"):
        raw_title = record["raw_role_title"]
        meta = classify_role(raw_title, mapping_df=mapping_df)
        classified_rows.append(
            {
                "raw_role_title": raw_title,
                "normalized_role": meta["normalized_role"],
                "role_group": meta["role_group"],
                "role_weight": meta["role_weight"],
                "row_count": record["row_count"],
                "is_executive": meta["is_executive"],
                "is_board_member": meta["is_board_member"],
                "is_large_owner": meta["is_large_owner"],
                "include_clean_mvp": meta["include_clean_mvp"],
                "requires_review": meta["requires_review"],
                "confidence": meta["confidence"],
                "matched_pattern": meta["matched_pattern"],
            }
        )

    coverage = pd.DataFrame(classified_rows)
    coverage["pct_total"] = (
        (100.0 * coverage["row_count"] / title_row_total).round(4)
        if title_row_total
        else 0.0
    )
    coverage = coverage.sort_values("row_count", ascending=False)
    export_csv(coverage, "role_taxonomy_coverage.csv", out)

    summary = (
        coverage.groupby(["normalized_role", "role_group"], dropna=False)
        .agg(
            row_count=("row_count", "sum"),
            distinct_titles=("raw_role_title", "nunique"),
            role_weight=("role_weight", "first"),
            is_executive=("is_executive", "max"),
            is_board_member=("is_board_member", "max"),
            is_large_owner=("is_large_owner", "max"),
            include_clean_mvp=("include_clean_mvp", "max"),
            requires_review=("requires_review", "max"),
        )
        .reset_index()
    )
    summary["pct_total"] = (
        (100.0 * summary["row_count"] / title_row_total).round(4)
        if title_row_total
        else 0.0
    )
    summary = summary.sort_values("row_count", ascending=False)
    export_csv(summary, "role_taxonomy_summary.csv", out)

    review_export = coverage[
        (coverage["normalized_role"] == "UNKNOWN")
        | (coverage["requires_review"])
        | (coverage["confidence"].str.upper() == "LOW")
    ].copy()
    export_csv(review_export, "role_taxonomy_unmapped_or_review.csv", out)

    rel_counts = _reporting_owner_relationship_counts(conn)
    rel_rows: list[dict[str, Any]] = []
    for record in rel_counts.to_dict(orient="records"):
        flags = parse_relationship_flags(record["raw_relationship"])
        rel_rows.append(
            {
                "raw_relationship": record["raw_relationship"],
                "row_count": record["row_count"],
                **{k: flags[k] for k in flags if k.startswith("is_")},
            }
        )
    relationship_summary = pd.DataFrame(rel_rows)

    executive_rows = int(coverage.loc[coverage["is_executive"], "row_count"].sum())
    board_rows = int(coverage.loc[coverage["is_board_member"], "row_count"].sum())
    owner_rows = int(coverage.loc[coverage["is_large_owner"], "row_count"].sum())
    mvp_rows = int(coverage.loc[coverage["include_clean_mvp"], "row_count"].sum())
    mapped_titles = int(
        coverage.loc[coverage["normalized_role"] != "UNKNOWN", "raw_role_title"].nunique()
    )

    plot_role_taxonomy_charts(coverage, executive_rows, title_row_total - executive_rows, out)

    summary_stats: dict[str, Any] = {
        "total_reporting_owner_rows": total_rows,
        "distinct_role_titles": len(title_counts),
        "mapped_role_titles": mapped_titles,
        "unmapped_role_titles": len(title_counts) - mapped_titles,
        "title_row_coverage_pct": round(100.0 * title_row_total / total_rows, 4)
        if total_rows
        else 0.0,
        "executive_rows": executive_rows,
        "board_member_rows": board_rows,
        "large_owner_rows": owner_rows,
        "include_clean_mvp_rows": mvp_rows,
    }

    if not relationship_summary.empty:
        for flag_col in ("is_officer", "is_director", "is_ten_percent_owner", "is_other"):
            if flag_col in relationship_summary.columns:
                summary_stats[f"relationship_{flag_col}_rows"] = int(
                    relationship_summary.loc[
                        relationship_summary[flag_col], "row_count"
                    ].sum()
                )

    logger.info(
        "role taxonomy: %s executives, %s board, MVP title-rows %.2f%%",
        f"{executive_rows:,}",
        f"{board_rows:,}",
        round(100.0 * mvp_rows / title_row_total, 2) if title_row_total else 0,
    )

    return {
        "coverage": coverage,
        "summary": summary,
        "review": review_export,
        "relationship_summary": relationship_summary,
        "summary_stats": summary_stats,
        "mapping": mapping_df,
    }


def plot_role_taxonomy_charts(
    coverage: pd.DataFrame,
    executive_rows: int,
    non_executive_rows: int,
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    if not coverage.empty:
        by_group = coverage.groupby("role_group")["row_count"].sum().sort_values(ascending=False)
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.bar(by_group.index.astype(str), by_group.values, color="steelblue")
        ax.set_title("Reporting Owner Rows by Role Group")
        ax.set_xlabel("Role group")
        ax.set_ylabel("Row count")
        plt.xticks(rotation=25, ha="right")
        fig.tight_layout()
        paths.append(save_chart(fig, "role_group_distribution.png", export_dir))

        by_role = (
            coverage.groupby("normalized_role")["row_count"]
            .sum()
            .sort_values(ascending=False)
            .head(15)
        )
        fig, ax = plt.subplots(figsize=(9, 5))
        ax.barh(by_role.index.astype(str), by_role.values, color="coral")
        ax.invert_yaxis()
        ax.set_title("Top Normalized Roles by Row Count")
        ax.set_xlabel("Row count")
        fig.tight_layout()
        paths.append(save_chart(fig, "role_weight_distribution.png", export_dir))

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(
        ["executive", "non_executive"],
        [executive_rows, non_executive_rows],
        color=["seagreen", "lightgray"],
    )
    ax.set_title("Executive vs Non-Executive (Title-Based)")
    ax.set_ylabel("Row count")
    fig.tight_layout()
    paths.append(save_chart(fig, "executive_vs_nonexecutive.png", export_dir))
    return paths


def analyze_security_titles(
    conn: sqlite3.Connection,
    top_n: int = 100,
) -> dict[str, pd.DataFrame]:
    """Section 4: security title value distribution."""
    union = """
        SELECT TRIM(SECURITY_TITLE) AS security_title, 'nonderiv' AS source_table
        FROM raw_nonderiv_trans
        WHERE NOT {blank}
        UNION ALL
        SELECT TRIM(SECURITY_TITLE) AS security_title, 'deriv' AS source_table
        FROM raw_deriv_trans
        WHERE NOT {blank}
    """.format(blank=is_blank_expr("SECURITY_TITLE"))

    top = read_sql(
        conn,
        f"""
        SELECT
            security_title,
            COUNT(*) AS row_count,
            ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM ({union})), 4) AS pct
        FROM ({union})
        GROUP BY 1
        ORDER BY row_count DESC
        LIMIT {top_n}
        """,
    )

    upper = top.copy()
    upper["security_title_upper"] = upper["security_title"].str.upper()
    common_stock = upper[
        upper["security_title_upper"].str.contains("COMMON", na=False)
    ]
    options = upper[
        upper["security_title_upper"].str.contains(
            "OPTION|WARRANT|RIGHT|SWAP|DERIV", na=False, regex=True
        )
    ]

    preview = top.head(30).copy()
    preview["normalized_preview"] = (
        preview["security_title"]
        .str.upper()
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )

    return {
        "security_type_distribution": top,
        "common_stock_candidates": common_stock,
        "derivative_candidates": options,
        "normalized_preview": preview,
    }


def analyze_ownership(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    """Section 5: direct/indirect ownership fields."""
    union = f"""
        SELECT TRIM(DIRECT_INDIRECT_OWNERSHIP) AS ownership_type,
               TRIM(NATURE_OF_OWNERSHIP) AS nature_of_ownership,
               'nonderiv' AS source_table
        FROM raw_nonderiv_trans
        UNION ALL
        SELECT TRIM(DIRECT_INDIRECT_OWNERSHIP),
               TRIM(NATURE_OF_OWNERSHIP),
               'deriv'
        FROM raw_deriv_trans
    """

    direct = read_sql(
        conn,
        f"""
        SELECT
            COALESCE(NULLIF(ownership_type, ''), '(blank)') AS ownership_type,
            COUNT(*) AS row_count,
            ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM ({union})), 4) AS pct
        FROM ({union})
        GROUP BY 1
        ORDER BY row_count DESC
        """,
    )

    nature = read_sql(
        conn,
        f"""
        SELECT
            COALESCE(NULLIF(nature_of_ownership, ''), '(blank)') AS nature_of_ownership,
            COUNT(*) AS row_count
        FROM ({union})
        WHERE nature_of_ownership IS NOT NULL AND nature_of_ownership != ''
        GROUP BY 1
        ORDER BY row_count DESC
        LIMIT 50
        """,
    )

    export_df = direct.copy()
    export_df["field"] = "direct_indirect_ownership"
    nature_export = nature.copy()
    nature_export["field"] = "nature_of_ownership"

    return {
        "ownership_distribution": pd.concat([export_df, nature_export], ignore_index=True),
        "direct_indirect": direct,
        "nature_of_ownership": nature,
    }


def analyze_officer_titles(conn: sqlite3.Connection, top_n: int = 100) -> dict[str, pd.DataFrame]:
    """Section 6: raw officer title frequency."""
    titles = read_sql(
        conn,
        f"""
        SELECT
            TRIM(RPTOWNER_TITLE) AS officer_title,
            COUNT(*) AS row_count,
            ROUND(100.0 * COUNT(*) / (
                SELECT COUNT(*) FROM raw_reporting_owner
                WHERE NOT {is_blank_expr("RPTOWNER_TITLE")}
            ), 4) AS pct
        FROM raw_reporting_owner
        WHERE NOT {is_blank_expr("RPTOWNER_TITLE")}
        GROUP BY 1
        ORDER BY row_count DESC
        LIMIT {top_n}
        """,
    )

    upper = titles.copy()
    upper["title_upper"] = upper["officer_title"].str.upper()
    ceo_like = upper[
        upper["title_upper"].str.contains(
            r"CEO|CHIEF EXECUTIVE|PRESIDENT|CHAIRMAN|CHAIR",
            na=False,
            regex=True,
        )
    ]

    normalization_groups = read_sql(
        conn,
        f"""
        SELECT
            UPPER(TRIM(RPTOWNER_TITLE)) AS title_normalized,
            COUNT(*) AS row_count,
            MIN(TRIM(RPTOWNER_TITLE)) AS example_raw_title
        FROM raw_reporting_owner
        WHERE NOT {is_blank_expr("RPTOWNER_TITLE")}
        GROUP BY 1
        HAVING COUNT(*) >= 10
        ORDER BY row_count DESC
        LIMIT 50
        """,
    )

    return {
        "officer_title_distribution": titles,
        "ceo_like_titles": ceo_like,
        "normalization_groups": normalization_groups,
    }


def analyze_missing_values(conn: sqlite3.Connection) -> pd.DataFrame:
    """Section 7: null rates for key fields."""
    rows: list[dict[str, Any]] = []
    for table, column, label in MISSINGNESS_FIELDS:
        total = int(read_sql(conn, f"SELECT COUNT(*) AS n FROM {table}").iloc[0, 0])
        nulls = int(
            read_sql(
                conn,
                f"SELECT SUM(CASE WHEN {is_blank_expr(column)} THEN 1 ELSE 0 END) AS n FROM {table}",
            ).iloc[0, 0]
        )
        completeness = round(100.0 * (1 - nulls / total), 4) if total else 0.0
        rows.append(
            {
                "table_name": table,
                "column_name": column,
                "field_label": label,
                "total_rows": total,
                "null_count": nulls,
                "null_pct": round(100.0 * nulls / total, 4) if total else 0.0,
                "completeness_score": completeness,
            }
        )
    return pd.DataFrame(rows)


def plot_missingness_heatmap(missing_df: pd.DataFrame, export_dir: Path) -> Path:
    pivot = missing_df.pivot_table(
        index="field_label",
        values="null_pct",
        aggfunc="first",
    )
    fig, ax = plt.subplots(figsize=(6, max(4, len(pivot) * 0.35)))
    im = ax.imshow(pivot.values.reshape(-1, 1), aspect="auto", cmap="YlOrRd", vmin=0, vmax=100)
    ax.set_yticks(range(len(pivot)))
    ax.set_yticklabels(pivot.index)
    ax.set_xticks([0])
    ax.set_xticklabels(["null %"])
    ax.set_title("Missing Value Rate by Key Field")
    fig.colorbar(im, ax=ax, label="null %")
    fig.tight_layout()
    return save_chart(fig, "missingness_heatmap.png", export_dir)


def _sample_numeric_trans(
    conn: sqlite3.Connection,
    sample_size: int = 100_000,
) -> pd.DataFrame:
    shares = numeric_expr("TRANS_SHARES")
    price = numeric_expr("TRANS_PRICEPERSHARE")
    return read_sql(
        conn,
        f"""
        SELECT
            {shares} AS transaction_shares,
            {price} AS transaction_price_per_share,
            ({shares} * {price}) AS implied_transaction_value
        FROM raw_nonderiv_trans
        WHERE {shares} IS NOT NULL
          AND {price} IS NOT NULL
          AND {shares} > 0
        ORDER BY RANDOM()
        LIMIT {sample_size}
        """,
    )


def analyze_distributions(
    conn: sqlite3.Connection,
    sample_size: int = 100_000,
) -> dict[str, Any]:
    """Section 8: numeric distributions and filing lag preview."""
    sample = _sample_numeric_trans(conn, sample_size)
    summary_rows: list[dict[str, Any]] = []

    for col in ["transaction_shares", "transaction_price_per_share", "implied_transaction_value"]:
        series = sample[col].dropna()
        if series.empty:
            continue
        summary_rows.append(
            {
                "metric": col,
                "count": int(series.count()),
                "mean": float(series.mean()),
                "median": float(series.median()),
                "p01": float(series.quantile(0.01)),
                "p25": float(series.quantile(0.25)),
                "p75": float(series.quantile(0.75)),
                "p99": float(series.quantile(0.99)),
                "max": float(series.max()),
                "min": float(series.min()),
            }
        )

    lag_sample = read_sql(
        conn,
        f"""
        SELECT
            julianday({sec_date_expr("s.FILING_DATE")}) -
            julianday({sec_date_expr("t.TRANS_DATE")}) AS filing_lag_days
        FROM raw_nonderiv_trans t
        INNER JOIN raw_submission s ON t.ACCESSION_NUMBER = s.ACCESSION_NUMBER
        WHERE {sec_date_expr("t.TRANS_DATE")} IS NOT NULL
          AND {sec_date_expr("s.FILING_DATE")} IS NOT NULL
        ORDER BY RANDOM()
        LIMIT {min(sample_size, 50000)}
        """,
    )

    if not lag_sample.empty:
        lag = lag_sample["filing_lag_days"].dropna()
        summary_rows.append(
            {
                "metric": "filing_lag_days",
                "count": int(lag.count()),
                "mean": float(lag.mean()),
                "median": float(lag.median()),
                "p01": float(lag.quantile(0.01)),
                "p25": float(lag.quantile(0.25)),
                "p75": float(lag.quantile(0.75)),
                "p99": float(lag.quantile(0.99)),
                "max": float(lag.max()),
                "min": float(lag.min()),
            }
        )

    return {
        "distribution_summary": pd.DataFrame(summary_rows),
        "numeric_sample": sample,
        "lag_sample": lag_sample,
    }


def plot_distribution_histograms(
    sample: pd.DataFrame,
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    metrics = [
        ("transaction_shares", "Transaction Shares"),
        ("transaction_price_per_share", "Price Per Share"),
        ("implied_transaction_value", "Implied Transaction Value"),
    ]
    for col, title in metrics:
        if col not in sample.columns or sample[col].dropna().empty:
            continue
        data = sample[col].dropna()
        for log_scale, suffix in [(False, "linear"), (True, "log")]:
            fig, ax = plt.subplots(figsize=(8, 4))
            plot_data = data[data > 0] if log_scale else data
            ax.hist(plot_data, bins=50, color="steelblue", edgecolor="white")
            if log_scale:
                ax.set_xscale("log")
            ax.set_title(f"{title} ({suffix} scale)")
            ax.set_xlabel(col)
            ax.set_ylabel("frequency")
            fig.tight_layout()
            paths.append(save_chart(fig, f"dist_{col}_{suffix}.png", export_dir))
    return paths


FILING_LAG_QUERY = """
    SELECT
        t.ACCESSION_NUMBER,
        t.NONDERIV_TRANS_SK,
        t.TRANS_DATE,
        s.FILING_DATE,
        s.DOCUMENT_TYPE,
        t.TRANS_CODE
    FROM raw_nonderiv_trans t
    INNER JOIN raw_submission s ON t.ACCESSION_NUMBER = s.ACCESSION_NUMBER
"""


@dataclass
class FilingLagAccumulator:
    row_count: int = 0
    valid_lag_count: int = 0
    invalid_date_count: int = 0
    sum_lag: float = 0.0
    min_lag: float | None = None
    max_lag: float | None = None
    same_day: int = 0
    lag_1_2_days: int = 0
    over_2_days: int = 0
    negative_lag: int = 0
    lag_reservoir: list[float] = field(default_factory=list)
    lag_reservoir_limit: int = 200_000
    yearly_lag_sums: dict[int, float] = field(default_factory=dict)
    yearly_lag_counts: dict[int, int] = field(default_factory=dict)
    yearly_lag_samples: dict[int, list[float]] = field(default_factory=dict)
    bucket_counts: dict[str, int] = field(default_factory=dict)
    bad_rows: list[dict[str, Any]] = field(default_factory=list)
    bad_row_limits: dict[str, int] = field(
        default_factory=lambda: {
            "unparseable_transaction_date": 500,
            "unparseable_filing_date": 500,
            "negative_lag": 500,
            "lag_over_30_days": 500,
        }
    )

    def _add_bad_row(self, category: str, row: dict[str, Any]) -> None:
        limit = self.bad_row_limits.get(category, 500)
        existing = sum(1 for r in self.bad_rows if r.get("issue_category") == category)
        if existing < limit:
            row["issue_category"] = category
            self.bad_rows.append(row)

    def _reservoir_add(self, value: float) -> None:
        if len(self.lag_reservoir) < self.lag_reservoir_limit:
            self.lag_reservoir.append(value)
        else:
            idx = random.randint(0, self.valid_lag_count - 1)
            if idx < self.lag_reservoir_limit:
                self.lag_reservoir[idx] = value

    def process_chunk(self, chunk: pd.DataFrame) -> None:
        for record in chunk.to_dict(orient="records"):
            self.row_count += 1
            trans_raw = record.get("TRANS_DATE")
            filing_raw = record.get("FILING_DATE")
            trans_date = parse_sec_date(trans_raw)
            filing_date = parse_sec_date(filing_raw)

            base_row = {
                "ACCESSION_NUMBER": record.get("ACCESSION_NUMBER"),
                "NONDERIV_TRANS_SK": record.get("NONDERIV_TRANS_SK"),
                "TRANS_DATE_raw": trans_raw,
                "FILING_DATE_raw": filing_raw,
                "DOCUMENT_TYPE": record.get("DOCUMENT_TYPE"),
                "TRANS_CODE": record.get("TRANS_CODE"),
            }

            if trans_date is None:
                self.invalid_date_count += 1
                self._add_bad_row("unparseable_transaction_date", {**base_row})
                continue
            if filing_date is None:
                self.invalid_date_count += 1
                self._add_bad_row("unparseable_filing_date", {**base_row})
                continue

            lag_days = (filing_date - trans_date).days
            self.valid_lag_count += 1
            self.sum_lag += lag_days
            self.min_lag = lag_days if self.min_lag is None else min(self.min_lag, lag_days)
            self.max_lag = lag_days if self.max_lag is None else max(self.max_lag, lag_days)
            self._reservoir_add(float(lag_days))

            if lag_days == 0:
                self.same_day += 1
            elif 1 <= lag_days <= 2:
                self.lag_1_2_days += 1
            elif lag_days > 2:
                self.over_2_days += 1
            if lag_days < 0:
                self.negative_lag += 1
                self._add_bad_row("negative_lag", {**base_row, "filing_lag_days": lag_days})

            if lag_days > 30:
                self._add_bad_row("lag_over_30_days", {**base_row, "filing_lag_days": lag_days})

            bucket = _lag_bucket(lag_days)
            self.bucket_counts[bucket] = self.bucket_counts.get(bucket, 0) + 1

            year = filing_date.year
            self.yearly_lag_sums[year] = self.yearly_lag_sums.get(year, 0.0) + lag_days
            self.yearly_lag_counts[year] = self.yearly_lag_counts.get(year, 0) + 1
            samples = self.yearly_lag_samples.setdefault(year, [])
            if len(samples) < 5000:
                samples.append(float(lag_days))

    def summary_dict(self) -> dict[str, Any]:
        valid = self.valid_lag_count
        median = float(pd.Series(self.lag_reservoir).median()) if self.lag_reservoir else None
        return {
            "row_count": self.row_count,
            "valid_lag_count": valid,
            "invalid_date_count": self.invalid_date_count,
            "mean_lag_days": round(self.sum_lag / valid, 4) if valid else None,
            "median_lag_days": median,
            "min_lag_days": self.min_lag,
            "max_lag_days": self.max_lag,
            "pct_same_day": round(100.0 * self.same_day / valid, 4) if valid else None,
            "pct_1_2_days": round(100.0 * self.lag_1_2_days / valid, 4) if valid else None,
            "pct_over_2_days": round(100.0 * self.over_2_days / valid, 4) if valid else None,
            "pct_negative_lag": round(100.0 * self.negative_lag / valid, 4) if valid else None,
        }


def _lag_bucket(lag_days: int) -> str:
    if lag_days < 0:
        return "negative"
    if lag_days <= 2:
        return "0-2 days"
    if lag_days <= 10:
        return "3-10 days"
    if lag_days <= 30:
        return "11-30 days"
    if lag_days <= 90:
        return "31-90 days"
    return "90+ days"


def compute_filing_lag(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    chunk_size: int = 100_000,
    sample_mode: bool = False,
    sample_limit: int = 500_000,
) -> dict[str, pd.DataFrame]:
    """
    Compute filing lag using Python SEC date parsing (supports DD-MON-YYYY).

    Processes the submission join in chunks to limit memory use.
    """
    out = ensure_export_dirs(export_dir)
    accumulator = FilingLagAccumulator()

    query = FILING_LAG_QUERY
    if sample_mode:
        query += f" ORDER BY RANDOM() LIMIT {sample_limit}"

    logger.info(
        "computing filing lag (chunk_size=%s, sample_mode=%s)",
        chunk_size,
        sample_mode,
    )

    for chunk in pd.read_sql_query(query, conn, chunksize=chunk_size):
        accumulator.process_chunk(chunk)

    summary = accumulator.summary_dict()
    summary_df = pd.DataFrame([summary])

    buckets_df = pd.DataFrame(
        [
            {"lag_bucket": bucket, "row_count": count}
            for bucket, count in sorted(
                accumulator.bucket_counts.items(),
                key=lambda item: item[0],
            )
        ]
    )

    yearly_rows = []
    for year in sorted(accumulator.yearly_lag_samples):
        samples = accumulator.yearly_lag_samples[year]
        yearly_rows.append(
            {
                "filing_year": year,
                "row_count": accumulator.yearly_lag_counts.get(year, 0),
                "avg_lag_days": round(
                    accumulator.yearly_lag_sums[year]
                    / accumulator.yearly_lag_counts[year],
                    4,
                ),
                "median_lag_days": float(pd.Series(samples).median()) if samples else None,
            }
        )
    yearly_df = pd.DataFrame(yearly_rows)

    bad_df = pd.DataFrame(accumulator.bad_rows)
    export_csv(summary_df, "filing_lag_analysis.csv", out)
    if not bad_df.empty:
        export_csv(bad_df, "filing_lag_bad_rows_sample.csv", out)

    plot_filing_lag_charts_v2(accumulator, yearly_df, out)

    export_lag = summary_df.copy()
    if not buckets_df.empty:
        export_lag = pd.concat(
            [export_lag.assign(section="summary"), buckets_df.assign(section="lag_bucket")],
            ignore_index=True,
        )

    return {
        "filing_lag_analysis": export_lag,
        "lag_stats": summary_df,
        "lag_buckets": buckets_df,
        "yearly_lag": yearly_df,
        "bad_rows": bad_df,
        "accumulator": accumulator,
    }


def plot_filing_lag_charts_v2(
    accumulator: FilingLagAccumulator,
    yearly_df: pd.DataFrame,
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    if accumulator.bucket_counts:
        buckets = sorted(accumulator.bucket_counts.items(), key=lambda x: x[0])
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.bar([b[0] for b in buckets], [b[1] for b in buckets], color="seagreen")
        ax.set_title("Filing Lag Distribution")
        ax.set_xlabel("Lag bucket")
        ax.set_ylabel("Count")
        plt.xticks(rotation=30, ha="right")
        fig.tight_layout()
        paths.append(save_chart(fig, "filing_lag_distribution.png", export_dir))

    if not yearly_df.empty and yearly_df["median_lag_days"].notna().any():
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(
            yearly_df["filing_year"],
            yearly_df["median_lag_days"],
            marker="o",
            color="seagreen",
        )
        ax.set_title("Median Filing Lag by Year")
        ax.set_xlabel("Filing year")
        ax.set_ylabel("Median lag (days)")
        fig.tight_layout()
        paths.append(save_chart(fig, "filing_lag_median_by_year.png", export_dir))
    return paths


def analyze_filing_lag(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    **kwargs: Any,
) -> dict[str, pd.DataFrame]:
    """Section 9: filing lag (delegates to compute_filing_lag)."""
    return compute_filing_lag(conn, export_dir=export_dir, **kwargs)


@dataclass
class NumericFieldAccumulator:
    table_name: str
    column_name: str
    total_rows: int = 0
    parseable_count: int = 0
    unparseable_count: int = 0
    null_count: int = 0
    zero_count: int = 0
    negative_count: int = 0
    values_reservoir: list[float] = field(default_factory=list)
    reservoir_limit: int = 200_000
    unparseable_samples: list[str] = field(default_factory=list)
    unparseable_sample_limit: int = 100

    def process_value(self, raw: Any) -> None:
        self.total_rows += 1
        if raw is None or str(raw).strip() == "":
            self.null_count += 1
            return
        parsed = parse_sec_number(raw)
        if parsed is None:
            self.unparseable_count += 1
            if len(self.unparseable_samples) < self.unparseable_sample_limit:
                self.unparseable_samples.append(str(raw))
            return
        self.parseable_count += 1
        if parsed == 0:
            self.zero_count += 1
        if parsed < 0:
            self.negative_count += 1
        if len(self.values_reservoir) < self.reservoir_limit:
            self.values_reservoir.append(parsed)
        else:
            idx = random.randint(0, self.parseable_count - 1)
            if idx < self.reservoir_limit:
                self.values_reservoir[idx] = parsed

    def summary_row(self) -> dict[str, Any]:
        series = pd.Series(self.values_reservoir)
        return {
            "table_name": self.table_name,
            "column_name": self.column_name,
            "total_rows": self.total_rows,
            "parseable_count": self.parseable_count,
            "unparseable_count": self.unparseable_count,
            "null_count": self.null_count,
            "zero_count": self.zero_count,
            "negative_count": self.negative_count,
            "p01": float(series.quantile(0.01)) if not series.empty else None,
            "median": float(series.median()) if not series.empty else None,
            "p99": float(series.quantile(0.99)) if not series.empty else None,
            "max": float(series.max()) if not series.empty else None,
        }


def audit_numeric_fields(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    chunk_size: int = 100_000,
) -> dict[str, pd.DataFrame]:
    """Audit numeric castability for key raw_nonderiv_trans fields."""
    out = ensure_export_dirs(export_dir)
    fields = [
        "TRANS_SHARES",
        "TRANS_PRICEPERSHARE",
        "SHRS_OWND_FOLWNG_TRANS",
        "VALU_OWND_FOLWNG_TRANS",
    ]
    accumulators = {
        col: NumericFieldAccumulator("raw_nonderiv_trans", col) for col in fields
    }

    query = f"SELECT {', '.join(fields)} FROM raw_nonderiv_trans"
    logger.info("auditing numeric fields in chunks of %s", chunk_size)

    for chunk in pd.read_sql_query(query, conn, chunksize=chunk_size):
        for col in fields:
            for value in chunk[col].tolist():
                accumulators[col].process_value(value)

    summary_rows = [acc.summary_row() for acc in accumulators.values()]
    summary_df = pd.DataFrame(summary_rows)
    export_csv(summary_df, "numeric_casting_audit.csv", out)

    unparseable_rows: list[dict[str, Any]] = []
    for col, acc in accumulators.items():
        for sample in acc.unparseable_samples:
            unparseable_rows.append(
                {"column_name": col, "raw_value": sample, "table_name": acc.table_name}
            )
    unparseable_df = pd.DataFrame(unparseable_rows)
    if not unparseable_df.empty:
        export_csv(unparseable_df, "numeric_unparseable_samples.csv", out)

    return {
        "numeric_casting_audit": summary_df,
        "numeric_unparseable_samples": unparseable_df,
    }


def analyze_yearly_activity(conn: sqlite3.Connection) -> pd.DataFrame:
    """Section 10: yearly filings, buys, sells, unique insiders and issuers."""
    filing_year_sql = f"CAST(strftime('%Y', {sec_date_expr('FILING_DATE')}) AS INTEGER)"
    filings = read_sql(
        conn,
        f"""
        SELECT {filing_year_sql} AS year, COUNT(*) AS filings
        FROM raw_submission
        WHERE {sec_date_expr('FILING_DATE')} IS NOT NULL
        GROUP BY 1
        """,
    )

    buys_sells = read_sql(
        conn,
        f"""
        SELECT
            source_year AS year,
            SUM(CASE WHEN TRIM(TRANS_CODE) = 'P' THEN 1 ELSE 0 END) AS insider_buys,
            SUM(CASE WHEN TRIM(TRANS_CODE) = 'S' THEN 1 ELSE 0 END) AS insider_sells
        FROM raw_nonderiv_trans
        WHERE source_year IS NOT NULL
        GROUP BY source_year
        ORDER BY source_year
        """,
    )

    unique_insiders = read_sql(
        conn,
        f"""
        SELECT source_year AS year, COUNT(DISTINCT TRIM(RPTOWNERCIK)) AS unique_insiders
        FROM raw_reporting_owner
        WHERE source_year IS NOT NULL AND NOT {is_blank_expr("RPTOWNERCIK")}
        GROUP BY source_year
        """,
    )

    unique_companies = read_sql(
        conn,
        f"""
        SELECT source_year AS year, COUNT(DISTINCT TRIM(ISSUERCIK)) AS unique_companies
        FROM raw_submission
        WHERE source_year IS NOT NULL AND NOT {is_blank_expr("ISSUERCIK")}
        GROUP BY source_year
        """,
    )

    summary = filings.merge(buys_sells, on="year", how="outer")
    summary = summary.merge(unique_insiders, on="year", how="outer")
    summary = summary.merge(unique_companies, on="year", how="outer")
    summary = summary.sort_values("year")
    return summary


def analyze_data_quality_flags(conn: sqlite3.Connection) -> pd.DataFrame:
    """Section 11: data quality issue counts."""
    shares = numeric_expr("TRANS_SHARES")
    price = numeric_expr("TRANS_PRICEPERSHARE")
    trans_date = sec_date_expr("TRANS_DATE")
    filing_date = sec_date_expr("FILING_DATE")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    flags: list[dict[str, Any]] = []

    def add_flag(flag_name: str, table_name: str, count: int, description: str) -> None:
        flags.append(
            {
                "flag_name": flag_name,
                "table_name": table_name,
                "issue_count": count,
                "description": description,
            }
        )

    neg_shares = read_sql(
        conn,
        f"SELECT COUNT(*) AS n FROM raw_nonderiv_trans WHERE {shares} < 0",
    ).iloc[0, 0]
    add_flag("negative_shares", "raw_nonderiv_trans", int(neg_shares), "TRANS_SHARES < 0")

    zero_price = read_sql(
        conn,
        f"SELECT COUNT(*) AS n FROM raw_nonderiv_trans WHERE {price} = 0",
    ).iloc[0, 0]
    add_flag("zero_price", "raw_nonderiv_trans", int(zero_price), "TRANS_PRICEPERSHARE = 0")

    future_trans = read_sql(
        conn,
        f"""
        SELECT COUNT(*) AS n FROM raw_nonderiv_trans
        WHERE {trans_date} > '{today}'
        """,
    ).iloc[0, 0]
    add_flag("future_transaction_date", "raw_nonderiv_trans", int(future_trans), "TRANS_DATE > today")

    future_filing = read_sql(
        conn,
        f"""
        SELECT COUNT(*) AS n FROM raw_submission
        WHERE {filing_date} > '{today}'
        """,
    ).iloc[0, 0]
    add_flag("future_filing_date", "raw_submission", int(future_filing), "FILING_DATE > today")

    impossible_lag = read_sql(
        conn,
        f"""
        SELECT COUNT(*) AS n
        FROM raw_nonderiv_trans t
        JOIN raw_submission s ON t.ACCESSION_NUMBER = s.ACCESSION_NUMBER
        WHERE julianday({sec_date_expr("s.FILING_DATE")}) <
              julianday({sec_date_expr("t.TRANS_DATE")})
        """,
    ).iloc[0, 0]
    add_flag(
        "filing_before_transaction",
        "raw_nonderiv_trans+raw_submission",
        int(impossible_lag),
        "Filing date before transaction date",
    )

    dup_hash = read_sql(
        conn,
        """
        SELECT COUNT(*) - COUNT(DISTINCT row_hash) AS n FROM raw_nonderiv_trans
        """,
    ).iloc[0, 0]
    add_flag("duplicate_row_hash", "raw_nonderiv_trans", int(dup_hash), "Duplicate row_hash values")

    extreme_value = read_sql(
        conn,
        f"""
        SELECT COUNT(*) AS n FROM raw_nonderiv_trans
        WHERE ({shares} * {price}) > 1e10
        """,
    ).iloc[0, 0]
    add_flag(
        "extreme_implied_value",
        "raw_nonderiv_trans",
        int(extreme_value),
        "Implied value > 10B",
    )

    return pd.DataFrame(flags)


def plot_filing_charts(
    filing_results: dict[str, pd.DataFrame],
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    by_year = filing_results.get("filings_by_year")
    if by_year is not None and not by_year.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(by_year["filing_year"], by_year["filing_count"], marker="o")
        ax.set_title("Filings Over Time")
        ax.set_xlabel("Year")
        ax.set_ylabel("Filing count")
        fig.tight_layout()
        paths.append(save_chart(fig, "filings_over_time.png", export_dir))

    doc = filing_results.get("document_type_distribution")
    if doc is not None and not doc.empty:
        top = doc.head(10)
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.barh(top["document_type"].astype(str), top["row_count"], color="steelblue")
        ax.invert_yaxis()
        ax.set_title("Top Document Types")
        ax.set_xlabel("Count")
        fig.tight_layout()
        paths.append(save_chart(fig, "filing_type_distribution.png", export_dir))
    return paths


def plot_transaction_code_charts(
    trans_results: dict[str, pd.DataFrame],
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    focus = trans_results.get("focus_codes_yearly")
    if focus is not None and not focus.empty:
        pivot = focus.pivot_table(
            index="source_year",
            columns="transaction_code",
            values="row_count",
            fill_value=0,
        )
        fig, ax = plt.subplots(figsize=(10, 5))
        for code in pivot.columns:
            ax.plot(pivot.index, pivot[code], marker="o", label=code)
        ax.set_title("Focus Transaction Codes Over Time")
        ax.set_xlabel("Year")
        ax.set_ylabel("Count")
        ax.legend(ncol=3, fontsize=8)
        fig.tight_layout()
        paths.append(save_chart(fig, "transaction_codes_over_time.png", export_dir))

    dist = trans_results.get("focus_codes")
    if dist is not None and not dist.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(dist["transaction_code"].astype(str), dist["row_count"], color="coral")
        ax.set_title("Focus Transaction Code Distribution")
        ax.set_xlabel("Code")
        ax.set_ylabel("Count")
        fig.tight_layout()
        paths.append(save_chart(fig, "transaction_code_distribution.png", export_dir))
    return paths


def plot_filing_lag_charts(
    lag_results: dict[str, pd.DataFrame],
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    buckets = lag_results.get("lag_buckets")
    if buckets is not None and not buckets.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(buckets["lag_bucket"].astype(str), buckets["row_count"], color="seagreen")
        ax.set_title("Filing Lag Distribution")
        ax.set_xlabel("Lag bucket")
        ax.set_ylabel("Count")
        plt.xticks(rotation=30, ha="right")
        fig.tight_layout()
        paths.append(save_chart(fig, "filing_lag_histogram.png", export_dir))

    yearly = lag_results.get("yearly_lag")
    if yearly is not None and not yearly.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(yearly["filing_year"], yearly["avg_lag_days"], marker="o", color="seagreen")
        ax.set_title("Average Filing Lag by Year")
        ax.set_xlabel("Year")
        ax.set_ylabel("Average lag (days)")
        fig.tight_layout()
        paths.append(save_chart(fig, "filing_lag_yearly_avg.png", export_dir))
    return paths


def plot_yearly_activity_charts(
    yearly_df: pd.DataFrame,
    export_dir: Path,
) -> list[Path]:
    paths: list[Path] = []
    if yearly_df.empty:
        return paths

    fig, ax = plt.subplots(figsize=(10, 4))
    if "filings" in yearly_df.columns:
        ax.plot(yearly_df["year"], yearly_df["filings"], marker="o", label="Filings")
    ax.set_title("Yearly Filing Activity")
    ax.set_xlabel("Year")
    ax.set_ylabel("Count")
    ax.legend()
    fig.tight_layout()
    paths.append(save_chart(fig, "yearly_filings.png", export_dir))

    if {"insider_buys", "insider_sells"}.issubset(yearly_df.columns):
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.bar(
            yearly_df["year"].astype(str),
            yearly_df["insider_buys"],
            label="Buys (P)",
            color="green",
            alpha=0.7,
        )
        ax.bar(
            yearly_df["year"].astype(str),
            yearly_df["insider_sells"],
            bottom=yearly_df["insider_buys"],
            label="Sells (S)",
            color="red",
            alpha=0.7,
        )
        ax.set_title("Insider Buys vs Sells by Year")
        ax.set_xlabel("Year")
        ax.set_ylabel("Transaction count")
        ax.legend()
        plt.xticks(rotation=45, ha="right")
        fig.tight_layout()
        paths.append(save_chart(fig, "yearly_buys_sells_stacked.png", export_dir))
    return paths


def generate_executive_summary(
    overview: pd.DataFrame,
    filing_results: dict[str, pd.DataFrame],
    trans_results: dict[str, pd.DataFrame],
    missing_df: pd.DataFrame,
    quality_df: pd.DataFrame,
    lag_results: dict[str, pd.DataFrame],
) -> str:
    """Section 12: markdown executive summary from audit artifacts."""
    total_rows = int(overview.loc[overview["table_name"].isin(RAW_TABLES), "row_count"].sum())
    top_codes = trans_results.get("focus_codes", pd.DataFrame())
    top_code_str = ", ".join(top_codes["transaction_code"].head(5).astype(str).tolist()) if not top_codes.empty else "n/a"
    worst_missing = missing_df.sort_values("null_pct", ascending=False).head(5)
    quality_issues = quality_df[quality_df["issue_count"] > 0]

    lines = [
        "# Phase 2 Raw Data Audit — Executive Summary",
        "",
        f"*Generated: {datetime.now(timezone.utc).isoformat()}*",
        "",
        "## Key findings",
        "",
        f"- **Total raw rows** (8 core tables): {total_rows:,}",
        f"- **Database size (MB)**: {overview['db_size_mb'].iloc[0] if 'db_size_mb' in overview.columns else 'n/a'}",
        f"- **Source year coverage**: {overview.get('min_source_year', pd.Series([None])).iloc[0]} – {overview.get('max_source_year', pd.Series([None])).iloc[0]}",
        "",
        "## Schema consistency (Phase 1.2)",
        "",
        "- Review `data/exports/schema_audit/` for header drift across quarterly folders.",
        "- Raw SQLite tables preserve SEC column names as TEXT without coercion.",
        "",
        "## Dominant transaction codes",
        "",
        f"- Focus codes tracked: {', '.join(FOCUS_TRANS_CODES)}",
        f"- Top focus codes in sample: {top_code_str}",
        "",
        "## Data quality issues",
        "",
    ]
    if quality_issues.empty:
        lines.append("- No quality flags triggered at configured thresholds.")
    else:
        for _, row in quality_issues.iterrows():
            lines.append(
                f"- **{row['flag_name']}** ({row['table_name']}): {row['issue_count']:,} — {row['description']}"
            )

    lines.extend(
        [
            "",
            "## Missingness hotspots",
            "",
        ]
    )
    for _, row in worst_missing.iterrows():
        lines.append(
            f"- `{row['field_label']}` ({row['table_name']}): {row['null_pct']}% null"
        )

    lag_stats = lag_results.get("lag_stats")
    if lag_stats is not None and not lag_stats.empty:
        row = lag_stats.iloc[0]
        lines.extend(
            [
                "",
                "## Filing lag",
                "",
                f"- Valid lag rows: {row.get('valid_lag_count', 'n/a'):,}",
                f"- Mean lag (days): {row.get('mean_lag_days', 'n/a')}",
                f"- Median lag (days): {row.get('median_lag_days', 'n/a')}",
                f"- Same-day filings: {row.get('pct_same_day', 'n/a')}%",
                f"- Negative lag: {row.get('pct_negative_lag', 'n/a')}%",
            ]
        )

    lines.extend(
        [
            "",
            "## Recommended cleaning rules (next phase — not applied here)",
            "",
            "- Parse SEC text dates into ISO dates; drop rows with unparseable dates.",
            "- Cast numeric fields only after validating non-numeric footnote placeholders.",
            "- Standardize `SECURITY_TITLE` and `RPTOWNER_TITLE` via normalization lookup tables.",
            "- Flag or exclude rows with negative shares, zero prices, or filing-before-transaction.",
            "",
            "## Recommended filters for clean transaction layer",
            "",
            "- Restrict to Form 4 open-market-style codes (`P`, `S`) only after explicit review.",
            "- Require non-null `TRANS_DATE`, `TRANS_CODE`, `ISSUERTRADINGSYMBOL`, and owner CIK.",
            "- Join `raw_nonderiv_trans` → `raw_submission` → `raw_reporting_owner` on `ACCESSION_NUMBER`.",
            "- Deduplicate on `row_hash` or business keys (`ACCESSION_NUMBER`, `NONDERIV_TRANS_SK`).",
            "",
        ]
    )
    return "\n".join(lines)


def run_full_audit(
    conn: sqlite3.Connection,
    export_dir: Path | None = None,
    db_path: Path | None = None,
    distribution_sample_size: int = 100_000,
) -> dict[str, Any]:
    """Run all audit sections and export CSV/chart artifacts."""
    out = ensure_export_dirs(export_dir)
    logger.info("starting raw data audit -> %s", out)

    results: dict[str, Any] = {}

    overview = get_database_overview(conn, db_path)
    export_csv(overview, "table_overview.csv", out)
    results["overview"] = overview

    filing = analyze_filing_types(conn)
    export_csv(filing["filing_type_distribution"], "filing_type_distribution.csv", out)
    plot_filing_charts(filing, out)
    results["filing"] = filing

    trans = analyze_transaction_codes(conn)
    export_csv(trans["transaction_code_distribution"], "transaction_code_distribution.csv", out)
    plot_transaction_code_charts(trans, out)
    results["transaction_codes"] = trans

    taxonomy = analyze_transaction_code_taxonomy(conn, export_dir=out)
    results["transaction_code_taxonomy"] = taxonomy

    security_tax = analyze_security_type_taxonomy(conn, export_dir=out)
    results["security_type_taxonomy"] = security_tax

    role_tax = analyze_role_taxonomy(conn, export_dir=out)
    results["role_taxonomy"] = role_tax

    security = analyze_security_titles(conn)
    export_csv(security["security_type_distribution"], "security_type_distribution.csv", out)
    results["security"] = security

    ownership = analyze_ownership(conn)
    export_csv(ownership["ownership_distribution"], "ownership_distribution.csv", out)
    results["ownership"] = ownership

    titles = analyze_officer_titles(conn)
    export_csv(titles["officer_title_distribution"], "officer_title_distribution.csv", out)
    results["officer_titles"] = titles

    missing = analyze_missing_values(conn)
    export_csv(missing, "missing_value_report.csv", out)
    plot_missingness_heatmap(missing, out)
    results["missing"] = missing

    dist = analyze_distributions(conn, sample_size=distribution_sample_size)
    export_csv(dist["distribution_summary"], "distribution_summary.csv", out)
    plot_distribution_histograms(dist["numeric_sample"], out)
    results["distributions"] = dist

    lag = compute_filing_lag(conn, export_dir=out)
    results["filing_lag"] = lag

    numeric = audit_numeric_fields(conn, export_dir=out)
    results["numeric_audit"] = numeric

    yearly = analyze_yearly_activity(conn)
    export_csv(yearly, "yearly_activity_summary.csv", out)
    plot_yearly_activity_charts(yearly, out)
    results["yearly_activity"] = yearly

    quality = analyze_data_quality_flags(conn)
    export_csv(quality, "data_quality_flags.csv", out)
    results["quality"] = quality

    summary_md = generate_executive_summary(
        overview, filing, trans, missing, quality, lag
    )
    summary_path = out / "executive_summary.md"
    summary_path.write_text(summary_md, encoding="utf-8")
    logger.info("wrote %s", summary_path.name)
    results["executive_summary"] = summary_md

    logger.info("raw data audit complete")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run raw SEC data audit sections.")
    parser.add_argument(
        "--section",
        choices=[
            "filing_lag",
            "numeric",
            "taxonomy",
            "security-taxonomy",
            "role-taxonomy",
            "debug",
            "all",
        ],
        default="all",
        help="Audit section to run (default: all).",
    )
    parser.add_argument("--export-dir", type=Path, default=EXPORT_DIR)
    parser.add_argument("--chunk-size", type=int, default=100_000)
    parser.add_argument(
        "--sample-mode",
        action="store_true",
        help="Filing lag: process random sample only.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)

    conn = get_connection()
    try:
        out = ensure_export_dirs(args.export_dir)
        if args.section == "filing_lag":
            compute_filing_lag(
                conn,
                export_dir=out,
                chunk_size=args.chunk_size,
                sample_mode=args.sample_mode,
            )
        elif args.section == "numeric":
            audit_numeric_fields(conn, export_dir=out, chunk_size=args.chunk_size)
        elif args.section == "taxonomy":
            analyze_transaction_code_taxonomy(conn, export_dir=out)
        elif args.section == "security-taxonomy":
            analyze_security_type_taxonomy(conn, export_dir=out)
        elif args.section == "role-taxonomy":
            analyze_role_taxonomy(conn, export_dir=out)
        elif args.section == "debug":
            from src.analytics.debug_raw_fields import run_debug_inspection

            debug_out = out / "debug_raw_fields"
            run_debug_inspection(conn, debug_out)
        else:
            run_full_audit(conn, export_dir=out)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
