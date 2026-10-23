"""Read-only manifest identity audit against the small Phase 5.1 mapping table."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sqlite3


def audit(manifest: Path, source_db: Path, output: Path) -> list[dict[str, str]]:
    with manifest.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    conn = sqlite3.connect(f"file:{source_db.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    report = []
    try:
        for row in rows:
            mapping = conn.execute("SELECT * FROM security_identifier_mapping WHERE mapping_id=?",
                                   (row["security_id"],)).fetchone()
            if mapping is None:
                raise ValueError(f"Mapping ID missing from source: {row['security_id']}")
            reused = conn.execute("""SELECT COUNT(DISTINCT issuer_cik) FROM security_identifier_mapping
                WHERE ticker_clean=?""", (row["ticker"],)).fetchone()[0]
            match = all(str(mapping[key]) == row[field] for key, field in (
                ("issuer_cik", "issuer_cik"), ("market_data_symbol", "ticker"),
                ("valid_from", "valid_from"), ("valid_to", "valid_to")))
            risks = []
            if not match:
                risks.append("MANIFEST_SOURCE_MISMATCH")
            if mapping["is_ambiguous"]:
                risks.append("AMBIGUOUS_SOURCE_MAPPING")
            if reused > 1 or mapping["is_reused_ticker"]:
                risks.append("KNOWN_TICKER_REUSE")
            if mapping["is_ticker_change"]:
                risks.append("TICKER_CHANGE")
            if mapping["is_delisted"]:
                risks.append("INACTIVE_TICKER_UNVERIFIED_DELISTING")
            if not mapping["security_class"]:
                risks.append("SHARE_CLASS_UNVERIFIED")
            if not row.get("provider_security_id"):
                risks.append("PROVIDER_SECURITY_ID_MISSING")
            if not row.get("verified_provider_name"):
                risks.append("PROVIDER_NAME_UNVERIFIED")
            report.append({
                "security_id": row["security_id"], "issuer_cik": row["issuer_cik"],
                "ticker": row["ticker"], "valid_from": row["valid_from"],
                "valid_to": row["valid_to"], "mapping_confidence": mapping["mapping_confidence"],
                "mapping_method": mapping["mapping_method"], "security_title": mapping["security_title"] or "",
                "security_class": mapping["security_class"] or "",
                "known_ticker_reuse_issuer_count": str(reused),
                "review_status": row.get("review_status", ""),
                "identity_risks": ";".join(risks),
            })
    finally:
        conn.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report[0]) if report else [])
        writer.writeheader()
        writer.writerows(report)
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--source-db", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    rows = audit(a.manifest, a.source_db, a.out)
    print(f"Audited {len(rows)} manifest identities; {sum('KNOWN_TICKER_REUSE' in r['identity_risks'] for r in rows)} have known ticker reuse")


if __name__ == "__main__":
    main()
