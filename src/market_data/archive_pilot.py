"""Offline, fail-closed importer for a manually supplied historical price archive."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
import sqlite3
import zipfile

CUTOFF = date(2017, 11, 10)
QA = (
    ("P5.1-QA-001", "72e14057575f575119edc8af1569a952e37bfe49295d31fcccbb8b8955b90a32", None, 10),
    ("P5.1-QA-002", "f479129d0cdb7cd3b85074e5ce3ee2e4f7a2fcd78f13eea5963b45cf591a30b1", "2019-06-17", 14),
    ("P5.1-QA-003", "d67a3811417f5d2841ecde9440e9851fb7dbcb61dc09cf499cac46756b798097", "2025-03-28", 57),
)
QA_ISSUER = {"P5.1-QA-001": "0001065280", "P5.1-QA-002": "0001024795",
             "P5.1-QA-003": "0001769628"}
SCHEMA = """
CREATE TABLE source_files(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
                            archive_sha256 TEXT, inspected_header TEXT NOT NULL);
CREATE TABLE provenance(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE securities(security_id TEXT PRIMARY KEY, issuer_cik TEXT NOT NULL, ticker TEXT NOT NULL,
 mapping_id TEXT NOT NULL, security_class TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
 identity_status TEXT NOT NULL, identity_evidence TEXT NOT NULL);
CREATE TABLE bars(security_id TEXT NOT NULL, trading_date TEXT NOT NULL, open REAL NOT NULL,
 high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
 source_date_text TEXT NOT NULL, open_source_text TEXT NOT NULL, high_source_text TEXT NOT NULL,
 low_source_text TEXT NOT NULL, close_source_text TEXT NOT NULL, volume_source_text TEXT NOT NULL,
 source_file TEXT NOT NULL, source_row INTEGER NOT NULL, adjustment_semantics TEXT NOT NULL,
 PRIMARY KEY(security_id,trading_date));
CREATE TABLE findings(security_id TEXT, trading_date TEXT, code TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE qa_quarantine(transaction_id TEXT PRIMARY KEY, qa_id TEXT NOT NULL, mapping_id TEXT NOT NULL);
CREATE TABLE cohort(security_id TEXT NOT NULL, year INTEGER NOT NULL, transaction_code TEXT NOT NULL,
 total INTEGER NOT NULL, qa_excluded INTEGER NOT NULL, price_window_covered INTEGER NOT NULL,
 PRIMARY KEY(security_id,year,transaction_code));
CREATE TABLE security_coverage(security_id TEXT PRIMARY KEY, bar_count INTEGER NOT NULL,
 first_date TEXT, last_date TEXT, finding_count INTEGER NOT NULL);
CREATE TABLE year_coverage(security_id TEXT NOT NULL, year INTEGER NOT NULL, bar_count INTEGER NOT NULL,
 first_date TEXT NOT NULL, last_date TEXT NOT NULL, PRIMARY KEY(security_id,year));
"""
REQUIRED = {"date", "open", "high", "low", "close", "volume"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def members(source: Path):
    """Yield safe CSV members without extracting or altering the source."""
    if source.is_dir():
        for path in sorted(p for p in source.rglob("*") if p.suffix.lower() in (".csv", ".txt")):
            if path.is_symlink():
                raise ValueError(f"Symlink forbidden: {path}")
            yield path.relative_to(source).as_posix(), path.read_bytes()
    elif source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                name = PurePosixPath(info.filename)
                if info.is_dir() or name.suffix.lower() not in (".csv", ".txt"):
                    continue
                if name.is_absolute() or ".." in name.parts or info.file_size > 250_000_000:
                    raise ValueError(f"Unsafe archive member: {info.filename}")
                yield name.as_posix(), archive.read(info)
    else:
        raise ValueError("Supply an existing extracted directory or ZIP archive; no downloads occur")


def csv_content(raw: bytes):
    handle = io.StringIO(raw.decode("utf-8-sig"), newline="")
    reader = csv.DictReader(handle)
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError("Missing or duplicate CSV header")
    return reader


def inspect(source: Path) -> dict:
    files = []
    for name, raw in members(source):
        header = csv_content(raw).fieldnames if raw else []
        files.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "header": header})
    if not files:
        raise ValueError("No CSV files found")
    return {"source": str(source.resolve()), "archive_sha256": sha256_file(source) if source.is_file() else None,
            "files": files, "headers": sorted({tuple(f["header"]) for f in files if f["header"]}),
            "empty_files": sum(not f["header"] for f in files)}


def load_manifest(path: Path, ro: sqlite3.Connection) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    needed = {"security_id", "issuer_cik", "ticker", "mapping_id", "security_class",
              "valid_from", "valid_to", "identity_status", "identity_evidence"}
    if not rows or not needed.issubset(rows[0]):
        raise ValueError(f"Manifest requires {sorted(needed)}")
    seen = set()
    for row in rows:
        if row["security_id"] in seen or row["identity_status"] != "APPROVED" or not row["identity_evidence"].strip():
            raise ValueError("Duplicate, unapproved, or unevidenced security; approval is never inferred")
        seen.add(row["security_id"])
        start, end = date.fromisoformat(row["valid_from"]), date.fromisoformat(row["valid_to"])
        if start > end or start > CUTOFF:
            raise ValueError("Invalid or post-cutoff mapping interval")
        original = ro.execute("SELECT issuer_cik,market_data_symbol,security_class,valid_from,valid_to,"
                              "is_ambiguous,is_reused_ticker,requires_review FROM security_identifier_mapping "
                              "WHERE mapping_id=?", (row["mapping_id"],)).fetchone()
        if not original or original[0:2] != (row["issuer_cik"], row["ticker"]):
            raise ValueError("Manifest identity does not match production mapping")
        if not row["security_class"].strip() or (original[2] and original[2] != row["security_class"]):
            raise ValueError("Security class must be explicitly evidenced and nonconflicting")
        if start < date.fromisoformat(original[3]) or end > date.fromisoformat(original[4]):
            raise ValueError("Manifest extends beyond historical mapping interval")
        if any(original[5:]):
            raise ValueError("Ambiguous/reused/review-required production mapping")
        conflicts = ro.execute(
            "SELECT COUNT(*) FROM security_identifier_mapping WHERE market_data_symbol=? "
            "AND mapping_id<>? AND valid_from<=? AND valid_to>=? "
            "AND (issuer_cik<>? OR (security_class IS NOT NULL AND security_class<>?))",
            (row["ticker"], row["mapping_id"], row["valid_to"], row["valid_from"],
             row["issuer_cik"], row["security_class"]),
        ).fetchone()[0]
        if conflicts:
            raise ValueError("Conflicting issuer/share class uses ticker during selected interval")
    for left in rows:
        for right in rows:
            if left is right or left["ticker"] != right["ticker"]:
                continue
            if left["issuer_cik"] != right["issuer_cik"] or left["security_class"] != right["security_class"]:
                raise ValueError("Ticker reuse or share-class conflict in manifest")
            if max(left["valid_from"], right["valid_from"]) <= min(left["valid_to"], right["valid_to"]):
                raise ValueError("Overlapping ticker intervals")
    return rows


def quarantine(ro: sqlite3.Connection, pilot: sqlite3.Connection) -> set[str]:
    blocked = set()
    has_issuer = any(r[1] == "issuer_cik" for r in ro.execute("PRAGMA table_info(transaction_market_mapping)"))
    for qa_id, mapping_id, before, expected in QA:
        sql = "SELECT transaction_id FROM transaction_market_mapping WHERE mapping_id=?"
        params = [mapping_id]
        if has_issuer:
            sql += " AND issuer_cik=?"
            params.append(QA_ISSUER[qa_id])
        if before:
            sql += " AND transaction_date<?"
            params.append(before)
        found = [r[0] for r in ro.execute(sql, params)]
        if len(found) != expected:
            raise ValueError(f"{qa_id} quarantine drift: expected {expected}, found {len(found)}")
        for tx in found:
            blocked.add(tx)
            pilot.execute("INSERT INTO qa_quarantine VALUES (?,?,?)", (tx, qa_id, mapping_id))
    if len(blocked) != 81:
        raise ValueError("QA quarantine is not exactly 81 distinct transactions")
    return blocked


def run(source: Path, schema_path: Path, manifest: Path, source_db: Path, pilot_db: Path,
        rights_record: Path) -> dict:
    for p in (source, schema_path, manifest, source_db, rights_record):
        if not p.exists():
            raise ValueError(f"Missing required input: {p}")
    if pilot_db.exists():
        raise ValueError("Pilot DB already exists; refusing overwrite")
    if pilot_db.resolve() == source_db.resolve():
        raise ValueError("Pilot DB cannot be production DB")
    spec = json.loads(schema_path.read_text(encoding="utf-8"))
    review = json.loads(rights_record.read_text(encoding="utf-8"))
    if not {"columns", "observed_headers", "price_basis", "methodology_evidence"}.issubset(spec):
        raise ValueError("Schema review must record observed headers, explicit column map and price basis")
    if set(spec["columns"]) != REQUIRED or len(set(spec["columns"].values())) != len(REQUIRED):
        raise ValueError("Explicit one-to-one Date/OHLCV mapping required")
    if spec["price_basis"] not in ("RAW", "ADJUSTED", "UNKNOWN"):
        raise ValueError("Invalid price basis")
    if spec["price_basis"] != "UNKNOWN" and not spec["methodology_evidence"].strip():
        raise ValueError("Price basis requires methodology evidence")
    if not {"source_url", "license_label", "provenance_limitations", "reviewed_at"}.issubset(review):
        raise ValueError("Rights/provenance record incomplete")
    scan = inspect(source)
    if sorted([list(x) for x in scan["headers"]]) != sorted(spec["observed_headers"]):
        raise ValueError("Actual CSV headers differ from separately inspected schema")
    ro = sqlite3.connect(f"file:{source_db.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        securities = load_manifest(manifest, ro)
        by_ticker = defaultdict(list)
        for row in securities:
            by_ticker[row["ticker"]].append(row)
        pilot_db.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(pilot_db)
        try:
            db.executescript(SCHEMA)
            blocked = quarantine(ro, db)
            for key, value in {**review, "source_path": str(source.resolve()),
                               "archive_sha256": scan["archive_sha256"] or "DIRECTORY",
                               "schema_review": spec, "cutoff": CUTOFF.isoformat(),
                               "qa_quarantine_count": len(blocked)}.items():
                db.execute("INSERT INTO provenance VALUES (?,?)", (key, json.dumps(value)))
            for row in securities:
                db.execute("INSERT INTO securities VALUES (?,?,?,?,?,?,?,?,?)",
                           tuple(row[k] for k in ("security_id", "issuer_cik", "ticker", "mapping_id",
                                                  "security_class", "valid_from", "valid_to",
                                                  "identity_status", "identity_evidence")))
            prices = defaultdict(set)
            issues = Counter()
            for name, raw in members(source):
                reader = csv_content(raw) if raw else None
                db.execute("INSERT INTO source_files VALUES (?,?,?,?,?)",
                           (name, hashlib.sha256(raw).hexdigest(), len(raw), scan["archive_sha256"],
                            json.dumps(reader.fieldnames if reader else [])))
                if not reader:
                    continue
                # This archive mirrors Data/Stocks under Stocks/. Keep both hashes,
                # but ingest only the canonical Data/Stocks copy.
                if name.startswith(("Stocks/", "ETFs/", "Data/ETFs/")):
                    continue
                ticker = Path(name).name.split(".")[0].upper()
                if ticker not in by_ticker:
                    continue
                if not set(spec["columns"].values()).issubset(reader.fieldnames):
                    raise ValueError(f"Missing mapped columns in {name}")
                for line, record in enumerate(reader, 2):
                    try:
                        source_date = record[spec["columns"]["date"]]
                        d = date.fromisoformat(source_date)
                        source_vals = [record[spec["columns"][key]] for key in
                                       ("open", "high", "low", "close", "volume")]
                        vals = [float(value) for value in source_vals]
                    except (ValueError, TypeError) as exc:
                        raise ValueError(f"Invalid date/number in {name}:{line}") from exc
                    if d > CUTOFF:
                        continue
                    matches = [s for s in by_ticker[ticker] if s["valid_from"] <= d.isoformat() <= s["valid_to"]]
                    if len(matches) != 1:
                        issues["UNRESOLVED_IDENTITY"] += 1
                        db.execute("INSERT INTO findings VALUES (?,?,?,?)", (None, d.isoformat(),
                                   "UNRESOLVED_IDENTITY", f"{name}:{line} ticker {ticker}"))
                        continue
                    security_id = matches[0]["security_id"]
                    o, h, l, c, v = vals
                    if d.weekday() >= 5 or min(o, h, l, c) <= 0 or v < 0 or h < max(o, c, l) or l > min(o, c, h):
                        issues["INVALID_BAR"] += 1
                        db.execute("INSERT INTO findings VALUES (?,?,?,?)", (security_id, d.isoformat(),
                                   "INVALID_BAR", f"{name}:{line}"))
                        continue
                    try:
                        db.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (security_id, d.isoformat(), o, h, l, c, v, source_date,
                                    *source_vals, name, line, spec["price_basis"]))
                    except sqlite3.IntegrityError:
                        issues["DUPLICATE_BAR"] += 1
                        db.execute("INSERT INTO findings VALUES (?,?,?,?)", (security_id, d.isoformat(),
                                   "DUPLICATE_BAR", f"{name}:{line}"))
                        continue
                    prices[security_id].add(d.isoformat())
            for security_id in prices:
                bars = db.execute("SELECT trading_date,close FROM bars WHERE security_id=? ORDER BY trading_date",
                                  (security_id,)).fetchall()
                for (prior_d, prior_c), (current_d, current_c) in zip(bars, bars[1:]):
                    gap = (date.fromisoformat(current_d) - date.fromisoformat(prior_d)).days
                    ratio = current_c / prior_c
                    if gap > 7:
                        issues["MISSING_INTERVAL"] += 1
                        db.execute("INSERT INTO findings VALUES (?,?,?,?)", (security_id, current_d,
                                   "MISSING_INTERVAL", f"{gap} calendar days since {prior_d}"))
                    if ratio >= 1.5 or ratio <= 1/1.5:
                        code = "POSSIBLE_ACTION_OR_JUMP"
                        issues[code] += 1
                        db.execute("INSERT INTO findings VALUES (?,?,?,?)", (security_id, current_d,
                                   code, f"close ratio {ratio:.6g}; action unknown without independent evidence"))
            for s in securities:
                sid = s["security_id"]
                summary = db.execute("SELECT COUNT(*),MIN(trading_date),MAX(trading_date) FROM bars "
                                     "WHERE security_id=?", (sid,)).fetchone()
                n_issues = db.execute("SELECT COUNT(*) FROM findings WHERE security_id=?", (sid,)).fetchone()[0]
                db.execute("INSERT INTO security_coverage VALUES (?,?,?,?,?)", (sid, *summary, n_issues))
                if summary[0] == 0:
                    issues["NO_BARS"] += 1
                    db.execute("INSERT INTO findings VALUES (?,?,?,?)", (sid, None, "NO_BARS", "No mapped bars"))
                for year, n, first, last in db.execute(
                    "SELECT substr(trading_date,1,4),COUNT(*),MIN(trading_date),MAX(trading_date) "
                    "FROM bars WHERE security_id=? GROUP BY substr(trading_date,1,4)", (sid,)).fetchall():
                    db.execute("INSERT INTO year_coverage VALUES (?,?,?,?,?)", (sid, int(year), n, first, last))
                has_issuer = any(r[1] == "issuer_cik" for r in
                                 ro.execute("PRAGMA table_info(transaction_market_mapping)"))
                issuer_filter = " AND tm.issuer_cik=?" if has_issuer else ""
                params = (s["mapping_id"], CUTOFF.isoformat(), s["issuer_cik"]) if has_issuer else \
                         (s["mapping_id"], CUTOFF.isoformat())
                rows = ro.execute("SELECT tm.transaction_id,tm.transaction_date,t.transaction_code "
                    "FROM transaction_market_mapping tm JOIN clean_insider_transactions t "
                    "ON t.transaction_id=tm.transaction_id WHERE tm.mapping_id=? AND tm.transaction_date<=?"
                    + issuer_filter, params)
                counts = defaultdict(lambda: [0, 0, 0])
                for tx, when, code in rows:
                    if not when:
                        continue
                    cell = counts[(int(when[:4]), code or "UNKNOWN")]
                    cell[0] += 1
                    cell[1] += tx in blocked
                    cell[2] += tx not in blocked and when in prices[s["security_id"]]
                for (year, code), values in counts.items():
                    db.execute("INSERT INTO cohort VALUES (?,?,?,?,?,?)", (s["security_id"], year, code, *values))
            db.commit()
            return {"pilot_db": str(pilot_db), "source_files": len(scan["files"]),
                    "bars": db.execute("SELECT COUNT(*) FROM bars").fetchone()[0],
                    "qa_quarantine": len(blocked), "findings": dict(issues),
                    "adjustment_semantics": spec["price_basis"],
                    "security_coverage": [dict(zip(("security_id", "bar_count", "first_date", "last_date", "finding_count"), r))
                                          for r in db.execute("SELECT * FROM security_coverage")]}
        except Exception:
            db.close()
            pilot_db.unlink(missing_ok=True)
            raise
        finally:
            try:
                db.close()
            except Exception:
                pass
    finally:
        ro.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("inspect")
    check.add_argument("--source", type=Path, required=True)
    imp = commands.add_parser("import")
    for flag in ("source", "schema", "manifest", "source-db", "pilot-db", "rights-record"):
        imp.add_argument("--" + flag, type=Path, required=True)
    args = parser.parse_args()
    result = inspect(args.source) if args.command == "inspect" else run(
        args.source, args.schema, args.manifest, args.source_db, args.pilot_db, args.rights_record)
    print(json.dumps(result, indent=2, default=list))


if __name__ == "__main__":
    main()
