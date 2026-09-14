"""
Phase 3.5: read-only validation and QA for clean_insider_transactions.

Usage:
    python -m src.analytics.validate_clean_layer
    python -m src.analytics.validate_clean_layer --verbose
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from src.cleaning.clean_logic import load_cleaning_config
from src.utils.db import PROJECT_ROOT, get_connection

logger = logging.getLogger(__name__)

EXPORT_DIR = PROJECT_ROOT / "data" / "exports" / "validation"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "insider_research.db"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "cleaning.yaml"

VALIDATION_SUMMARY_COLUMNS: tuple[str, ...] = (
    "check_id",
    "check_name",
    "severity",
    "metric_value",
    "threshold",
    "description",
    "recommended_action",
)

DEFAULT_VALIDATION_CONFIG: dict[str, Any] = {
    "max_reasonable_median_filing_lag": 5,
    "max_unknown_role_pct_mvp_warn": 30.0,
    "max_unknown_security_pct_warn": 5.0,
    "extreme_filing_lag_warn": 365,
}

MVP_INTEGRITY_RULES: tuple[tuple[str, str], ...] = (
    ("not_form_4", "is_form_4 != 1"),
    ("is_amendment", "is_amendment = 1"),
    ("transaction_code_not_ps", "transaction_code NOT IN ('P', 'S')"),
    ("trans_taxonomy_not_mvp", "transaction_include_clean_mvp != 1"),
    ("security_not_common_equity", "normalized_security_type != 'COMMON_EQUITY'"),
    ("security_taxonomy_not_mvp", "security_include_clean_mvp != 1"),
    ("invalid_clean_event", "is_valid_clean_event != 1"),
    ("below_minimum_value", "transaction_value < :min_value OR transaction_value IS NULL"),
    ("shares_not_positive", "shares IS NULL OR shares <= 0"),
    ("price_not_positive", "price_per_share IS NULL OR price_per_share <= 0"),
    ("negative_filing_lag", "filing_lag_days IS NULL OR filing_lag_days < 0"),
    ("invalid_ticker_placeholder", "is_invalid_ticker_placeholder = 1"),
    ("multisymbol_ticker", "is_multisymbol_ticker = 1"),
    ("invalid_ticker", "is_valid_ticker != 1"),
    ("ticker_clean_null", "ticker_clean IS NULL OR TRIM(ticker_clean) = ''"),
    ("missing_insider_cik", "insider_cik IS NULL OR TRIM(insider_cik) = ''"),
)


@dataclass
class ValidationCheck:
    check_id: str
    check_name: str
    severity: str
    metric_value: str
    threshold: str
    description: str
    recommended_action: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def load_validation_config(config_path: Path) -> dict[str, Any]:
    with config_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    cleaning = load_cleaning_config(data.get("cleaning", data))
    validation = data.get("validation", {})
    merged = dict(cleaning)
    merged.update(DEFAULT_VALIDATION_CONFIG)
    merged.update(validation)
    return merged


def ensure_export_dirs(export_dir: Path) -> Path:
    (export_dir / "charts").mkdir(parents=True, exist_ok=True)
    return export_dir


def export_csv(df: pd.DataFrame, filename: str, export_dir: Path) -> Path:
    path = export_dir / filename
    df.to_csv(path, index=False)
    logger.info("exported %s (%s rows)", path.name, len(df))
    return path


def save_chart(fig: Any, filename: str, export_dir: Path) -> Path:
    import matplotlib.pyplot as plt

    chart_dir = export_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    path = chart_dir / filename
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    logger.info("saved chart %s", path.name)
    return path


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def scalar(conn: sqlite3.Connection, sql: str, params: tuple | dict | None = None) -> Any:
    if params is None:
        return conn.execute(sql).fetchone()[0]
    return conn.execute(sql, params).fetchone()[0]


def severity_from_count(
    count: int | float,
    *,
    fail_if_positive: bool = True,
    warn_if_positive: bool = False,
) -> str:
    if fail_if_positive and count > 0:
        return "FAIL"
    if warn_if_positive and count > 0:
        return "WARN"
    return "PASS"


def severity_from_pct(
    pct: float,
    warn_threshold: float,
    fail_threshold: float | None = None,
) -> str:
    if fail_threshold is not None and pct >= fail_threshold:
        return "FAIL"
    if pct >= warn_threshold:
        return "WARN"
    return "PASS"


def checks_to_dataframe(checks: list[ValidationCheck]) -> pd.DataFrame:
    rows = [c.to_dict() for c in checks]
    return pd.DataFrame(rows, columns=list(VALIDATION_SUMMARY_COLUMNS))


def compute_mvp_integrity_violations(
    conn: sqlite3.Connection,
    config: dict[str, Any],
) -> pd.DataFrame:
    min_value = float(config["minimum_transaction_value"])
    rows: list[dict[str, Any]] = []
    for violation_type, condition in MVP_INTEGRITY_RULES:
        sql = f"""
            SELECT COUNT(*) FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND ({condition})
        """
        if ":min_value" in condition:
            count = scalar(conn, sql, {"min_value": min_value})
        else:
            count = scalar(conn, sql)
        rows.append(
            {
                "violation_type": violation_type,
                "violation_count": int(count or 0),
                "condition": condition,
            }
        )
    return pd.DataFrame(rows)


def compute_survival_funnel(
    conn: sqlite3.Connection,
    config: dict[str, Any],
) -> pd.DataFrame:
    min_value = float(config["minimum_transaction_value"])
    stages = [
        ("1_total_clean_rows", "SELECT COUNT(*) FROM clean_insider_transactions"),
        ("2_form_4_rows", "SELECT COUNT(*) FROM clean_insider_transactions WHERE is_form_4 = 1"),
        (
            "3_non_amendment_form_4",
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE is_form_4 = 1 AND is_amendment = 0",
        ),
        (
            "4_ps_transaction_codes",
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE transaction_code IN ('P', 'S')",
        ),
        (
            "5_common_equity",
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE normalized_security_type = 'COMMON_EQUITY'",
        ),
        (
            "6_valid_clean_event",
            "SELECT COUNT(*) FROM clean_insider_transactions WHERE is_valid_clean_event = 1",
        ),
        (
            "7_value_above_minimum",
            f"SELECT COUNT(*) FROM clean_insider_transactions "
            f"WHERE transaction_value >= {min_value}",
        ),
        (
            "8_include_mvp_alpha",
            "SELECT COUNT(*) FROM clean_insider_transactions WHERE include_mvp_alpha = 1",
        ),
    ]
    records = []
    total = None
    for stage_id, sql in stages:
        count = int(scalar(conn, sql) or 0)
        if total is None:
            total = count
        pct = (count / total * 100) if total else 0.0
        records.append(
            {
                "stage_id": stage_id,
                "row_count": count,
                "pct_of_clean": round(pct, 4),
            }
        )
    return pd.DataFrame(records)


def distribution_by_column(
    conn: sqlite3.Connection,
    column: str,
    *,
    mvp_only: bool = False,
    extra_columns: list[str] | None = None,
    limit: int = 50,
) -> pd.DataFrame:
    cols = [column] + (extra_columns or [])
    group_cols = ", ".join(cols)
    where = "WHERE include_mvp_alpha = 1" if mvp_only else ""
    total = scalar(
        conn,
        f"SELECT COUNT(*) FROM clean_insider_transactions {where}",
    )
    sql = f"""
        SELECT {group_cols}, COUNT(*) AS row_count
        FROM clean_insider_transactions
        {where}
        GROUP BY {group_cols}
        ORDER BY row_count DESC
        LIMIT {limit}
    """
    df = pd.read_sql_query(sql, conn)
    df["universe"] = "mvp" if mvp_only else "all_clean"
    df["pct_total"] = (df["row_count"] / total * 100).round(4) if total else 0.0
    return df


def section_a_row_counts(
    conn: sqlite3.Connection,
    checks: list[ValidationCheck],
) -> pd.DataFrame:
    clean_exists = table_exists(conn, "clean_insider_transactions")
    checks.append(
        ValidationCheck(
            "A01",
            "clean_table_exists",
            "PASS" if clean_exists else "FAIL",
            str(clean_exists),
            "true",
            "clean_insider_transactions must exist",
            "Run build_clean_insider_transactions --reset" if not clean_exists else "None",
        )
    )

    raw_nonderiv = int(scalar(conn, "SELECT COUNT(*) FROM raw_nonderiv_trans") or 0) if clean_exists else 0
    raw_submission = int(scalar(conn, "SELECT COUNT(*) FROM raw_submission") or 0)
    raw_owners = int(scalar(conn, "SELECT COUNT(*) FROM raw_reporting_owner") or 0)
    clean_rows = (
        int(scalar(conn, "SELECT COUNT(*) FROM clean_insider_transactions") or 0)
        if clean_exists
        else 0
    )
    mvp_rows = (
        int(
            scalar(
                conn,
                "SELECT COUNT(*) FROM clean_insider_transactions WHERE include_mvp_alpha = 1",
            )
            or 0
        )
        if clean_exists
        else 0
    )

    summary = pd.DataFrame(
        [
            {"table_name": "raw_nonderiv_trans", "row_count": raw_nonderiv},
            {"table_name": "raw_submission", "row_count": raw_submission},
            {"table_name": "raw_reporting_owner", "row_count": raw_owners},
            {"table_name": "clean_insider_transactions", "row_count": clean_rows},
            {"table_name": "mvp_alpha_rows", "row_count": mvp_rows},
        ]
    )

    checks.extend(
        [
            ValidationCheck(
                "A02",
                "clean_row_count_positive",
                severity_from_count(0 if clean_rows > 0 else 1, fail_if_positive=True),
                str(clean_rows),
                "> 0",
                "Clean table must contain rows",
                "Rebuild clean layer",
            ),
            ValidationCheck(
                "A03",
                "clean_rows_gte_raw_nonderiv",
                "PASS" if clean_rows >= raw_nonderiv else "WARN",
                str(clean_rows),
                f">= {raw_nonderiv}",
                "Clean rows should be >= raw nonderiv (owner expansion)",
                "Investigate join if clean count is unexpectedly low",
            ),
            ValidationCheck(
                "A04",
                "mvp_alpha_rows_positive",
                severity_from_count(0 if mvp_rows > 0 else 1, fail_if_positive=True),
                str(mvp_rows),
                "> 0",
                "MVP alpha universe must be non-empty",
                "Review cleaning config and taxonomies",
            ),
            ValidationCheck(
                "A05",
                "mvp_lt_total_clean",
                "PASS" if mvp_rows < clean_rows else "WARN",
                str(mvp_rows),
                f"< {clean_rows}",
                "MVP rows should be a subset of clean rows",
                "Review include_mvp_alpha logic",
            ),
        ]
    )
    return summary


def section_b_mvp_integrity(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    checks: list[ValidationCheck],
) -> pd.DataFrame:
    violations = compute_mvp_integrity_violations(conn, config)
    total_violations = int(violations["violation_count"].sum())
    checks.append(
        ValidationCheck(
            "B01",
            "mvp_integrity_zero_violations",
            severity_from_count(total_violations, fail_if_positive=True),
            str(total_violations),
            "0",
            "MVP rows must satisfy all integrity rules",
            "Inspect mvp_integrity_violations.csv and fix clean build logic",
        )
    )
    for _, row in violations.iterrows():
        if row["violation_count"] > 0:
            checks.append(
                ValidationCheck(
                    f"B_{row['violation_type']}",
                    f"mvp_violation_{row['violation_type']}",
                    "FAIL",
                    str(int(row["violation_count"])),
                    "0",
                    f"MVP violation: {row['violation_type']}",
                    "Fix taxonomy or MVP flag logic",
                )
            )
    return violations


def section_c_funnel(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    funnel = compute_survival_funnel(conn, config)
    mvp_count = int(funnel.loc[funnel["stage_id"] == "8_include_mvp_alpha", "row_count"].iloc[0])
    checks.append(
        ValidationCheck(
            "C01",
            "funnel_mvp_matches_flag",
            "PASS",
            str(mvp_count),
            str(mvp_count),
            "Final funnel stage matches include_mvp_alpha count",
            "None",
        )
    )
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5))
    labels = [s.replace("_", "\n") for s in funnel["stage_id"]]
    ax.barh(labels, funnel["row_count"], color="steelblue")
    ax.set_xlabel("Row count")
    ax.set_title("Filter survival funnel (clean → MVP)")
    ax.invert_yaxis()
    save_chart(fig, "filter_survival_funnel.png", export_dir)
    return funnel


def section_d_transaction_codes(
    conn: sqlite3.Connection,
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    all_dist = distribution_by_column(
        conn, "transaction_code", extra_columns=["transaction_category"]
    )
    mvp_dist = distribution_by_column(
        conn, "transaction_code", mvp_only=True, extra_columns=["transaction_category"]
    )
    combined = pd.concat([all_dist, mvp_dist], ignore_index=True)
    export_csv(combined, "transaction_code_distribution_clean.csv", export_dir)

    mvp_codes = set(mvp_dist["transaction_code"].astype(str))
    unexpected = mvp_codes - {"P", "S"}
    checks.append(
        ValidationCheck(
            "D01",
            "mvp_only_ps_codes",
            severity_from_count(len(unexpected), fail_if_positive=True),
            ",".join(sorted(unexpected)) or "none",
            "P,S only",
            "MVP universe should only contain P and S transaction codes",
            "Review transaction taxonomy and MVP filter",
        )
    )
    has_p = "P" in mvp_codes
    has_s = "S" in mvp_codes
    checks.append(
        ValidationCheck(
            "D02",
            "mvp_has_purchases",
            "PASS" if has_p else "WARN",
            str(has_p),
            "true",
            "MVP should include purchase (P) transactions",
            "Verify data coverage",
        )
    )
    checks.append(
        ValidationCheck(
            "D03",
            "mvp_has_sales",
            "PASS" if has_s else "WARN",
            str(has_s),
            "true",
            "MVP should include sale (S) transactions",
            "Verify data coverage",
        )
    )
    return combined


def section_e_security_types(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    all_dist = distribution_by_column(conn, "normalized_security_type")
    mvp_dist = distribution_by_column(conn, "normalized_security_type", mvp_only=True)
    combined = pd.concat([all_dist, mvp_dist], ignore_index=True)
    export_csv(combined, "security_type_distribution_clean.csv", export_dir)

    mvp_types = set(mvp_dist["normalized_security_type"].astype(str))
    unexpected = mvp_types - {"COMMON_EQUITY"}
    checks.append(
        ValidationCheck(
            "E01",
            "mvp_only_common_equity",
            severity_from_count(len(unexpected), fail_if_positive=True),
            ",".join(sorted(unexpected)) or "none",
            "COMMON_EQUITY only",
            "MVP should only include common equity securities",
            "Review security taxonomy",
        )
    )

    unknown_pct = float(
        all_dist.loc[
            all_dist["normalized_security_type"] == "UNKNOWN", "pct_total"
        ].sum()
        if "UNKNOWN" in all_dist["normalized_security_type"].values
        else 0.0
    )
    warn_thresh = float(config["max_unknown_security_pct_warn"])
    checks.append(
        ValidationCheck(
            "E02",
            "unknown_security_share_clean",
            severity_from_pct(unknown_pct, warn_thresh),
            f"{unknown_pct:.2f}%",
            f"< {warn_thresh}%",
            "UNKNOWN security share in full clean table",
            "Expand security_title_map.csv patterns",
        )
    )
    return combined


def section_f_roles(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    all_dist = distribution_by_column(
        conn, "normalized_role", extra_columns=["role_group"]
    )
    mvp_dist = distribution_by_column(
        conn, "normalized_role", mvp_only=True, extra_columns=["role_group"]
    )
    combined = pd.concat([all_dist, mvp_dist], ignore_index=True)
    export_csv(combined, "role_distribution_clean.csv", export_dir)

    unknown_pct = float(
        mvp_dist.loc[mvp_dist["normalized_role"] == "UNKNOWN", "pct_total"].sum()
        if "UNKNOWN" in mvp_dist["normalized_role"].values
        else 0.0
    )
    checks.append(
        ValidationCheck(
            "F01",
            "unknown_role_share_mvp",
            severity_from_pct(unknown_pct, float(config["max_unknown_role_pct_mvp_warn"])),
            f"{unknown_pct:.2f}%",
            f"< {config['max_unknown_role_pct_mvp_warn']}%",
            "UNKNOWN role share in MVP universe",
            "Expand officer_title_map.csv",
        )
    )

    exec_rows = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE include_mvp_alpha = 1 AND is_executive = 1",
        )
        or 0
    )
    board_rows = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE include_mvp_alpha = 1 AND is_board_member = 1",
        )
        or 0
    )
    null_weight = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE include_mvp_alpha = 1 AND role_weight IS NULL",
        )
        or 0
    )
    checks.extend(
        [
            ValidationCheck(
                "F02",
                "mvp_has_executives",
                "PASS" if exec_rows > 0 else "WARN",
                str(exec_rows),
                "> 0",
                "MVP should include executive-flagged rows",
                "Review role taxonomy",
            ),
            ValidationCheck(
                "F03",
                "mvp_has_board_members",
                "PASS" if board_rows > 0 else "WARN",
                str(board_rows),
                "> 0",
                "MVP should include board-member rows",
                "Review role taxonomy",
            ),
            ValidationCheck(
                "F04",
                "mvp_role_weight_not_null",
                severity_from_count(null_weight, fail_if_positive=True),
                str(null_weight),
                "0",
                "MVP rows should have role_weight populated",
                "Fix role taxonomy defaults",
            ),
        ]
    )

    if not mvp_dist.empty:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 5))
        top = mvp_dist.head(15)
        ax.barh(top["normalized_role"], top["row_count"])
        ax.set_title("Top normalized roles (MVP)")
        ax.invert_yaxis()
        save_chart(fig, "role_distribution_mvp.png", export_dir)

        group_df = (
            mvp_dist.groupby("role_group", as_index=False)["row_count"]
            .sum()
            .sort_values("row_count", ascending=True)
        )
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.barh(group_df["role_group"], group_df["row_count"], color="teal")
        ax.set_title("Role group distribution (MVP)")
        save_chart(fig, "role_group_distribution_mvp.png", export_dir)

    return combined


def section_g_filing_lag(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    lag_sql = """
        SELECT filing_lag_days
        FROM clean_insider_transactions
        WHERE include_mvp_alpha = 1 AND filing_lag_days IS NOT NULL
    """
    lags = pd.read_sql_query(lag_sql, conn)["filing_lag_days"]
    null_trans = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE transaction_date IS NULL",
        )
        or 0
    )
    null_filing = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions WHERE filing_date IS NULL"
        )
        or 0
    )
    neg_lag = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions WHERE filing_lag_days < 0"
        )
        or 0
    )
    mvp_neg = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE include_mvp_alpha = 1 AND filing_lag_days < 0",
        )
        or 0
    )

    summary = pd.DataFrame(
        [
            {"metric": "null_transaction_date", "value": null_trans},
            {"metric": "null_filing_date", "value": null_filing},
            {"metric": "negative_filing_lag_all", "value": neg_lag},
            {"metric": "negative_filing_lag_mvp", "value": mvp_neg},
            {"metric": "min", "value": lags.min() if len(lags) else None},
            {"metric": "p01", "value": lags.quantile(0.01) if len(lags) else None},
            {"metric": "median", "value": lags.median() if len(lags) else None},
            {"metric": "mean", "value": lags.mean() if len(lags) else None},
            {"metric": "p95", "value": lags.quantile(0.95) if len(lags) else None},
            {"metric": "p99", "value": lags.quantile(0.99) if len(lags) else None},
            {"metric": "max", "value": lags.max() if len(lags) else None},
        ]
    )

    median_lag = float(lags.median()) if len(lags) else 0.0
    max_median = float(config["max_reasonable_median_filing_lag"])
    extreme_lag = int(
        (lags > float(config.get("extreme_filing_lag_warn", 365))).sum()
        if len(lags)
        else 0
    )

    checks.extend(
        [
            ValidationCheck(
                "G01",
                "mvp_no_negative_filing_lag",
                severity_from_count(mvp_neg, fail_if_positive=True),
                str(mvp_neg),
                "0",
                "MVP rows must not have negative filing lag",
                "Review date parsing and clean build",
            ),
            ValidationCheck(
                "G02",
                "mvp_median_filing_lag_reasonable",
                "PASS" if median_lag <= max_median else "WARN",
                f"{median_lag:.2f}",
                f"<= {max_median}",
                "Median MVP filing lag should be reasonable",
                "Investigate delayed filings",
            ),
            ValidationCheck(
                "G03",
                "extreme_filing_lag_mvp",
                severity_from_count(extreme_lag, fail_if_positive=False, warn_if_positive=True),
                str(extreme_lag),
                f"0 (warn if > {config.get('extreme_filing_lag_warn', 365)} days)",
                "Very long filing lags in MVP",
                "Review outliers; may be data quality issues",
            ),
        ]
    )

    if len(lags) > 0:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 4))
        clip = lags.clip(upper=lags.quantile(0.99))
        ax.hist(clip, bins=50, color="steelblue", edgecolor="white")
        ax.set_xlabel("Filing lag (days)")
        ax.set_title("MVP filing lag distribution (99th pct clip)")
        save_chart(fig, "filing_lag_distribution_mvp.png", export_dir)

        year_sql = """
            SELECT SUBSTR(transaction_date, 1, 4) AS year,
                   AVG(filing_lag_days) AS avg_lag,
                   COUNT(*) AS rows
            FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND transaction_date IS NOT NULL
            GROUP BY year ORDER BY year
        """
        by_year = pd.read_sql_query(year_sql, conn)
        if not by_year.empty:
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot(by_year["year"], by_year["avg_lag"], marker="o")
            ax.set_xlabel("Year")
            ax.set_ylabel("Mean filing lag (days)")
            ax.set_title("MVP mean filing lag by year")
            plt.xticks(rotation=45, ha="right")
            save_chart(fig, "filing_lag_by_year_mvp.png", export_dir)

    return summary


def section_h_transaction_value(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    min_val = float(config["minimum_transaction_value"])
    extreme_val = float(config["extreme_transaction_value"])

    values = pd.read_sql_query(
        """
        SELECT transaction_value
        FROM clean_insider_transactions
        WHERE include_mvp_alpha = 1 AND transaction_value IS NOT NULL
        """,
        conn,
    )["transaction_value"]

    below_min = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE include_mvp_alpha = 1 AND (transaction_value < ? OR transaction_value IS NULL)",
            (min_val,),
        )
        or 0
    )
    above_extreme = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE include_mvp_alpha = 1 AND transaction_value > ?",
            (extreme_val,),
        )
        or 0
    )
    null_mvp = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE include_mvp_alpha = 1 AND transaction_value IS NULL",
        )
        or 0
    )

    summary = pd.DataFrame(
        [
            {"metric": "min", "value": values.min() if len(values) else None},
            {"metric": "p01", "value": values.quantile(0.01) if len(values) else None},
            {"metric": "median", "value": values.median() if len(values) else None},
            {"metric": "mean", "value": values.mean() if len(values) else None},
            {"metric": "p95", "value": values.quantile(0.95) if len(values) else None},
            {"metric": "p99", "value": values.quantile(0.99) if len(values) else None},
            {"metric": "max", "value": values.max() if len(values) else None},
            {"metric": "below_minimum_threshold", "value": below_min},
            {"metric": "above_extreme_threshold", "value": above_extreme},
            {"metric": "null_transaction_value_mvp", "value": null_mvp},
        ]
    )

    checks.extend(
        [
            ValidationCheck(
                "H01",
                "mvp_no_below_minimum_value",
                severity_from_count(below_min, fail_if_positive=True),
                str(below_min),
                "0",
                "No MVP row should be below minimum transaction value",
                "Fix include_mvp_alpha threshold logic",
            ),
            ValidationCheck(
                "H02",
                "mvp_no_null_transaction_value",
                severity_from_count(null_mvp, fail_if_positive=True),
                str(null_mvp),
                "0",
                "MVP rows must have transaction_value",
                "Fix value computation in clean build",
            ),
            ValidationCheck(
                "H03",
                "mvp_extreme_transaction_values",
                severity_from_count(above_extreme, fail_if_positive=False, warn_if_positive=True),
                str(above_extreme),
                f"0 (warn if > {extreme_val:,.0f})",
                "Extreme transaction values in MVP",
                "Review outliers and price parsing",
            ),
        ]
    )

    if len(values) > 0:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 4))
        log_vals = values[values > 0]
        ax.hist(log_vals.apply(math.log10), bins=50, color="darkorange")
        ax.set_xlabel("log10(transaction value)")
        ax.set_title("MVP transaction value distribution")
        save_chart(fig, "transaction_value_distribution_mvp.png", export_dir)

        year_sql = """
            SELECT SUBSTR(transaction_date, 1, 4) AS year,
                   SUM(transaction_value) AS total_value,
                   COUNT(*) AS rows
            FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND transaction_date IS NOT NULL
            GROUP BY year ORDER BY year
        """
        by_year = pd.read_sql_query(year_sql, conn)
        if not by_year.empty:
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.bar(by_year["year"], by_year["total_value"] / 1e9, color="darkorange")
            ax.set_ylabel("Total value ($ billions)")
            ax.set_title("MVP total transaction value by year")
            plt.xticks(rotation=45, ha="right")
            save_chart(fig, "transaction_value_by_year_mvp.png", export_dir)

    return summary


def section_i_duplicates(
    conn: sqlite3.Connection,
    checks: list[ValidationCheck],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    dup_tid = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT transaction_id FROM clean_insider_transactions
                GROUP BY transaction_id HAVING COUNT(*) > 1
            )
            """,
        )
        or 0
    )
    dup_acc_sk_cik = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT accession_number, nonderiv_trans_sk, insider_cik
                FROM clean_insider_transactions
                GROUP BY 1, 2, 3 HAVING COUNT(*) > 1
            )
            """,
        )
        or 0
    )
    dup_acc_sk = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT accession_number, nonderiv_trans_sk
                FROM clean_insider_transactions
                GROUP BY 1, 2 HAVING COUNT(*) > 1
            )
            """,
        )
        or 0
    )

    summary = pd.DataFrame(
        [
            {
                "duplicate_key": "transaction_id",
                "duplicate_groups": dup_tid,
                "expected": "none",
            },
            {
                "duplicate_key": "accession_number+nonderiv_trans_sk+insider_cik",
                "duplicate_groups": dup_acc_sk_cik,
                "expected": "none (unique row key)",
            },
            {
                "duplicate_key": "accession_number+nonderiv_trans_sk",
                "duplicate_groups": dup_acc_sk,
                "expected": "allowed (multi-owner expansion)",
            },
        ]
    )

    checks.append(
        ValidationCheck(
            "I01",
            "no_duplicate_transaction_id",
            severity_from_count(dup_tid, fail_if_positive=True),
            str(dup_tid),
            "0",
            "transaction_id must be unique",
            "Fix transaction_id hash logic in clean build",
        )
    )
    checks.append(
        ValidationCheck(
            "I02",
            "accession_sk_cik_unique",
            severity_from_count(dup_acc_sk_cik, fail_if_positive=True),
            str(dup_acc_sk_cik),
            "0",
            "Each transaction×owner combination should be unique",
            "Investigate clean build duplicates",
        )
    )

    samples = pd.read_sql_query(
        """
        SELECT * FROM clean_insider_transactions
        WHERE transaction_id IN (
            SELECT transaction_id FROM clean_insider_transactions
            GROUP BY transaction_id HAVING COUNT(*) > 1
            LIMIT 5
        )
        LIMIT 50
        """,
        conn,
    )
    return summary, samples


def section_j_yearly_activity(
    conn: sqlite3.Connection,
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    yearly = pd.read_sql_query(
        """
        SELECT
            CAST(SUBSTR(transaction_date, 1, 4) AS INTEGER) AS year,
            COUNT(*) AS mvp_rows,
            SUM(CASE WHEN transaction_code = 'P' THEN 1 ELSE 0 END) AS p_rows,
            SUM(CASE WHEN transaction_code = 'S' THEN 1 ELSE 0 END) AS s_rows,
            COUNT(DISTINCT ticker) AS unique_tickers,
            COUNT(DISTINCT insider_cik) AS unique_insiders,
            SUM(transaction_value) AS total_transaction_value,
            AVG(transaction_value) AS mean_transaction_value
        FROM clean_insider_transactions
        WHERE include_mvp_alpha = 1 AND transaction_date IS NOT NULL
        GROUP BY year
        ORDER BY year
        """,
        conn,
    )
    if not yearly.empty:
        yr_values = pd.read_sql_query(
            """
            SELECT SUBSTR(transaction_date, 1, 4) AS year, transaction_value
            FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1
              AND transaction_date IS NOT NULL
              AND transaction_value IS NOT NULL
            """,
            conn,
        )
        medians = (
            yr_values.groupby("year")["transaction_value"]
            .median()
            .reset_index()
            .rename(columns={"transaction_value": "median_transaction_value"})
        )
        medians["year"] = medians["year"].astype(int)
        yearly = yearly.merge(medians, on="year", how="left")

    years_present = set(yearly["year"].dropna().astype(int)) if not yearly.empty else set()
    expected_years = set(range(2006, 2027))
    missing = expected_years - years_present
    zero_years = yearly.loc[yearly["mvp_rows"] == 0, "year"].tolist() if not yearly.empty else []

    checks.append(
        ValidationCheck(
            "J01",
            "mvp_years_2006_2026_present",
            "WARN" if missing and missing != {2026} else ("PASS" if not missing else "WARN"),
            str(sorted(missing)) if missing else "none",
            "2006-2026",
            "Expected filing years in MVP activity",
            "Check ingestion coverage for missing years",
        )
    )
    if zero_years:
        checks.append(
            ValidationCheck(
                "J02",
                "mvp_zero_row_years",
                "WARN",
                str(zero_years),
                "> 0 per year",
                "Years with zero MVP rows",
                "Verify source data for those years",
            )
        )

    if not yearly.empty:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(yearly["year"], yearly["mvp_rows"], marker="o")
        ax.set_title("Yearly MVP row counts")
        ax.set_ylabel("Rows")
        save_chart(fig, "yearly_mvp_rows.png", export_dir)

        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(yearly["year"], yearly["p_rows"], label="P", marker="o")
        ax.plot(yearly["year"], yearly["s_rows"], label="S", marker="o")
        ax.legend()
        ax.set_title("Yearly MVP P vs S")
        save_chart(fig, "yearly_mvp_p_vs_s.png", export_dir)

    return yearly


def section_k_ticker_readiness(
    conn: sqlite3.Connection,
    checks: list[ValidationCheck],
    export_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    mvp_invalid_placeholder = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND is_invalid_ticker_placeholder = 1
            """,
        )
        or 0
    )
    mvp_multisymbol = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND is_multisymbol_ticker = 1
            """,
        )
        or 0
    )
    mvp_ticker_clean_null = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND (ticker_clean IS NULL OR TRIM(ticker_clean) = '')
            """,
        )
        or 0
    )
    distinct_tickers = int(
        scalar(
            conn,
            """
            SELECT COUNT(DISTINCT ticker_clean) FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND is_valid_ticker = 1
            """,
        )
        or 0
    )
    clean_invalid_placeholder = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions WHERE is_invalid_ticker_placeholder = 1",
        )
        or 0
    )
    clean_multisymbol = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions WHERE is_multisymbol_ticker = 1",
        )
        or 0
    )

    summary = pd.DataFrame(
        [
            {"metric": "mvp_invalid_ticker_placeholder_count", "value": mvp_invalid_placeholder},
            {"metric": "mvp_multisymbol_ticker_count", "value": mvp_multisymbol},
            {"metric": "mvp_ticker_clean_null_count", "value": mvp_ticker_clean_null},
            {"metric": "mvp_distinct_valid_tickers", "value": distinct_tickers},
            {"metric": "clean_invalid_ticker_placeholder_count", "value": clean_invalid_placeholder},
            {"metric": "clean_multisymbol_ticker_count", "value": clean_multisymbol},
        ]
    )

    checks.extend(
        [
            ValidationCheck(
                "K01",
                "mvp_invalid_ticker_placeholders",
                severity_from_count(mvp_invalid_placeholder, fail_if_positive=True),
                str(mvp_invalid_placeholder),
                "0",
                "MVP must not contain NONE/n/a style ticker placeholders",
                "Rebuild clean layer with ticker validation",
            ),
            ValidationCheck(
                "K02",
                "mvp_multisymbol_tickers",
                severity_from_count(mvp_multisymbol, fail_if_positive=True),
                str(mvp_multisymbol),
                "0",
                "MVP must not contain multi-symbol tickers",
                "Review ISSUERTRADINGSYMBOL parsing",
            ),
            ValidationCheck(
                "K03",
                "mvp_ticker_clean_null",
                severity_from_count(mvp_ticker_clean_null, fail_if_positive=True),
                str(mvp_ticker_clean_null),
                "0",
                "MVP rows must have ticker_clean populated",
                "Review ticker cleaning logic",
            ),
        ]
    )

    invalid_samples = pd.read_sql_query(
        """
        SELECT ticker_raw, ticker_clean, is_valid_ticker, is_invalid_ticker_placeholder,
               is_multisymbol_ticker, include_mvp_alpha, accession_number, transaction_value
        FROM clean_insider_transactions
        WHERE is_invalid_ticker_placeholder = 1 OR is_multisymbol_ticker = 1
        ORDER BY include_mvp_alpha DESC, transaction_value DESC
        LIMIT 500
        """,
        conn,
    )
    export_csv(invalid_samples, "invalid_ticker_samples.csv", export_dir)
    export_csv(summary, "ticker_quality_summary.csv", export_dir)

    top_rows = pd.read_sql_query(
        """
        SELECT ticker_clean AS ticker, COUNT(*) AS row_count
        FROM clean_insider_transactions
        WHERE include_mvp_alpha = 1 AND is_valid_ticker = 1
        GROUP BY ticker_clean ORDER BY row_count DESC LIMIT 50
        """,
        conn,
    )
    top_value = pd.read_sql_query(
        """
        SELECT ticker_clean AS ticker, SUM(transaction_value) AS total_transaction_value,
               COUNT(*) AS row_count
        FROM clean_insider_transactions
        WHERE include_mvp_alpha = 1 AND is_valid_ticker = 1
        GROUP BY ticker_clean ORDER BY total_transaction_value DESC LIMIT 50
        """,
        conn,
    )
    top_tickers = top_rows.merge(top_value, on="ticker", how="outer", suffixes=("_rows", "_value"))
    return summary, top_tickers


def section_l_stale_filings(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    stale_threshold = int(config.get("stale_filing_lag_days", 365))
    mvp_stale = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions "
            "WHERE include_mvp_alpha = 1 AND is_stale_filing = 1",
        )
        or 0
    )
    mvp_total = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions WHERE include_mvp_alpha = 1",
        )
        or 1
    )
    stale_pct = (mvp_stale / mvp_total * 100) if mvp_total else 0.0

    by_year = pd.read_sql_query(
        """
        SELECT CAST(SUBSTR(transaction_date, 1, 4) AS INTEGER) AS year,
               SUM(is_stale_filing) AS stale_rows,
               COUNT(*) AS mvp_rows,
               ROUND(100.0 * SUM(is_stale_filing) / COUNT(*), 4) AS stale_pct
        FROM clean_insider_transactions
        WHERE include_mvp_alpha = 1 AND transaction_date IS NOT NULL
        GROUP BY year ORDER BY year
        """,
        conn,
    )

    bucket_df = pd.read_sql_query(
        """
        SELECT filing_lag_bucket, COUNT(*) AS row_count
        FROM clean_insider_transactions
        WHERE include_mvp_alpha = 1
        GROUP BY filing_lag_bucket ORDER BY row_count DESC
        """,
        conn,
    )

    summary = pd.DataFrame(
        [
            {"metric": "stale_filing_lag_threshold_days", "value": stale_threshold},
            {"metric": "mvp_stale_filing_count", "value": mvp_stale},
            {"metric": "mvp_stale_filing_pct", "value": round(stale_pct, 4)},
        ]
    )
    export_csv(summary, "stale_filing_summary.csv", export_dir)
    if not bucket_df.empty:
        export_csv(bucket_df, "stale_filing_bucket_mvp.csv", export_dir)
    if not by_year.empty:
        export_csv(by_year, "stale_filing_by_year_mvp.csv", export_dir)

    checks.append(
        ValidationCheck(
            "L01",
            "mvp_stale_filing_count",
            severity_from_count(mvp_stale, fail_if_positive=False, warn_if_positive=True),
            str(mvp_stale),
            f"flagged (>{stale_threshold} days)",
            "Stale filings in MVP (flagged, not necessarily errors)",
            "Review stale_filing_summary.csv; set exclude_stale_filings_from_mvp if needed",
        )
    )
    return summary


def section_m_pre_source_dates(
    conn: sqlite3.Connection,
    checks: list[ValidationCheck],
    export_dir: Path,
) -> pd.DataFrame:
    pre_2006 = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND transaction_year IS NOT NULL AND transaction_year < 2006
            """,
        )
        or 0
    )
    pre_source = int(
        scalar(
            conn,
            "SELECT COUNT(*) FROM clean_insider_transactions WHERE is_pre_source_transaction_date = 1",
        )
        or 0
    )
    mvp_pre_source = int(
        scalar(
            conn,
            """
            SELECT COUNT(*) FROM clean_insider_transactions
            WHERE include_mvp_alpha = 1 AND is_pre_source_transaction_date = 1
            """,
        )
        or 0
    )

    summary = pd.DataFrame(
        [
            {"metric": "mvp_transaction_year_before_2006", "value": pre_2006},
            {"metric": "clean_pre_source_transaction_date_rows", "value": pre_source},
            {"metric": "mvp_pre_source_transaction_date_rows", "value": mvp_pre_source},
        ]
    )
    export_csv(summary, "pre_source_transaction_date_summary.csv", export_dir)

    sample_path = PROJECT_ROOT / "data" / "exports" / "cleaning" / "pre_source_transaction_date_sample.csv"
    checks.append(
        ValidationCheck(
            "M01",
            "pre_source_sample_export_exists",
            "PASS" if sample_path.exists() else "WARN",
            str(sample_path.exists()),
            "true",
            "Pre-source transaction date sample should be exported at build time",
            "Re-run build_clean_insider_transactions --reset",
        )
    )
    checks.append(
        ValidationCheck(
            "M02",
            "mvp_transaction_year_before_2006",
            severity_from_count(pre_2006, fail_if_positive=False, warn_if_positive=True),
            str(pre_2006),
            "review",
            "MVP rows with transaction_year < 2006 (may be valid historical dates)",
            "Review pre_source_transaction_date_summary.csv",
        )
    )
    checks.append(
        ValidationCheck(
            "M03",
            "pre_source_transaction_dates_flagged",
            "PASS",
            str(pre_source),
            "flagged",
            "Rows where transaction_year < source_year are explicitly flagged",
            "None",
        )
    )
    return summary


def section_n_unknown_role_refinement(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    checks: list[ValidationCheck],
    export_dir: Path,
) -> None:
    from src.analytics.unknown_role_refinement import analyze_unknown_roles_for_refinement

    candidates_path = export_dir / "unknown_role_refinement_candidates.csv"
    top_path = export_dir / "top_unknown_roles_for_mapping.csv"
    if not candidates_path.exists():
        analyze_unknown_roles_for_refinement(conn, export_dir)

    unknown_pct = float(
        scalar(
            conn,
            """
            SELECT 100.0 * SUM(CASE WHEN normalized_role = 'UNKNOWN' THEN 1 ELSE 0 END)
                / COUNT(*) FROM clean_insider_transactions WHERE include_mvp_alpha = 1
            """,
        )
        or 0
    )
    checks.append(
        ValidationCheck(
            "N01",
            "unknown_role_share_mvp",
            severity_from_pct(unknown_pct, float(config["max_unknown_role_pct_mvp_warn"])),
            f"{unknown_pct:.2f}%",
            f"< {config['max_unknown_role_pct_mvp_warn']}%",
            "UNKNOWN role share in MVP universe",
            "Use top_unknown_roles_for_mapping.csv to refine officer_title_map.csv",
        )
    )
    checks.append(
        ValidationCheck(
            "N02",
            "unknown_role_refinement_export_exists",
            "PASS" if candidates_path.exists() and top_path.exists() else "WARN",
            str(candidates_path.exists()),
            "true",
            "Unknown role refinement exports should exist",
            "Run analyze_unknown_roles_for_refinement or rebuild clean layer",
        )
    )


def run_validation(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    export_dir: Path,
) -> pd.DataFrame:
    ensure_export_dirs(export_dir)
    checks: list[ValidationCheck] = []

    row_summary = section_a_row_counts(conn, checks)
    export_csv(row_summary, "row_count_summary.csv", export_dir)

    violations = section_b_mvp_integrity(conn, config, checks)
    export_csv(violations, "mvp_integrity_violations.csv", export_dir)

    funnel = section_c_funnel(conn, config, checks, export_dir)
    export_csv(funnel, "filter_survival_funnel.csv", export_dir)

    section_d_transaction_codes(conn, checks, export_dir)
    section_e_security_types(conn, config, checks, export_dir)
    section_f_roles(conn, config, checks, export_dir)

    lag_summary = section_g_filing_lag(conn, config, checks, export_dir)
    export_csv(lag_summary, "filing_lag_clean_summary.csv", export_dir)

    value_summary = section_h_transaction_value(conn, config, checks, export_dir)
    export_csv(value_summary, "transaction_value_summary_mvp.csv", export_dir)

    dup_summary, dup_samples = section_i_duplicates(conn, checks)
    export_csv(dup_summary, "duplicate_checks.csv", export_dir)
    export_csv(dup_samples, "duplicate_samples.csv", export_dir)

    yearly = section_j_yearly_activity(conn, checks, export_dir)
    export_csv(yearly, "yearly_mvp_activity.csv", export_dir)

    section_k_ticker_readiness(conn, checks, export_dir)
    top_tickers = pd.read_sql_query(
        """
        SELECT ticker_clean AS ticker, COUNT(*) AS row_count
        FROM clean_insider_transactions
        WHERE include_mvp_alpha = 1 AND is_valid_ticker = 1
        GROUP BY ticker_clean ORDER BY row_count DESC LIMIT 50
        """,
        conn,
    )
    export_csv(top_tickers, "top_tickers_mvp.csv", export_dir)

    section_l_stale_filings(conn, config, checks, export_dir)
    section_m_pre_source_dates(conn, checks, export_dir)
    section_n_unknown_role_refinement(conn, config, checks, export_dir)

    summary_df = checks_to_dataframe(checks)
    export_csv(summary_df, "validation_summary.csv", export_dir)

    fail_count = (summary_df["severity"] == "FAIL").sum()
    warn_count = (summary_df["severity"] == "WARN").sum()
    logger.info(
        "Validation complete: %s checks (%s FAIL, %s WARN, %s PASS)",
        len(summary_df),
        fail_count,
        warn_count,
        (summary_df["severity"] == "PASS").sum(),
    )
    return summary_df


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate clean_insider_transactions (read-only QA).",
    )
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out-dir", type=Path, default=EXPORT_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    if not args.db_path.exists():
        logger.error("Database not found: %s", args.db_path)
        return 1

    config = load_validation_config(args.config)
    conn = get_connection(args.db_path)
    try:
        if not table_exists(conn, "clean_insider_transactions"):
            logger.error("clean_insider_transactions not found. Run Phase 3 build first.")
            return 1
        run_validation(conn, config, args.out_dir)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
