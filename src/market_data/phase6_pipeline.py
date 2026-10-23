"""Offline Phase 6 SEC event construction and research diagnostics.

No network access is performed. Real return calculation fails closed unless a
separately validated adjusted total-return source is supplied in a later phase.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.market_data.phase6_engine import (
    HORIZONS, UsEquityCalendar, add_lag_fields, aggregate_issuer_sessions,
    construct_filing_events, flag_overlaps, stable_id,
)

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "processed" / "insider_research.db"
EDGAR_METADATA = ROOT / "data" / "exports" / "market_data" / "edgar_filing_event_metadata.csv"
ARCHIVE_SUMMARY = ROOT / "data" / "exports" / "market_data" / "archive_2017" / "summary.json"
OUT = ROOT / "data" / "exports" / "research" / "phase_6"

TRANSACTION_COLUMNS = [
    "transaction_id", "accession_number", "issuer_cik", "ticker_clean", "insider_cik",
    "normalized_role", "role_group", "transaction_date", "filing_date", "document_type",
    "is_amendment", "transaction_code", "transaction_category", "signal_direction",
    "acquired_disposed_code", "shares", "price_per_share", "transaction_value",
    "is_purchase", "is_sale", "is_open_market", "include_mvp_alpha",
]


def read_transactions(db_path: Path, accessions: list[str]) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    frames = []
    try:
        for start in range(0, len(accessions), 500):
            chunk = accessions[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            query = f"SELECT {','.join(TRANSACTION_COLUMNS)} FROM clean_insider_transactions WHERE accession_number IN ({placeholders})"
            frames.append(pd.read_sql_query(query, conn, params=chunk))
    finally:
        conn.close()
    result = pd.concat(frames, ignore_index=True)
    if result["transaction_id"].duplicated().any():
        raise ValueError("Duplicate transaction identifiers in pinned cohort")
    observed = set(result["accession_number"])
    missing = sorted(set(accessions) - observed)
    if missing:
        raise ValueError(f"Pinned accessions without transaction rows: {missing[:5]}")
    return result


def describe(series: pd.Series) -> dict[str, float | int | None]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return {"count": 0, "missing": int(series.isna().sum())}
    return {
        "count": int(len(values)), "missing": int(series.isna().sum()), "negative": int((values < 0).sum()),
        "mean": float(values.mean()), "median": float(values.median()), "std": float(values.std()),
        **{f"p{int(q * 100)}": float(values.quantile(q)) for q in (.05, .25, .75, .90, .95, .99)},
        "maximum": float(values.max()),
    }


def lag_group_summary(frame: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    rows = []
    for keys, group in frame.groupby(groups, dropna=False, sort=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        row = dict(zip(groups, keys))
        row.update(describe(group["business_days_to_disclose"]))
        rows.append(row)
    return pd.DataFrame(rows)


def write_charts(frame: pd.DataFrame, out: Path) -> list[str]:
    chart_dir = out / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    created = []
    values = frame.loc[frame["calendar_days_to_disclose"].between(-5, 30), "calendar_days_to_disclose"]
    fig, ax = plt.subplots(figsize=(9, 5)); ax.hist(values, bins=36); ax.set(title="Filing lag (focused range)", xlabel="Calendar days", ylabel="Transactions")
    path = chart_dir / "filing_lag_histogram.png"; fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig); created.append(str(path.relative_to(ROOT)))
    sorted_values = np.sort(frame["calendar_days_to_disclose"].dropna().to_numpy())
    fig, ax = plt.subplots(figsize=(9, 5)); ax.plot(sorted_values, np.arange(1, len(sorted_values)+1)/len(sorted_values)); ax.set(title="Filing lag empirical CDF", xlabel="Calendar days", ylabel="Cumulative probability"); ax.set_xscale("symlog")
    path = chart_dir / "filing_lag_ecdf.png"; fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig); created.append(str(path.relative_to(ROOT)))
    fig, ax = plt.subplots(figsize=(9, 5)); frame.boxplot(column="calendar_days_to_disclose", by="ticker_clean", ax=ax, showfliers=False); ax.set(title="Filing lag by ticker", xlabel="Ticker", ylabel="Calendar days"); fig.suptitle("")
    path = chart_dir / "filing_lag_by_ticker.png"; fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig); created.append(str(path.relative_to(ROOT)))
    annual = frame.assign(year=frame["filing_date"].str[:4]).groupby("year")["business_days_to_disclose"].median()
    fig, ax = plt.subplots(figsize=(9, 5)); annual.plot(ax=ax); ax.set(title="Annual median filing lag", xlabel="Filing year", ylabel="SEC business days")
    path = chart_dir / "annual_lag_trend.png"; fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig); created.append(str(path.relative_to(ROOT)))
    return created


def run(db_path: Path = DB, output: Path = OUT) -> dict:
    metadata = pd.read_csv(EDGAR_METADATA, dtype=str, keep_default_na=False)
    if len(metadata) != 6067 or set(metadata["verification_status"]) != {"VERIFIED_RAW"}:
        raise ValueError("Phase 6 requires exactly 6,067 VERIFIED_RAW filing metadata rows")
    transactions = read_transactions(db_path, metadata["accession_number"].tolist())
    if len(transactions) != 33986:
        raise ValueError(f"Expected 33,986 pinned transaction rows, found {len(transactions)}")
    transactions = add_lag_fields(transactions)
    eligible_value = (transactions["is_open_market"].eq(1) & transactions["shares"].gt(0)
                      & transactions["price_per_share"].gt(0) & transactions["transaction_value"].notna())
    transactions["purchase_value"] = np.where(eligible_value & transactions["is_purchase"].eq(1), transactions["transaction_value"], 0.0)
    transactions["sale_value"] = np.where(eligible_value & transactions["is_sale"].eq(1), transactions["transaction_value"], 0.0)
    transactions["signed_value"] = transactions["purchase_value"] - transactions["sale_value"]
    transactions["event_id"] = transactions["transaction_id"].map(lambda value: stable_id("transaction", value))
    transactions["event_aggregation_level"] = "TRANSACTION"

    cal = UsEquityCalendar()
    filings = construct_filing_events(transactions, metadata, cal)
    if len(filings) != 6067:
        raise ValueError("Filing event count does not reconcile to pinned accessions")
    issuer_sessions = aggregate_issuer_sessions(filings)
    for horizon in HORIZONS:
        issuer_sessions[f"overlapping_window_{horizon}d"] = flag_overlaps(issuer_sessions, horizon, cal)

    output.mkdir(parents=True, exist_ok=True)
    transactions.to_csv(output / "transaction_events.csv", index=False)
    filings.to_csv(output / "filing_events.csv", index=False)
    issuer_sessions.to_csv(output / "issuer_session_events.csv", index=False)
    exceptions = transactions.loc[transactions["lag_quality_code"] != "OK"]
    exceptions.to_csv(output / "filing_lag_quality_exceptions.csv", index=False)
    lag_group_summary(transactions, ["ticker_clean", "filing_date"]).to_csv(output / "filing_lag_by_ticker_and_date.csv", index=False)
    lag_group_summary(transactions.assign(filing_year=transactions["filing_date"].str[:4]), ["ticker_clean", "filing_year"]).to_csv(output / "filing_lag_by_ticker_and_year.csv", index=False)
    lag_group_summary(transactions, ["transaction_code", "role_group", "acquired_disposed_code", "document_type"]).to_csv(output / "filing_lag_distributions.csv", index=False)
    transactions.assign(filing_weekday=pd.to_datetime(transactions["filing_date"]).dt.day_name()).groupby(
        "filing_weekday").size().rename("transaction_rows").reset_index().to_csv(
            output / "filings_by_weekday.csv", index=False)
    acceptance_local = pd.to_datetime(metadata["acceptance_datetime_new_york"], utc=True).dt.tz_convert("America/New_York")
    pd.DataFrame({"acceptance_hour_new_york": acceptance_local.dt.hour}).groupby(
        "acceptance_hour_new_york").size().rename("filings").reset_index().to_csv(
            output / "acceptance_time_distribution.csv", index=False)
    transactions.groupby(["transaction_category", "filing_lag_bucket_phase6"]).size().rename(
        "transaction_rows").reset_index().to_csv(output / "business_day_lag_by_transaction_category.csv", index=False)
    filings[["accession_number", "filing_date", "acceptance_datetime_new_york", "entry_session",
             "acceptance_entry_session", "acceptance_timing_class", "timing_policy_differs"]].to_csv(output / "event_timing_comparison.csv", index=False)
    filings.groupby(["issuer_cik", "entry_session"]).size().rename("filing_count").reset_index().to_csv(output / "event_aggregation_duplicate_report.csv", index=False)
    overlap_rows = [{"horizon_sessions": h, "events_flagged": int(issuer_sessions[f"overlapping_window_{h}d"].sum()),
                     "events_total": len(issuer_sessions)} for h in HORIZONS]
    pd.DataFrame(overlap_rows).to_csv(output / "event_overlap_report.csv", index=False)

    empty_return_columns = ["event_id", "status", "blocking_reason"]
    pd.DataFrame(columns=empty_return_columns).to_csv(output / "forward_returns.csv", index=False)
    pd.DataFrame(columns=empty_return_columns).to_csv(output / "benchmark_relative_returns.csv", index=False)
    coverage = pd.DataFrame([{"price_source": "archive_2017", "status": "BLOCKED",
                              "reason": "UNKNOWN adjustment semantics; owner acceptance forbids return/alpha computation; no SPY or sector series"}])
    coverage.to_csv(output / "return_coverage_report.csv", index=False)
    coverage.to_csv(output / "benchmark_coverage_report.csv", index=False)
    coverage.to_csv(output / "missing_price_and_censoring_report.csv", index=False)
    charts = write_charts(transactions, output)

    archive = json.loads(ARCHIVE_SUMMARY.read_text(encoding="utf-8"))
    quality = {
        "transaction_rows": len(transactions), "unique_transaction_ids": int(transactions["transaction_id"].nunique()),
        "unique_accessions": int(transactions["accession_number"].nunique()),
        "filing_events": len(filings), "issuer_session_events": len(issuer_sessions),
        "negative_lag_rows": int((transactions["calendar_days_to_disclose"] < 0).sum()),
        "missing_transaction_dates": int(transactions["transaction_date"].isna().sum()),
        "missing_filing_dates": int(transactions["filing_date"].isna().sum()),
        "amendment_rows": int(transactions["is_amendment"].sum()),
        "timing_policy_differences": int(filings["timing_policy_differs"].sum()),
        "lag_calendar_days": describe(transactions["calendar_days_to_disclose"]),
        "lag_sec_business_days": describe(transactions["business_days_to_disclose"]),
        "archive_validation_status": archive["status"],
        "market_return_status": "BLOCKED_REAL_DATA",
        "charts": charts,
    }
    (output / "filing_lag_summary.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")
    (output / "data_quality_report.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")
    methodology = {"event_engine": "READY", "real_returns": "BLOCKED", "reason": coverage.iloc[0]["reason"],
                   "entry_policy": "NEXT_SESSION_AFTER_FILING_DATE_CLOSE", "horizons": list(HORIZONS)}
    (output / "methodology_and_assumptions.json").write_text(json.dumps(methodology, indent=2) + "\n", encoding="utf-8")
    return quality


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DB)
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args()
    print(json.dumps(run(args.database, args.output), indent=2))


if __name__ == "__main__":
    main()
