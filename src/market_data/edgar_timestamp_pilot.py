"""Bounded, opt-in EDGAR acceptance-time evidence pilot.

Importing this module and generating its manifest never contacts the network.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


APPROVED = {
    "NFLX": ("0001065280", "9565cf77f858e885841123ff3259010c33f5228fa062ca991fb4bce275073f2e", "2006-01-05"),
    "CRM": ("0001108524", "a07dec2efeffaef85ad8e8f4da12ff9c7c12378f835fe62cc3f3ee9cc119e45a", "2006-01-01"),
    "TSLA": ("0001318605", "cd109e805ba2fbfed82d39eb2542de67b73d8102a5965e25d4b4187f0dc7ec24", "2017-02-01"),
}
END = "2017-11-10"
MAX_TOTAL_ATTEMPTS = 60
MAX_ACCESSION_ATTEMPTS = 3
PREFLIGHT_MANIFEST_SHA256 = "fef896b229179e4aa97911c50ea520208a8c9de428ffadabb8f80bee9299cc86"
PREFLIGHT_PLAN_SHA256 = "8906a0d3257b6eec33f4c8b3dd821e03a8d876cc0d77072eadb9fa888df055ed"
FIELDS = ["ticker", "issuer_cik", "reporting_owner_ciks", "archive_cik_candidate", "archive_cik_basis", "accession_number", "document_type", "filing_date", "local_transaction_count", "transaction_years", "source_folder", "selection_reason"]
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
ACCEPTANCE = re.compile(r"(?im)^\s*<ACCEPTANCE-DATETIME>\s*(\d{14})\s*$")


def readonly_db(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)


def load_candidates(db_path: Path, raw_sec_dir: Path) -> list[dict[str, str]]:
    """Exact historical mapping join; raw owner files resolve every listed party."""
    conn = readonly_db(db_path)
    grouped: dict[str, dict] = {}
    try:
        for ticker, (issuer, mapping, start) in APPROVED.items():
            query = """SELECT t.accession_number,t.insider_cik,t.document_type,t.filing_date,
                              t.source_folder,tm.transaction_date
                       FROM transaction_market_mapping tm
                       JOIN clean_insider_transactions t ON t.transaction_id=tm.transaction_id
                       WHERE tm.issuer_cik=? AND tm.mapping_id=?
                         AND tm.transaction_date BETWEEN ? AND ?
                         AND t.issuer_cik=? AND t.accession_number IS NOT NULL"""
            for acc, owner, form, filing, folder, txdate in conn.execute(query, (issuer, mapping, start, END, issuer)):
                if not ACCESSION.fullmatch(acc or ""):
                    continue
                item = grouped.setdefault(acc, dict(ticker=ticker, issuer_cik=issuer, accession_number=acc,
                    document_type=form, filing_date=filing, source_folder=folder, local_transaction_count=0,
                    owners=set(), transaction_years=set(), conflict=False))
                item["local_transaction_count"] += 1
                item["owners"].add(str(owner or "").zfill(10))
                item["transaction_years"].add(str(txdate)[:4])
                item["conflict"] |= (item["document_type"] != form or item["filing_date"] != filing or item["issuer_cik"] != issuer)
    finally:
        conn.close()
    if not grouped:
        raise ValueError("No exactly mapped transactions found")
    # Read each local flattened owner file once. A transaction insider alone is
    # insufficient evidence of all reporting owners on a multi-owner filing.
    by_folder: dict[str, set[str]] = defaultdict(set)
    for acc, row in grouped.items():
        by_folder[str(row["source_folder"])].add(acc)
    for folder, wanted in by_folder.items():
        path = raw_sec_dir / folder / "REPORTINGOWNER.tsv"
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for record in csv.DictReader(handle, delimiter="\t"):
                acc = record.get("ACCESSION_NUMBER", "")
                if acc in wanted:
                    grouped[acc].setdefault("raw_owners", set()).add(str(record.get("RPTOWNERCIK", "")).strip().zfill(10))
    candidates = []
    for row in grouped.values():
        if row["conflict"] or row["document_type"] not in {"4", "4/A"}:
            continue
        owners = sorted(row.get("raw_owners", set()))
        if not owners or not row["owners"].issubset(set(owners)) or any(not re.fullmatch(r"\d{10}", x) for x in owners):
            continue
        row["reporting_owner_ciks"] = "|".join(owners)
        prefix = acc[:10]
        candidate = prefix if prefix in owners else owners[0]
        row["archive_cik_candidate"] = str(int(candidate))
        row["archive_cik_basis"] = "accession_prefix_is_reported_owner_candidate_unverified" if prefix in owners else "first_reported_owner_candidate_unverified"
        row["transaction_years"] = "|".join(sorted(row["transaction_years"]))
        candidates.append(row)
    return candidates


def select_twenty(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deterministic strata: each issuer, amendment and multi-owner if available."""
    chosen: list[dict[str, str]] = []
    quotas = {"NFLX": 6, "CRM": 8, "TSLA": 6}
    for ticker, quota in quotas.items():
        pool = sorted((r for r in candidates if r["ticker"] == ticker and r["filing_date"] <= END), key=lambda r: (r["filing_date"], r["accession_number"]))
        if len(pool) < quota:
            raise ValueError(f"Only {len(pool)} eligible {ticker} filings")
        picks: list[dict[str, str]] = []
        def add(row, reason):
            if row and all(p["accession_number"] != row["accession_number"] for p in picks):
                row = dict(row)
                row["selection_reason"] = reason
                picks.append(row)
        amendments = [r for r in pool if r["document_type"] == "4/A"]
        add(amendments[len(amendments)//2] if amendments else None, "amendment")
        multi = [r for r in pool if "|" in r["reporting_owner_ciks"]]
        add(multi[len(multi)//2] if multi else None, "multi_owner")
        years = sorted({r["filing_date"][:4] for r in pool})
        for year in (years[0], years[len(years)//2], years[-1]):
            year_rows = [r for r in pool if r["filing_date"].startswith(year)]
            add(year_rows[len(year_rows)//2], "year_span")
        for i in range(quota):
            add(pool[round(i * (len(pool)-1) / max(quota-1, 1))], "date_quantile")
        for row in pool:
            if len(picks) >= quota:
                break
            add(row, "deterministic_fill")
        chosen.extend(picks[:quota])
    if len(chosen) != 20 or len({r["accession_number"] for r in chosen}) != 20:
        raise ValueError("Selection did not yield 20 distinct accessions")
    return sorted(chosen, key=lambda r: (r["ticker"], r["filing_date"], r["accession_number"]))


def write_manifest(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in FIELDS} for row in rows)


def archive_url(row: dict[str, str]) -> str:
    acc = row["accession_number"]
    cik = row["archive_cik_candidate"]
    if not ACCESSION.fullmatch(acc) or not cik.isdigit():
        raise ValueError("Invalid accession or archive CIK candidate")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{acc}.txt"


def validate_manifest_row(row: dict[str, str]) -> None:
    ticker = row.get("ticker", "")
    if ticker not in APPROVED or row.get("issuer_cik") != APPROVED[ticker][0]:
        raise ValueError("Manifest contains an unapproved issuer")
    if row.get("document_type") not in {"4", "4/A"}:
        raise ValueError("Only Form 4 and 4/A are permitted")
    if not (APPROVED[ticker][2] <= row.get("filing_date", "") <= END):
        raise ValueError("Filing date outside bounded pilot period")
    owners = row.get("reporting_owner_ciks", "").split("|")
    if not owners or any(not re.fullmatch(r"\d{10}", cik) for cik in owners):
        raise ValueError("Invalid reporting-owner identity")
    if row.get("archive_cik_candidate") not in {str(int(cik)) for cik in owners}:
        raise ValueError("Archive path candidate is not a recorded owner")
    if int(row.get("local_transaction_count", "0")) < 1:
        raise ValueError("No local transactions for accession")
    archive_url(row)


def eastern_to_utc(raw: str) -> str:
    if not re.fullmatch(r"\d{14}", raw):
        raise ValueError("Invalid acceptance value")
    wall = datetime.strptime(raw, "%Y%m%d%H%M%S")
    zone = ZoneInfo("America/New_York")
    a, b = wall.replace(tzinfo=zone, fold=0), wall.replace(tzinfo=zone, fold=1)
    if a.utcoffset() != b.utcoffset():
        raise ValueError("Ambiguous or nonexistent Eastern local time")
    utc = a.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != wall:
        raise ValueError("Nonexistent Eastern local time")
    return utc.isoformat()


def verify_header(body: bytes, row: dict[str, str]) -> tuple[str, None]:
    text = body.decode("latin-1")
    match = re.search(r"(?is)<SEC-HEADER>(.*?)</SEC-HEADER>", text)
    if not match:
        raise ValueError("SEC-HEADER absent")
    header = match.group(1)
    values = ACCEPTANCE.findall(header)
    if len(values) != 1:
        raise ValueError("Missing or multiple acceptance values")
    acc_values = re.findall(r"(?im)^\s*ACCESSION NUMBER:\s*(\d{10}-\d{2}-\d{6})\s*$", header)
    if acc_values != [row["accession_number"]]:
        raise ValueError("Accession mismatch")
    form_values = re.findall(r"(?im)^\s*CONFORMED SUBMISSION TYPE:\s*([^\r\n]+)", header)
    if len(form_values) != 1 or form_values[0].strip() != row["document_type"]:
        raise ValueError("Form type mismatch")
    date_values = re.findall(r"(?im)^\s*FILED AS OF DATE:\s*(\d{8})\s*$", header)
    if len(date_values) != 1 or date_values[0] != row["filing_date"].replace("-", ""):
        raise ValueError("Filing date mismatch")
    ciks = {x.zfill(10) for x in re.findall(r"(?im)^\s*CENTRAL INDEX KEY:\s*(\d+)\s*$", header)}
    expected = {row["issuer_cik"], *row["reporting_owner_ciks"].split("|")}
    if not expected.issubset(ciks):
        raise ValueError("Issuer or reporting-owner CIK missing from header")
    return values[0], None  # No UTC assertion until SEC timezone convention is documented.


def raw_acceptance_values(body: bytes) -> list[str]:
    text = body.decode("latin-1")
    match = re.search(r"(?is)<SEC-HEADER>(.*?)</SEC-HEADER>", text)
    return ACCEPTANCE.findall(match.group(1)) if match else []


def open_pilot_db(path: Path) -> sqlite3.Connection:
    if path.name.lower() == "insider_research.db":
        raise ValueError("Production database is never a pilot output")
    if path.exists():
        existing = readonly_db(path)
        try:
            tables = {r[0] for r in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"evidence", "http_attempts", "sqlite_sequence"}:
                raise ValueError("Refusing non-pilot existing database")
        finally:
            existing.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("BEGIN IMMEDIATE")
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS evidence (
      id INTEGER PRIMARY KEY, accession_number TEXT NOT NULL, source_url TEXT NOT NULL,
      retrieved_at_utc TEXT NOT NULL, response_sha256 TEXT, raw_response BLOB,
      raw_acceptance TEXT, parsed_utc TEXT, verification_status TEXT NOT NULL,
      detail TEXT NOT NULL, http_status INTEGER, attempt_count INTEGER NOT NULL,
      timezone_status TEXT NOT NULL DEFAULT 'UNVERIFIED')""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT,'append-only'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT,'append-only'); END""")
        had_attempts = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='http_attempts'").fetchone()[0] == 1
        db.execute("""CREATE TABLE IF NOT EXISTS http_attempts (
          id INTEGER PRIMARY KEY, accession_number TEXT NOT NULL, attempt_number INTEGER NOT NULL,
          started_at_utc TEXT, finished_at_utc TEXT, exception_type TEXT,
          sanitized_error TEXT, http_status INTEGER, outcome TEXT NOT NULL,
          legacy_evidence_at_utc TEXT,
          UNIQUE(accession_number,attempt_number))""")
        if not had_attempts:
            for accession, retrieval_time, detail, status, count in db.execute(
                "SELECT accession_number,retrieved_at_utc,detail,http_status,attempt_count FROM evidence ORDER BY id"):
                for number in range(1, count + 1):
                    db.execute("""INSERT INTO http_attempts(accession_number,attempt_number,started_at_utc,
                                  finished_at_utc,exception_type,sanitized_error,http_status,outcome,legacy_evidence_at_utc)
                                  VALUES(?,?,?,?,?,?,?,?,?)""", (accession, number, None, None,
                                  detail.removeprefix("transport_") if number == count and detail.startswith("transport_") else None,
                                  "not captured by previous implementation", status if number == count else None,
                                  "LEGACY_FINAL" if number == count else "LEGACY_UNKNOWN", retrieval_time))
        if db.execute("SELECT count(*) FROM http_attempts").fetchone()[0] > MAX_TOTAL_ATTEMPTS:
            raise ValueError("Existing attempt history exceeds the 60-attempt cap")
    return db


def sanitize_error(value: object) -> str:
    message = str(value).replace("\r", " ").replace("\n", " ")
    message = re.sub(r"https?://\S+", "[url]", message)
    message = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", message)
    return message[:240]


def reserve_attempt(db: sqlite3.Connection, accession: str) -> int:
    """A durable reservation is counted even if the process dies during open()."""
    db.execute("BEGIN IMMEDIATE")
    try:
        total = db.execute("SELECT count(*) FROM http_attempts").fetchone()[0]
        used = db.execute("SELECT count(*) FROM http_attempts WHERE accession_number=?", (accession,)).fetchone()[0]
        if total >= MAX_TOTAL_ATTEMPTS or used >= MAX_ACCESSION_ATTEMPTS:
            raise ValueError("HTTP attempt budget exhausted")
        number = used + 1
        db.execute("""INSERT INTO http_attempts(accession_number,attempt_number,started_at_utc,outcome)
                      VALUES(?,?,?,?)""", (accession, number, datetime.now(timezone.utc).isoformat(), "IN_FLIGHT"))
        db.commit()
        return number
    except BaseException:
        db.rollback()
        raise


def finish_attempt(db: sqlite3.Connection, accession: str, number: int, status: int | None,
                   exception_type: str | None, message: object, outcome: str) -> None:
    with db:
        result = db.execute("""UPDATE http_attempts SET finished_at_utc=?,http_status=?,exception_type=?,
                      sanitized_error=?,outcome=? WHERE accession_number=? AND attempt_number=? AND outcome='IN_FLIGHT'""",
                   (datetime.now(timezone.utc).isoformat(), status, exception_type,
                    sanitize_error(message) if message else None, outcome, accession, number))
        if result.rowcount != 1:
            raise ValueError("Attempt reservation missing or already completed")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def fetch_bounded(url: str, user_agent: str, opener=None, sleep=time.sleep, max_attempts: int = 3,
                  on_start=None, on_result=None, halt_on_429: bool = False) -> tuple[bytes | None, int | None, int, str]:
    if urlparse(url).hostname != "www.sec.gov" or urlparse(url).scheme != "https":
        raise ValueError("Only official SEC HTTPS archive URLs are allowed")
    if max_attempts < 1 or max_attempts > 3:
        raise ValueError("Retry cap is three")
    opener = opener or build_opener(NoRedirect())
    for attempt in range(1, max_attempts + 1):
        if on_start:
            on_start(attempt)
        try:
            response = opener.open(Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"}), timeout=20)
            if response.geturl() != url:
                if on_result:
                    on_result(attempt, None, None, "redirect_or_url_change", "REDIRECT_BLOCKED")
                return None, None, attempt, "redirect_or_url_change"
            with response:
                body = response.read(10_000_001)
                if len(body) > 10_000_000:
                    if on_result:
                        on_result(attempt, response.status, None, "response_too_large", "OVERSIZE")
                    return None, response.status, attempt, "response_too_large"
                if on_result:
                    on_result(attempt, response.status, None, None, "HTTP_OK")
                return body, response.status, attempt, "ok"
        except HTTPError as exc:
            if on_result:
                on_result(attempt, exc.code, type(exc).__name__, exc.reason, "HTTP_ERROR")
            if exc.code == 429 and halt_on_429:
                return exc.read(10_000_001), exc.code, attempt, "http_429_policy_stop"
            if exc.code not in {429, 500, 502, 503, 504} or attempt == max_attempts:
                return exc.read(10_000_001), exc.code, attempt, f"http_{exc.code}"
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry_after).astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        return exc.read(10_000_001), exc.code, attempt, "invalid_retry_after"
                if delay > 60:
                    return exc.read(10_000_001), exc.code, attempt, "retry_after_exceeds_60s_cap"
                sleep(max(delay, 2 ** attempt))
                continue
        except (URLError, TimeoutError) as exc:
            if on_result:
                on_result(attempt, None, type(exc).__name__, getattr(exc, "reason", exc), "TRANSPORT_ERROR")
            if attempt == max_attempts:
                return None, None, attempt, f"transport_{type(exc).__name__}"
        except Exception as exc:
            if on_result:
                on_result(attempt, None, type(exc).__name__, exc, "UNEXPECTED_ERROR")
            raise
        sleep(2 ** attempt)
    raise AssertionError("unreachable")


def recover(manifest: Path, pilot_db: Path, user_agent: str, opener=None, sleep=time.sleep) -> dict[str, int]:
    if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_agent) or len(user_agent) < 15:
        raise ValueError("Descriptive User-Agent with real contact email required")
    with manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if opener is None:
        canonical_db = Path(__file__).resolve().parents[2] / "data" / "processed" / "edgar_timestamp_pilot_20.db"
        if pilot_db.resolve() != canonical_db or not pilot_db.is_file():
            raise ValueError("Live resume requires the existing canonical pilot database")
        plan = manifest.with_name("edgar_timestamp_20_request_plan.csv")
        if (hashlib.sha256(manifest.read_bytes()).hexdigest() != PREFLIGHT_MANIFEST_SHA256 or
                not plan.is_file() or hashlib.sha256(plan.read_bytes()).hexdigest() != PREFLIGHT_PLAN_SHA256):
            raise ValueError("Preflight manifest or request plan hash changed")
    if len(rows) != 20 or len({r["accession_number"] for r in rows}) != 20:
        raise ValueError("Exactly 20 distinct accessions required")
    for row in rows:
        validate_manifest_row(row)
    db = open_pilot_db(pilot_db)
    counts = defaultdict(int)
    try:
        for i, row in enumerate(rows):
            accession = row["accession_number"]
            previous = db.execute("""SELECT verification_status,http_status,detail,raw_response,
                                     response_sha256,raw_acceptance,source_url FROM evidence
                                     WHERE accession_number=? ORDER BY id DESC LIMIT 1""", (accession,)).fetchone()
            if previous:
                prior_status, prior_http, prior_detail, prior_body, prior_sha, prior_raw, prior_url = previous
                if prior_status == "VERIFIED_RAW":
                    try:
                        if (prior_body is None or hashlib.sha256(prior_body).hexdigest() != prior_sha or
                                prior_url != archive_url(row) or verify_header(prior_body, row)[0] != prior_raw):
                            raise ValueError("Prior verified evidence failed integrity check")
                    except ValueError:
                        counts["HALTED_PRIOR_EVIDENCE_INTEGRITY"] = 1
                        break
                    counts["SKIPPED_VERIFIED"] += 1
                    continue
                if prior_status in {"UNRESOLVED", "CONFLICT"}:
                    counts["SKIPPED_UNRESOLVED"] += 1
                    continue
                if prior_http in {401, 403, 429} or prior_detail in {"invalid_retry_after", "retry_after_exceeds_60s_cap"}:
                    counts["HALTED_PRIOR_ACCESS_POLICY"] = 1
                    break
            used = db.execute("SELECT count(*) FROM http_attempts WHERE accession_number=?", (accession,)).fetchone()[0]
            total = db.execute("SELECT count(*) FROM http_attempts").fetchone()[0]
            if used >= MAX_ACCESSION_ATTEMPTS:
                counts["SKIPPED_EXHAUSTED"] += 1
                continue
            if total >= MAX_TOTAL_ATTEMPTS:
                counts["GLOBAL_BUDGET_EXHAUSTED"] = 1
                break
            if i:
                sleep(1.25)  # below one request per second, excluding retry backoff
            url = archive_url(row)
            reserved: dict[int, int] = {}
            def start(local_number: int) -> None:
                reserved[local_number] = reserve_attempt(db, accession)
            def result(local_number: int, status: int | None, exception_type: str | None,
                       message: object, outcome: str) -> None:
                finish_attempt(db, accession, reserved[local_number], status, exception_type, message, outcome)
            body, status, attempts, detail = fetch_bounded(url, user_agent, opener=opener, sleep=sleep,
                max_attempts=min(MAX_ACCESSION_ATTEMPTS-used, MAX_TOTAL_ATTEMPTS-total),
                on_start=start, on_result=result)
            raw, utc, state = None, None, "FAILED"
            if body is not None and status == 200:
                values = raw_acceptance_values(body)
                raw = values[0] if len(values) == 1 else json.dumps(values)
                try:
                    verified_raw, _provisional_utc = verify_header(body, row)
                    previous = {r[0] for r in db.execute("SELECT raw_acceptance FROM evidence WHERE accession_number=? AND verification_status='VERIFIED_RAW'", (row["accession_number"],))}
                    if previous and previous != {verified_raw}:
                        state, detail = "CONFLICT", "previous_acceptance_disagrees"
                    else:
                        state, detail = "VERIFIED_RAW", "header_identity_and_raw_acceptance_verified;timezone_unverified"
                except ValueError as exc:
                    state, detail = "UNRESOLVED", str(exc)
            elif status in {404, 410}:
                state = "UNRESOLVED"
            db.execute("""INSERT INTO evidence(accession_number,source_url,retrieved_at_utc,response_sha256,raw_response,
                          raw_acceptance,parsed_utc,verification_status,detail,http_status,attempt_count,timezone_status)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (row["accession_number"], url, datetime.now(timezone.utc).isoformat(),
                          hashlib.sha256(body).hexdigest() if body is not None else None, body, raw, utc, state, detail, status, attempts, "UNVERIFIED"))
            db.commit()
            counts[state] += 1
            if state in {"FAILED", "CONFLICT"} or (state == "UNRESOLVED" and status == 200):
                counts["HALTED"] = 1
                break
    finally:
        db.close()
    return dict(counts)


def write_validation_report(manifest: Path, pilot_db: Path, report: Path) -> None:
    with manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    db = readonly_db(pilot_db)
    try:
        evidence = {r[0]: r[1:] for r in db.execute("""SELECT accession_number,verification_status,detail,raw_acceptance,
                     parsed_utc,source_url,response_sha256,attempt_count FROM evidence ORDER BY id""")}
    finally:
        db.close()
    report.parent.mkdir(parents=True, exist_ok=True)
    fields = ["accession_number", "ticker", "filing_date", "verification_status", "detail", "raw_acceptance", "parsed_utc", "source_url", "response_sha256", "attempt_count"]
    with report.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        for row in rows:
            values = evidence.get(row["accession_number"], ("NOT_REQUESTED", "", "", "", "", "", ""))
            writer.writerow(dict(zip(fields, [row["accession_number"], row["ticker"], row["filing_date"], *values])))


def write_request_plan(manifest: Path, report: Path) -> None:
    with manifest.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 20 or len({r["accession_number"] for r in rows}) != 20:
        raise ValueError("Exactly 20 distinct accessions required")
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, ["accession_number", "ticker", "endpoint", "planned_gets", "maximum_attempts", "preflight_status", "path_risk"])
        writer.writeheader()
        for row in rows:
            validate_manifest_row(row)
            writer.writerow(dict(accession_number=row["accession_number"], ticker=row["ticker"], endpoint=archive_url(row),
                                 planned_gets=1, maximum_attempts=3, preflight_status="NOT_REQUESTED",
                                 path_risk=row["archive_cik_basis"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("manifest", help="Local read-only selection; no network")
    make.add_argument("--production-db", type=Path, required=True)
    make.add_argument("--raw-sec-dir", type=Path, required=True)
    make.add_argument("--output", type=Path, required=True)
    plan = sub.add_parser("plan", help="Write exact endpoint plan; no network")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    live = sub.add_parser("recover", help="LIVE SEC requests; separate owner authorization required")
    live.add_argument("--manifest", type=Path, required=True)
    live.add_argument("--pilot-db", type=Path, required=True)
    live.add_argument("--user-agent", required=True)
    live.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "manifest":
        rows = select_twenty(load_candidates(args.production_db, args.raw_sec_dir))
        write_manifest(rows, args.output)
        print(json.dumps({"rows": len(rows), "manifest": str(args.output)}, indent=2))
    elif args.command == "plan":
        write_request_plan(args.manifest, args.output)
        print(json.dumps({"planned_gets": 20, "maximum_http_attempts": 60, "report": str(args.output)}, indent=2))
    else:
        counts = recover(args.manifest, args.pilot_db, args.user_agent)
        write_validation_report(args.manifest, args.pilot_db, args.report)
        print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
