"""Synthetic-only tests for bounded EDGAR batch recovery."""

import csv
import hashlib
import sqlite3

import pytest

from src.market_data import edgar_batch
from src.market_data.edgar_full_manifest import FIELDS
from src.market_data.edgar_timestamp_pilot import APPROVED


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
        return None


def make_files(tmp_path, monkeypatch, count=2):
    pilot = tmp_path / "pilot.csv"
    pilot.write_text("accession_number\n0000000000-17-000001\n", encoding="utf-8")
    monkeypatch.setattr(edgar_batch, "PREFLIGHT_MANIFEST_SHA256", hashlib.sha256(pilot.read_bytes()).hexdigest())
    rows = []
    issuer, mapping, start = APPROVED["NFLX"]
    for i in range(1, count + 1):
        rows.append(dict(accession_number=f"0001234567-17-{i:06d}", ticker="NFLX", historical_security_id=mapping,
                         mapping_id=mapping, issuer_cik=issuer, approved_valid_from=start, approved_valid_to="2017-11-10",
                         first_transaction_date="2017-03-10", last_transaction_date="2017-03-10", document_type="4",
                         filing_date="2017-03-13", reporting_owner_ciks="0001234567", local_transaction_count="1",
                         source_folder="synthetic", archive_cik_candidate="1234567", archive_cik_basis="owner",
                         eligibility_status="READY", eligibility_issue=""))
    manifest = tmp_path / "batch.csv"
    with manifest.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS); writer.writeheader(); writer.writerows(rows)
    return manifest, pilot, hashlib.sha256(manifest.read_bytes()).hexdigest(), rows


class Success:
    calls = 0
    def open(self, request, timeout):
        self.calls += 1
        acc = request.full_url.rsplit("/", 1)[-1][:-4]
        body = (f"<SEC-HEADER>\nACCESSION NUMBER: {acc}\n<ACCEPTANCE-DATETIME>20170313161500\n"
                "CONFORMED SUBMISSION TYPE: 4\nFILED AS OF DATE: 20170313\n"
                "CENTRAL INDEX KEY: 1065280\nCENTRAL INDEX KEY: 1234567\n</SEC-HEADER>").encode()
        return Response(request.full_url, body)


def test_interruption_restart_dedup_and_raw_timezone(tmp_path, monkeypatch):
    manifest, pilot, digest, rows = make_files(tmp_path, monkeypatch)
    database = tmp_path / "batch.db"
    class Interrupted:
        def open(self, request, timeout):
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        edgar_batch.recover_batch(manifest, pilot, database, digest, "Research contact@example.org",
                                  opener=Interrupted(), sleep=lambda _: None, progress=lambda *a, **k: None)
    db = edgar_batch.open_batch_db(database, digest)
    assert db.execute("SELECT count(*),outcome FROM http_attempts").fetchone() == (1, "IN_FLIGHT")
    db.close()
    waits = []
    client = Success()
    result = edgar_batch.recover_batch(manifest, pilot, database, digest, "Research contact@example.org",
                                        opener=client, sleep=waits.append, progress=lambda *a, **k: None)
    assert result == {"VERIFIED_RAW": 2, "ATTEMPTS_CUMULATIVE": 3}
    assert len(waits) == 2
    assert 0 < waits[0] <= edgar_batch.PACE_SECONDS
    assert waits[1] == edgar_batch.PACE_SECONDS
    assert client.calls == 2
    db = edgar_batch.open_batch_db(database, digest)
    assert db.execute("SELECT count(*) FROM evidence WHERE parsed_utc IS NULL AND timezone_status='UNVERIFIED'").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM http_attempts").fetchone()[0] == 3
    db.close()
    another = Success()
    assert edgar_batch.recover_batch(manifest, pilot, database, digest, "Research contact@example.org",
                                     opener=another, sleep=lambda _: None, progress=lambda *a, **k: None) == {
                                         "SKIPPED_VERIFIED": 2, "ATTEMPTS_CUMULATIVE": 3}
    assert another.calls == 0


def test_cumulative_limit_and_manifest_pin(tmp_path, monkeypatch):
    manifest, pilot, digest, rows = make_files(tmp_path, monkeypatch)
    monkeypatch.setattr(edgar_batch, "MAX_BATCH_ATTEMPTS", 3)
    database = tmp_path / "batch.db"
    db = edgar_batch.open_batch_db(database, digest)
    for _ in range(3):
        edgar_batch.reserve(db, rows[0]["accession_number"])
    with pytest.raises(ValueError, match="budget"):
        edgar_batch.reserve(db, rows[1]["accession_number"])
    db.close()
    client = Success()
    assert edgar_batch.recover_batch(manifest, pilot, database, digest, "Research contact@example.org",
                                     opener=client, sleep=lambda _: None, progress=lambda *a, **k: None) == {
                                         "SKIPPED_EXHAUSTED": 1, "BUDGET_EXHAUSTED": 1, "ATTEMPTS_CUMULATIVE": 3}
    assert client.calls == 0
    with pytest.raises(ValueError, match="manifest or budget mismatch"):
        edgar_batch.open_batch_db(database, "different")


def test_identity_failure_halts_without_continuing(tmp_path, monkeypatch):
    manifest, pilot, digest, rows = make_files(tmp_path, monkeypatch)
    database = tmp_path / "batch.db"
    class WrongIdentity(Success):
        def open(self, request, timeout):
            response = super().open(request, timeout)
            response.body = response.body.replace(b"1065280", b"9999999")
            return response
    client = WrongIdentity()
    result = edgar_batch.recover_batch(manifest, pilot, database, digest, "Research contact@example.org",
                                        opener=client, sleep=lambda _: None, progress=lambda *a, **k: None)
    assert result == {"UNRESOLVED": 1, "HALTED": 1, "ATTEMPTS_CUMULATIVE": 1}
    assert client.calls == 1


def test_existing_non_batch_database_is_rejected(tmp_path):
    path = tmp_path / "other.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY)")
    db.close()
    with pytest.raises(ValueError, match="without batch identity"):
        edgar_batch.open_batch_db(path, "synthetic")
