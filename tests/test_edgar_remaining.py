"""Offline-only tests for the remaining EDGAR accession plan."""

import csv
import hashlib
import sqlite3

import pytest

from src.market_data import edgar_batch, edgar_remaining
from src.market_data.edgar_full_manifest import FIELDS
from tests.test_edgar_batch import Success, make_files


def test_partition_deduplicates_verified_and_separates_exhausted():
    rows = [{"accession_number": str(i)} for i in range(4)]
    standard, retry = edgar_remaining.partition_remaining(rows, {"0", "2"}, "1")
    assert [r["accession_number"] for r in standard] == ["3"]
    assert [r["accession_number"] for r in retry] == ["1"]
    with pytest.raises(ValueError, match="partition"):
        edgar_remaining.partition_remaining(rows + [rows[0]], {"0"}, "1")
    with pytest.raises(ValueError, match="partition"):
        edgar_remaining.partition_remaining(rows, {"0", "1"}, "1")


def test_manifest_is_immutable_and_hash_pinned(tmp_path):
    path = tmp_path / "manifest.csv"
    row = {field: "" for field in FIELDS}
    row["accession_number"] = "0000000000-17-000001"
    digest = edgar_remaining.write_csv_immutable(path, [row])
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert edgar_remaining.write_csv_immutable(path, [row]) == digest
    row["ticker"] = "NFLX"
    with pytest.raises(ValueError, match="overwrite"):
        edgar_remaining.write_csv_immutable(path, [row])
    with pytest.raises(ValueError, match="hash mismatch"):
        edgar_remaining.require_hash(path, "0" * 64)


def test_prior_verified_registry_blocks_repeat_without_http(tmp_path, monkeypatch):
    manifest, pilot, digest, rows = make_files(tmp_path, monkeypatch)
    prior = tmp_path / "prior.db"
    db = sqlite3.connect(prior)
    db.execute("CREATE TABLE evidence(accession_number TEXT, verification_status TEXT)")
    db.execute("INSERT INTO evidence VALUES(?,?)", (rows[0]["accession_number"], "VERIFIED_RAW"))
    db.commit(); db.close()
    client = Success()
    with pytest.raises(ValueError, match="previously verified"):
        edgar_batch.recover_batch(manifest, pilot, tmp_path / "new.db", digest,
                                  "Research contact@example.org", opener=client,
                                  verified_evidence_dbs=[prior], sleep=lambda _: None)
    assert client.calls == 0
    assert not (tmp_path / "new.db").exists()


def test_prior_registry_requires_existing_distinct_database(tmp_path, monkeypatch):
    manifest, pilot, digest, rows = make_files(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Missing or self-referential"):
        edgar_batch.recover_batch(manifest, pilot, tmp_path / "new.db", digest,
                                  "Research contact@example.org", opener=Success(),
                                  verified_evidence_dbs=[tmp_path / "missing.db"], sleep=lambda _: None)


def test_batch_restart_keeps_cumulative_attempt_budget(tmp_path, monkeypatch):
    manifest, pilot, digest, rows = make_files(tmp_path, monkeypatch)
    database = tmp_path / "batch.db"
    first = Success()
    result = edgar_batch.recover_batch(manifest, pilot, database, digest,
                                        "Research contact@example.org", opener=first,
                                        sleep=lambda _: None, progress=lambda *a, **k: None)
    assert result["ATTEMPTS_CUMULATIVE"] == 2
    second = Success()
    result = edgar_batch.recover_batch(manifest, pilot, database, digest,
                                        "Research contact@example.org", opener=second,
                                        sleep=lambda _: None, progress=lambda *a, **k: None)
    assert result["ATTEMPTS_CUMULATIVE"] == 2
    assert result["SKIPPED_VERIFIED"] == 2
    assert second.calls == 0


def test_controlled_retry_rejects_any_unpinned_accession_before_network():
    client = Success()
    with pytest.raises(ValueError, match="pinned controlled-retry"):
        edgar_remaining.recover_controlled(
            "0000000000-00-000000", "Research contact@example.org", opener=client,
            sleep=lambda _: None)
    assert client.calls == 0
