import csv
import json
import sqlite3
from pathlib import Path

import pytest

from src.market_data.archive_pilot import inspect, run


MAP = "m" * 64


def setup_case(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    with (source / "ABC.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows([["Date", "Open", "High", "Low", "Close", "Volume"],
                          ["2017-01-03", 10, 12, 9, 11, 100],
                          ["2017-01-04", 11, 12, 10, 11, 100],
                          ["2017-01-12", 20, 21, 19, 20, 100],
                          ["2017-01-12", 20, 21, 19, 20, 100],
                          ["2017-01-14", 20, 21, 19, 20, 100]])
    source_db = tmp_path / "production.db"
    conn = sqlite3.connect(source_db)
    conn.executescript("CREATE TABLE security_identifier_mapping(mapping_id TEXT,issuer_cik TEXT,market_data_symbol TEXT,security_class TEXT,valid_from TEXT,valid_to TEXT,is_ambiguous INTEGER,is_reused_ticker INTEGER,requires_review INTEGER);"
                       "CREATE TABLE transaction_market_mapping(transaction_id TEXT,mapping_id TEXT,transaction_date TEXT);"
                       "CREATE TABLE clean_insider_transactions(transaction_id TEXT,transaction_code TEXT);")
    conn.execute("INSERT INTO security_identifier_mapping VALUES (?,?,?,?,?,?,?,?,?)",
                 (MAP, "0000000001", "ABC", "COMMON", "2017-01-01", "2017-12-31", 0, 0, 0))
    for qa_id, mapping_id, before, expected in __import__("src.market_data.archive_pilot", fromlist=["QA"]).QA:
        for i in range(expected):
            tx = f"{qa_id}-{i}"
            conn.execute("INSERT INTO transaction_market_mapping VALUES (?,?,?)",
                         (tx, mapping_id, "2017-01-03" if before else "2009-03-01"))
    conn.execute("INSERT INTO transaction_market_mapping VALUES (?,?,?)", ("good", MAP, "2017-01-03"))
    conn.execute("INSERT INTO clean_insider_transactions VALUES (?,?)", ("good", "P"))
    conn.commit()
    conn.close()
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["security_id", "issuer_cik", "ticker", "mapping_id", "security_class",
                         "valid_from", "valid_to", "identity_status", "identity_evidence"])
        writer.writerow(["security-1", "0000000001", "ABC", MAP, "COMMON", "2017-01-01",
                         "2017-11-10", "APPROVED", "Independent dated SEC filing URL"])
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"observed_headers": [["Date", "Open", "High", "Low", "Close", "Volume"]],
                                  "columns": {k: k.title() for k in ("date", "open", "high", "low", "close", "volume")},
                                  "price_basis": "UNKNOWN", "methodology_evidence": ""}), encoding="utf-8")
    rights = tmp_path / "rights.json"
    rights.write_text(json.dumps({"source_url": "https://example.org/archive", "license_label": "CC0 uploader claim",
                                  "provenance_limitations": "Upstream unknown", "reviewed_at": "2026-10-16"}), encoding="utf-8")
    return source, schema, manifest, source_db, tmp_path / "pilot.db", rights


def test_inspect_actual_schema(tmp_path):
    source, *_ = setup_case(tmp_path)
    result = inspect(source)
    assert result["headers"] == [("Date", "Open", "High", "Low", "Close", "Volume")]
    assert len(result["files"][0]["sha256"]) == 64


def test_import_isolated_and_quarantined(tmp_path):
    inputs = setup_case(tmp_path)
    before = inputs[3].stat().st_size
    result = run(*inputs)
    assert result["bars"] == 3
    assert result["qa_quarantine"] == 81
    assert result["findings"] == {"DUPLICATE_BAR": 1, "INVALID_BAR": 1,
                                   "MISSING_INTERVAL": 1, "POSSIBLE_ACTION_OR_JUMP": 1}
    assert inputs[3].stat().st_size == before
    with sqlite3.connect(inputs[4]) as db:
        assert db.execute("SELECT total,qa_excluded,price_window_covered FROM cohort").fetchone() == (1, 0, 1)
        assert db.execute("SELECT open_source_text,close_source_text,adjustment_semantics,source_row "
                          "FROM bars WHERE trading_date='2017-01-03'").fetchone() == ("10", "11", "UNKNOWN", 2)
        assert db.execute("SELECT COUNT(*) FROM qa_quarantine").fetchone()[0] == 81


def test_pending_never_autoapproved(tmp_path):
    source, schema, manifest, source_db, pilot_db, rights = setup_case(tmp_path)
    contents = manifest.read_text(encoding="utf-8").replace("APPROVED", "PENDING")
    manifest.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="unapproved"):
        run(source, schema, manifest, source_db, pilot_db, rights)
    assert not pilot_db.exists()


def test_schema_change_rejected(tmp_path):
    source, schema, manifest, source_db, pilot_db, rights = setup_case(tmp_path)
    spec = json.loads(schema.read_text(encoding="utf-8"))
    spec["observed_headers"] = [["Date", "Adjusted Close"]]
    schema.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="headers differ"):
        run(source, schema, manifest, source_db, pilot_db, rights)


def test_observed_txt_layout_mirror_and_empty_file(tmp_path):
    source, schema, manifest, source_db, pilot_db, rights = setup_case(tmp_path)
    raw = (source / "ABC.csv").read_bytes()
    (source / "ABC.csv").unlink()
    canonical = source / "Data" / "Stocks"
    mirror = source / "Stocks"
    canonical.mkdir(parents=True)
    mirror.mkdir()
    (canonical / "abc.us.txt").write_bytes(raw)
    (mirror / "abc.us.txt").write_bytes(raw)
    (canonical / "empty.us.txt").write_bytes(b"")
    report = inspect(source)
    assert report["empty_files"] == 1
    result = run(source, schema, manifest, source_db, pilot_db, rights)
    assert result["bars"] == 3
    assert result["source_files"] == 3
