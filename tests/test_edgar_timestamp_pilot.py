"""All EDGAR pilot tests are synthetic and offline."""

import csv
import sqlite3
from urllib.error import HTTPError, URLError

import pytest

from src.market_data.edgar_timestamp_pilot import (
    archive_url, eastern_to_utc, fetch_bounded, open_pilot_db, recover, reserve_attempt,
    sanitize_error, verify_header,
)


ROW = dict(ticker="NFLX", issuer_cik="0001065280", reporting_owner_ciks="0001234567",
           archive_cik_candidate="1234567", accession_number="0001234567-17-000001",
           document_type="4", filing_date="2017-03-13", local_transaction_count="1")


def header(acceptance="20170313161500", accession=None, form="4", filing="20170313", owner="1234567"):
    accession = accession or ROW["accession_number"]
    return (f"<SEC-HEADER>\nACCESSION NUMBER: {accession}\n<ACCEPTANCE-DATETIME>{acceptance}\n"
            f"CONFORMED SUBMISSION TYPE: {form}\nFILED AS OF DATE: {filing}\n"
            f"CENTRAL INDEX KEY: 1065280\nCENTRAL INDEX KEY: {owner}\n</SEC-HEADER>").encode()


def test_exact_accession_and_parties():
    raw, utc = verify_header(header(), ROW)
    assert raw == "20170313161500"
    assert utc is None  # Raw SEC value is verified; timezone is not.
    for body in (header(accession="0001234567-17-000002"), header(owner="9999999"),
                 header(form="4/A"), header(filing="20170314")):
        with pytest.raises(ValueError):
            verify_header(body, ROW)


def test_dst_and_multiple_acceptance_fail_closed():
    assert eastern_to_utc("20170113161500") == "2017-01-13T21:15:00+00:00"
    assert eastern_to_utc("20170713161500") == "2017-07-13T20:15:00+00:00"
    for raw in ("20171105013000", "20170312023000"):
        with pytest.raises(ValueError):
            eastern_to_utc(raw)
    with pytest.raises(ValueError, match="multiple"):
        verify_header(header().replace(b"</SEC-HEADER>", b"<ACCEPTANCE-DATETIME>20170313161600\n</SEC-HEADER>"), ROW)


class Response:
    status = 200
    def __init__(self, url, body):
        self.url, self.body = url, body
    def geturl(self):
        return self.url
    def read(self, _):
        return self.body
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


class Flaky:
    def __init__(self, failures=0, body=None):
        self.failures, self.calls, self.body = failures, 0, body or header()
    def open(self, request, timeout):
        self.calls += 1
        if self.calls <= self.failures:
            raise HTTPError(request.full_url, 503, "unavailable", {}, None)
        return Response(request.full_url, self.body)


def synthetic_manifest(tmp_path):
    path = tmp_path / "manifest.csv"
    rows = [dict(ROW, accession_number=f"0001234567-17-{i:06d}") for i in range(1, 21)]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    return path, rows


class Dynamic:
    def __init__(self, acceptance="20170313161500"):
        self.acceptance, self.calls = acceptance, 0
    def open(self, request, timeout):
        self.calls += 1
        acc = request.full_url.rsplit("/", 1)[-1][:-4]
        return Response(request.full_url, header(self.acceptance, accession=acc))


def test_retry_cap_and_url():
    url = archive_url(ROW)
    assert url == "https://www.sec.gov/Archives/edgar/data/1234567/000123456717000001/0001234567-17-000001.txt"
    fake = Flaky(10)
    body, status, count, detail = fetch_bounded(url, "Research Example contact@example.org", opener=fake, sleep=lambda _: None)
    assert (body, status, count, detail) == (b"", 503, 3, "http_503")
    assert fake.calls == 3
    fake = Flaky(1)
    assert fetch_bounded(url, "Research Example contact@example.org", opener=fake, sleep=lambda _: None)[2] == 2
    with pytest.raises(ValueError):
        fetch_bounded("https://example.com/filing.txt", "contact@example.org", opener=fake)


def test_missing_filing_and_multi_owner_fail_closed():
    class Missing:
        calls = 0
        def open(self, request, timeout):
            self.calls += 1
            raise HTTPError(request.full_url, 404, "missing", {}, None)
    missing = Missing()
    assert fetch_bounded(archive_url(ROW), "Research contact@example.org", opener=missing, sleep=lambda _: None)[2:] == (1, "http_404")
    assert missing.calls == 1
    row = dict(ROW, reporting_owner_ciks="0001234567|0007654321")
    with pytest.raises(ValueError, match="reporting-owner"):
        verify_header(header(), row)


def test_retry_after_and_access_policy():
    class Throttle:
        calls = 0
        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise HTTPError(request.full_url, 429, "throttled", {"Retry-After": "7"}, None)
            return Response(request.full_url, header())
    waits = []
    throttle = Throttle()
    assert fetch_bounded(archive_url(ROW), "Research contact@example.org", opener=throttle, sleep=waits.append)[2] == 2
    assert waits == [7]
    policy_stop = Throttle()
    assert fetch_bounded(archive_url(ROW), "Research contact@example.org", opener=policy_stop,
                         sleep=lambda _: None, halt_on_429=True)[1:4] == (429, 1, "http_429_policy_stop")
    assert policy_stop.calls == 1
    class Forbidden:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 403, "forbidden", {}, None)
    assert fetch_bounded(archive_url(ROW), "Research contact@example.org", opener=Forbidden(), sleep=lambda _: None)[1:4] == (403, 1, "http_403")


def test_append_only_and_completed_filing_skip(tmp_path):
    path, rows = synthetic_manifest(tmp_path)
    dbpath = tmp_path / "pilot.db"
    assert recover(path, dbpath, "Example Research contact@example.org", opener=Dynamic("20170313161500"), sleep=lambda _: None) == {"VERIFIED_RAW": 20}
    second = Dynamic("20170313161600")
    assert recover(path, dbpath, "Example Research contact@example.org", opener=second, sleep=lambda _: None) == {"SKIPPED_VERIFIED": 20}
    assert second.calls == 0
    db = open_pilot_db(dbpath)
    assert db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 20
    assert db.execute("SELECT count(*) FROM http_attempts").fetchone()[0] == 20
    assert db.execute("SELECT count(*) FROM evidence WHERE parsed_utc IS NOT NULL OR timezone_status!='UNVERIFIED'").fetchone()[0] == 0
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM evidence")
    db.execute("""INSERT INTO evidence(accession_number,source_url,retrieved_at_utc,response_sha256,raw_response,
                  raw_acceptance,parsed_utc,verification_status,detail,http_status,attempt_count,timezone_status)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (rows[0]["accession_number"], archive_url(rows[0]),
                  "2026-10-16T00:00:00+00:00", "bad_hash", header(accession=rows[0]["accession_number"]),
                  "20170313161500", None, "VERIFIED_RAW", "synthetic_corruption", 200, 1, "UNVERIFIED"))
    db.commit()
    db.close()
    client = Dynamic()
    assert recover(path, dbpath, "Example Research contact@example.org", opener=client, sleep=lambda _: None) == {"HALTED_PRIOR_EVIDENCE_INTEGRITY": 1}
    assert client.calls == 0


def test_interruption_reserves_attempt_and_restart_skips_consumed_number(tmp_path):
    path, rows = synthetic_manifest(tmp_path)
    dbpath = tmp_path / "pilot.db"
    class Interrupted:
        def open(self, request, timeout):
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        recover(path, dbpath, "Example Research contact@example.org", opener=Interrupted(), sleep=lambda _: None)
    db = open_pilot_db(dbpath)
    assert db.execute("SELECT attempt_number,outcome FROM http_attempts").fetchall() == [(1, "IN_FLIGHT")]
    db.close()
    client = Dynamic()
    assert recover(path, dbpath, "Example Research contact@example.org", opener=client, sleep=lambda _: None) == {"VERIFIED_RAW": 20}
    assert client.calls == 20
    db = open_pilot_db(dbpath)
    assert db.execute("SELECT attempt_number FROM http_attempts WHERE accession_number=? ORDER BY attempt_number", (rows[0]["accession_number"],)).fetchall() == [(1,), (2,)]
    assert db.execute("SELECT count(*) FROM http_attempts").fetchone()[0] == 21
    db.close()


def test_cumulative_budget_across_restart(tmp_path):
    path, rows = synthetic_manifest(tmp_path)
    dbpath = tmp_path / "pilot.db"
    db = open_pilot_db(dbpath)
    for row in rows[:19]:
        for _ in range(3):
            reserve_attempt(db, row["accession_number"])
    for _ in range(2):
        reserve_attempt(db, rows[-1]["accession_number"])
    db.close()
    client = Dynamic()
    assert recover(path, dbpath, "Example Research contact@example.org", opener=client, sleep=lambda _: None) == {"SKIPPED_EXHAUSTED": 19, "VERIFIED_RAW": 1}
    assert client.calls == 1
    db = open_pilot_db(dbpath)
    assert db.execute("SELECT count(*) FROM http_attempts").fetchone()[0] == 60
    with pytest.raises(ValueError, match="budget"):
        reserve_attempt(db, rows[0]["accession_number"])
    db.close()
    assert recover(path, dbpath, "Example Research contact@example.org", opener=Dynamic(), sleep=lambda _: None) == {"SKIPPED_EXHAUSTED": 19, "SKIPPED_VERIFIED": 1}


def test_legacy_three_attempts_are_counted_without_inventing_times(tmp_path):
    path, rows = synthetic_manifest(tmp_path)
    dbpath = tmp_path / "pilot.db"
    db = sqlite3.connect(dbpath)
    db.execute("""CREATE TABLE evidence(id INTEGER PRIMARY KEY,accession_number TEXT,source_url TEXT,
               retrieved_at_utc TEXT,response_sha256 TEXT,raw_response BLOB,raw_acceptance TEXT,parsed_utc TEXT,
               verification_status TEXT,detail TEXT,http_status INTEGER,attempt_count INTEGER,timezone_status TEXT)""")
    db.execute("""INSERT INTO evidence(accession_number,retrieved_at_utc,verification_status,detail,attempt_count)
                  VALUES(?,?,?,?,?)""", (rows[0]["accession_number"], "2026-10-16T16:12:07+00:00", "FAILED", "transport_URLError", 3))
    db.commit(); db.close()
    db = open_pilot_db(dbpath)
    assert db.execute("SELECT attempt_number,started_at_utc,exception_type FROM http_attempts ORDER BY attempt_number").fetchall() == [(1, None, None), (2, None, None), (3, None, "URLError")]
    db.close()
    client = Dynamic()
    assert recover(path, dbpath, "Example Research contact@example.org", opener=client, sleep=lambda _: None) == {"SKIPPED_EXHAUSTED": 1, "VERIFIED_RAW": 19}
    assert client.calls == 19


def test_error_sanitization_and_logged_retry_history(tmp_path):
    assert "secret@example.org" not in sanitize_error("secret@example.org https://secret.invalid/path")
    path, rows = synthetic_manifest(tmp_path)
    dbpath = tmp_path / "pilot.db"
    class Broken:
        def open(self, request, timeout):
            raise URLError("DNS failure secret@example.org https://secret.invalid/path")
    result = recover(path, dbpath, "Example Research contact@example.org", opener=Broken(), sleep=lambda _: None)
    assert result == {"FAILED": 1, "HALTED": 1}
    db = open_pilot_db(dbpath)
    log = db.execute("SELECT attempt_number,exception_type,sanitized_error,http_status,outcome FROM http_attempts ORDER BY id").fetchall()
    assert [r[0] for r in log] == [1, 2, 3]
    assert all(r[1] == "URLError" and r[3] is None and r[4] == "TRANSPORT_ERROR" for r in log)
    assert all("secret@example.org" not in r[2] and "secret.invalid" not in r[2] for r in log)
    db.close()


def test_live_mode_cannot_start_a_fresh_pilot_database(tmp_path):
    path, _ = synthetic_manifest(tmp_path)
    with pytest.raises(ValueError, match="existing canonical pilot database"):
        recover(path, tmp_path / "new_pilot.db", "Example Research contact@example.org")
