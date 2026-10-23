"""Pilot QA with machine-readable per-security and summary reports."""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path
import sqlite3


def validate(db_path: Path, out_dir: Path) -> list[dict[str, str]]:
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    checks: list[dict[str, str]] = []

    def add(name: str, count: int, level: str = "FAIL") -> None:
        checks.append({"check": name, "severity": level if count else "PASS", "count": str(count)})

    try:
        add("duplicate_security_provider_date", conn.execute("""SELECT COUNT(*) FROM
            (SELECT security_id,provider,trading_date FROM pilot_prices
             GROUP BY 1,2,3 HAVING COUNT(*)>1)""").fetchone()[0])
        add("invalid_raw_ohlc", conn.execute("""SELECT COUNT(*) FROM pilot_prices WHERE
            low>high OR open<low OR open>high OR close<low OR close>high""").fetchone()[0])
        add("nonpositive_prices", conn.execute("""SELECT COUNT(*) FROM pilot_prices WHERE
            close<=0 OR adj_close<=0 OR open<=0 OR high<=0 OR low<=0""").fetchone()[0])
        add("invalid_volume", conn.execute("""SELECT COUNT(*) FROM pilot_prices WHERE
            volume<0 OR adj_volume<0""").fetchone()[0])
        add("identity_mismatch", conn.execute("""SELECT COUNT(*) FROM pilot_prices p
            LEFT JOIN pilot_securities s ON p.security_id=s.security_id
            WHERE s.security_id IS NULL OR p.ticker<>s.provider_ticker""").fetchone()[0])
        add("wrong_adjustment_basis", conn.execute("""SELECT COUNT(*) FROM pilot_prices WHERE
            (provider='tiingo' AND adjustment_basis<>'TIINGO_DIVIDEND_AND_SPLIT_ADJUSTED') OR
            (provider='yahoo' AND adjustment_basis<>'YAHOO_ADJ_CLOSE_VENDOR') OR
            provider NOT IN ('tiingo','yahoo')""").fetchone()[0])
        add("corporate_action_inconsistency", conn.execute("""SELECT COUNT(*) FROM pilot_prices WHERE
            (provider='tiingo' AND split_factor<=0) OR split_factor<0 OR dividend_cash<0""").fetchone()[0])
        add("missing_yahoo_corporate_action_fields", conn.execute("""SELECT COUNT(*) FROM pilot_prices WHERE
            provider='yahoo' AND (split_factor IS NULL OR dividend_cash IS NULL)""").fetchone()[0])
        add("price_outside_mapping_interval", conn.execute("""SELECT COUNT(*) FROM pilot_prices p
            JOIN pilot_securities s ON s.security_id=p.security_id
            WHERE p.trading_date<s.valid_from OR p.trading_date>s.valid_to""").fetchone()[0])
        add("failed_or_partial_requests", conn.execute("""SELECT COUNT(*) FROM pilot_requests WHERE
            status IN ('FAILED','PARTIAL')""").fetchone()[0])
        add("no_data_requests", conn.execute("""SELECT COUNT(*) FROM pilot_requests WHERE
            status='NO_DATA'""").fetchone()[0])
        add("unknown_or_mismatched_identity", conn.execute("""SELECT COUNT(*) FROM pilot_requests WHERE
            status IN ('UNKNOWN_SYMBOL','IDENTITY_REVIEW')""").fetchone()[0], "FAIL")
        add("unreviewed_identity", conn.execute("""SELECT COUNT(*) FROM pilot_securities WHERE
            review_status<>'APPROVED'""").fetchone()[0])
        security_columns = {r[1] for r in conn.execute("PRAGMA table_info(pilot_securities)")}
        if "identity_evidence" in security_columns:
            add("missing_identity_evidence", conn.execute("""SELECT COUNT(*) FROM pilot_securities WHERE
                identity_evidence IS NULL OR TRIM(identity_evidence)=''""").fetchone()[0])
            add("known_ticker_reuse", conn.execute("""SELECT COUNT(*) FROM pilot_securities WHERE
                known_ticker_reuse_issuer_count<>1""").fetchone()[0])
            add("ticker_change_identity_review", conn.execute("""SELECT COUNT(*) FROM pilot_securities WHERE
                ticker_change_flag=1""").fetchone()[0], "WARN")
            add("inactive_identity_review", conn.execute("""SELECT COUNT(*) FROM pilot_securities WHERE
                inactive_observation_flag=1""").fetchone()[0], "WARN")
        details = []
        for s in conn.execute("SELECT * FROM pilot_securities"):
            prices = conn.execute("""SELECT trading_date, close, adj_close, split_factor,
                dividend_cash FROM pilot_prices WHERE security_id=? ORDER BY trading_date""",
                (s["security_id"],)).fetchall()
            requests = conn.execute("""SELECT requested_start,requested_end,status,row_count
                FROM pilot_requests WHERE security_id=?""", (s["security_id"],)).fetchall()
            dates = [date.fromisoformat(p["trading_date"]) for p in prices]
            gaps = sum((b-a).days > 4 for a,b in zip(dates, dates[1:]))
            jumps = sum(abs(p["adj_close"] / q["adj_close"] - 1) > .5
                for q,p in zip(prices, prices[1:]) if q["adj_close"] and p["adj_close"])
            unexplained_jumps = sum(abs(p["adj_close"] / q["adj_close"] - 1) > .5 and
                not ((p["split_factor"] or 0) > 0 or (p["dividend_cash"] or 0) > 0)
                for q,p in zip(prices, prices[1:]) if q["adj_close"] and p["adj_close"])
            requested = sum(max(1, int((date.fromisoformat(r["requested_end"]) -
                         date.fromisoformat(r["requested_start"])).days * 5 / 7)) for r in requests)
            details.append({"security_id": s["security_id"], "ticker": s["ticker"],
                "price_rows": len(prices), "earliest": dates[0].isoformat() if dates else "",
                "latest": dates[-1].isoformat() if dates else "",
                "calendar_gaps_gt_4_days": gaps, "adjusted_jumps_gt_50pct": jumps,
                "large_jumps_without_action": unexplained_jumps,
                "rough_business_day_coverage_pct": round(100*len(prices)/requested, 1) if requested else 0,
                "possible_inactive": (bool(s["inactive_observation_flag"])
                    if "inactive_observation_flag" in security_columns else
                    s["selection_reason"] == "possible_inactive"),
                "has_split": any((p["split_factor"] or 0) not in (0, 1) for p in prices),
                "has_dividend": any((p["dividend_cash"] or 0)>0 for p in prices),
                "has_5d": len(prices)>=6, "has_20d": len(prices)>=21,
                "has_60d": len(prices)>=61, "has_120d": len(prices)>=121})
        add("missing_price_series", sum(not d["price_rows"] for d in details))
        add("calendar_gaps", sum(d["calendar_gaps_gt_4_days"] for d in details), "WARN")
        add("suspicious_adjusted_jumps", sum(d["adjusted_jumps_gt_50pct"] for d in details), "WARN")
        add("large_jumps_without_action", sum(d["large_jumps_without_action"] for d in details), "WARN")
        add("no_corporate_actions_observed", sum(not d["has_split"] and not d["has_dividend"]
            for d in details if d["price_rows"]), "WARN")
        add("insufficient_rough_coverage", sum(d["rough_business_day_coverage_pct"]<80 for d in details), "WARN")
        for n in (5,20,60,120):
            add(f"missing_{n}d_series_length", sum(not d[f"has_{n}d"] for d in details), "WARN")
    finally:
        conn.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, rows in (("pilot_validation_summary.csv", checks),
                           ("pilot_security_diagnostics.csv", details)):
        with (out_dir/filename).open("w", newline="", encoding="utf-8") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    return checks


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pilot-db", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    a = p.parse_args()
    results = validate(a.pilot_db, a.out_dir)
    print({level: sum(c["severity"] == level for c in results) for level in ("PASS","WARN","FAIL")})
    raise SystemExit(1 if any(c["severity"] == "FAIL" for c in results) else 0)


if __name__ == "__main__":
    main()
