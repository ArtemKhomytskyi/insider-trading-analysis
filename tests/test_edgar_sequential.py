"""Synthetic-only tests; no SEC endpoint is opened."""

import hashlib
import json

import pytest

from src.market_data import edgar_sequential as seq


def fixture_runner(tmp_path, monkeypatch, behavior=None):
    items = [{"batch": f"{n:03d}", "manifest": f"batch_{n:03d}.csv", "rows": 1, "sha256": "pin"}
             for n in range(2, 17)]
    plan = {"batches": items}
    full = [{"accession_number": "pilot"}]
    states = {"pilot": {"status": "FAILED", "attempts": 3},
              "acc002": {"status": "VERIFIED_RAW", "attempts": 1}}
    calls = []
    plan_file = tmp_path / "plan.json"
    plan_file.write_text("pinned", encoding="utf-8")
    monkeypatch.setattr(seq.remaining, "PLAN", plan_file)
    monkeypatch.setattr(seq, "PROGRESS", tmp_path / "progress.json")
    monkeypatch.setattr(seq, "LOCK", tmp_path / "runner.lock")
    monkeypatch.setattr(seq.remaining, "load_plan", lambda: (plan, full))
    monkeypatch.setattr(seq.remaining, "evidence_state", lambda *_: (dict(states), []))
    monkeypatch.setattr(seq, "batch_rows", lambda item: [{"accession_number": "acc" + item["batch"]}])
    monkeypatch.setattr(seq, "validate_global", lambda *_: {
        "verified_total": 218 + sum(v["status"] == "VERIFIED_RAW" for k, v in states.items() if k != "pilot"),
        "remaining_normal": 6067 - 1 - (218 + sum(v["status"] == "VERIFIED_RAW" for k, v in states.items() if k != "pilot")),
        "controlled_retry": 1})
    monkeypatch.setattr(seq.remaining, "status", lambda *_: {
        "accessions": 6067, "transactions": 33986,
        "status": {"VERIFIED_RAW": 218 + sum(v["status"] == "VERIFIED_RAW" for k, v in states.items() if k != "pilot"),
                   "CONTROLLED_RETRY": 1}})
    def recover(number, user_agent, progress, halt_on_429):
        assert halt_on_429 is True
        calls.append(number)
        if behavior:
            behavior(number, states)
        else:
            states["acc" + number] = {"status": "VERIFIED_RAW", "attempts": 1}
        return {"VERIFIED_RAW": 1, "ATTEMPTS_CUMULATIVE": 1}
    monkeypatch.setattr(seq.remaining, "recover_planned", recover)
    return plan, states, calls


def test_batch_boundaries_and_predecessor_gate(tmp_path, monkeypatch):
    plan, states, _ = fixture_runner(tmp_path, monkeypatch)
    assert [x["batch"] for x in seq.validate_boundaries(plan, "003", "005", 3)] == ["003", "004", "005"]
    assert len(seq.validate_boundaries(plan, "006", "015", 10)) == 10
    with pytest.raises(ValueError, match="Maximum"):
        seq.validate_boundaries(plan, "006", "016", 11)
    with pytest.raises(ValueError, match="reversed"):
        seq.validate_boundaries(plan, "005", "003", 1)
    with pytest.raises(ValueError, match="three-digit"):
        seq.validate_boundaries(plan, "3", "005", 1)
    with pytest.raises(RuntimeError, match="Earlier batch 003"):
        seq.validate_predecessors(plan, "004", states)


def test_offline_preflight_persists_global_progress(tmp_path, monkeypatch):
    fixture_runner(tmp_path, monkeypatch)
    result = seq.preflight()
    assert result["state"] == "READY"
    assert result["completed_batches"] == ["002"]
    assert result["next_batch"] == "003"
    assert result["verified_total"] == 219
    assert json.loads(seq.PROGRESS.read_text(encoding="utf-8")) == result


def test_interruption_checkpoint_and_restart_skip_completed(tmp_path, monkeypatch):
    interrupted = {"once": False}
    def behavior(number, states):
        if number == "004" and not interrupted["once"]:
            interrupted["once"] = True
            raise KeyboardInterrupt()
        states["acc" + number] = {"status": "VERIFIED_RAW", "attempts": 1}
    _, states, calls = fixture_runner(tmp_path, monkeypatch, behavior)
    with pytest.raises(KeyboardInterrupt):
        seq.run_sequential("003", "005", 3, "Research contact@example.org", emit=lambda *a, **k: None)
    saved = json.loads(seq.PROGRESS.read_text(encoding="utf-8"))
    assert saved["state"] == "INTERRUPTED"
    assert saved["current_batch"] == "004"
    assert saved["verified_total"] == 220
    assert saved["completed_batches"] == ["002", "003"]
    assert saved["next_batch"] == "004"
    result = seq.run_sequential("003", "005", 3, "Research contact@example.org", emit=lambda *a, **k: None)
    assert result["state"] == "COMPLETED"
    assert result["completed_this_run"] == ["004", "005"]
    assert result["completed_batches"] == ["002", "003", "004", "005"]
    assert result["next_batch"] == "006"
    assert calls == ["003", "004", "004", "005"]
    assert states["acc003"]["status"] == "VERIFIED_RAW"


def test_maximum_stops_without_touching_later_batches(tmp_path, monkeypatch):
    _, _, calls = fixture_runner(tmp_path, monkeypatch)
    result = seq.run_sequential("003", "006", 2, "Research contact@example.org", emit=lambda *a, **k: None)
    assert result["state"] == "LIMIT_REACHED"
    assert result["next_batch"] == "005"
    assert calls == ["003", "004"]


def test_ten_batch_limit_and_sequential_order(tmp_path, monkeypatch):
    _, states, calls = fixture_runner(tmp_path, monkeypatch)
    for number in range(3, 6):
        states[f"acc{number:03d}"] = {"status": "VERIFIED_RAW", "attempts": 1}
    result = seq.run_sequential("006", "016", 10, "Research contact@example.org", emit=lambda *a, **k: None)
    assert result["state"] == "LIMIT_REACHED"
    assert result["next_batch"] == "016"
    assert calls == [f"{number:03d}" for number in range(6, 16)]


def test_ten_batch_interruption_restart_skips_verified_batches(tmp_path, monkeypatch):
    interrupted = {"once": False}
    def behavior(number, states):
        if number == "011" and not interrupted["once"]:
            interrupted["once"] = True
            raise KeyboardInterrupt()
        states["acc" + number] = {"status": "VERIFIED_RAW", "attempts": 1}
    _, states, calls = fixture_runner(tmp_path, monkeypatch, behavior)
    for number in range(3, 6):
        states[f"acc{number:03d}"] = {"status": "VERIFIED_RAW", "attempts": 1}
    with pytest.raises(KeyboardInterrupt):
        seq.run_sequential("006", "015", 10, "Research contact@example.org", emit=lambda *a, **k: None)
    assert json.loads(seq.PROGRESS.read_text(encoding="utf-8"))["next_batch"] == "011"
    result = seq.run_sequential("006", "015", 10, "Research contact@example.org", emit=lambda *a, **k: None)
    assert result["state"] == "COMPLETED"
    assert result["next_batch"] == "016"
    assert calls == [f"{number:03d}" for number in range(6, 12)] + [f"{number:03d}" for number in range(11, 16)]
    assert result["completed_this_run"] == [f"{number:03d}" for number in range(11, 16)]


def test_partial_batch_completion_is_resumed_without_repeating_verified(tmp_path, monkeypatch):
    _, states, _ = fixture_runner(tmp_path, monkeypatch)
    for number in range(3, 8):
        states[f"acc{number:03d}"] = {"status": "VERIFIED_RAW", "attempts": 1}
    states["acc008a"] = {"status": "VERIFIED_RAW", "attempts": 1}
    states["acc008b"] = {"status": "ATTEMPTED_NO_EVIDENCE", "attempts": 1}
    original_rows = seq.batch_rows
    monkeypatch.setattr(seq, "batch_rows", lambda item: (
        [{"accession_number": "acc008a"}, {"accession_number": "acc008b"}]
        if item["batch"] == "008" else original_rows(item)))
    calls = []
    def resume(number, user_agent, progress, halt_on_429):
        calls.append(number)
        assert number == "008" and halt_on_429 is True
        assert states["acc008a"]["status"] == "VERIFIED_RAW"
        states["acc008b"] = {"status": "VERIFIED_RAW", "attempts": 2}
        return {"VERIFIED_RAW": 1, "SKIPPED_VERIFIED": 1, "ATTEMPTS_CUMULATIVE": 3}
    monkeypatch.setattr(seq.remaining, "recover_planned", resume)
    messages = []
    result = seq.run_sequential("008", "008", 1, "Research contact@example.org",
                                emit=lambda message, **_: messages.append(json.loads(message)))
    assert calls == ["008"]
    assert next(m for m in messages if m["state"] == "START")["already_verified"] == 1
    assert result["state"] == "COMPLETED"
    assert result["next_batch"] == "009"
    assert states["acc008a"]["attempts"] == 1


def test_terminal_evidence_stops_before_http_and_checkpoints(tmp_path, monkeypatch):
    _, states, calls = fixture_runner(tmp_path, monkeypatch)
    states["acc003"] = {"status": "UNRESOLVED", "attempts": 1}
    with pytest.raises(RuntimeError, match="manual review"):
        seq.run_sequential("003", "005", 3, "Research contact@example.org", emit=lambda *a, **k: None)
    assert calls == []
    assert json.loads(seq.PROGRESS.read_text(encoding="utf-8"))["state"] == "STOPPED"


def test_identity_conflict_stops_before_next_batch(tmp_path, monkeypatch):
    def behavior(number, states):
        states["acc" + number] = {"status": "CONFLICT", "attempts": 1}
    _, _, calls = fixture_runner(tmp_path, monkeypatch, behavior)
    with pytest.raises(RuntimeError, match="did not validate"):
        seq.run_sequential("003", "005", 3, "Research contact@example.org", emit=lambda *a, **k: None)
    assert calls == ["003"]
    assert json.loads(seq.PROGRESS.read_text(encoding="utf-8"))["state"] == "STOPPED"


def test_batch_sha_pin_and_checkpoint_plan_pin(tmp_path, monkeypatch):
    manifest = tmp_path / "batch_003.csv"
    manifest.write_text("original", encoding="utf-8")
    monkeypatch.setattr(seq.remaining, "EXPORTS", tmp_path)
    monkeypatch.setattr(seq.remaining, "read_rows", lambda *_: [{"accession_number": "x"}])
    item = {"batch": "003", "manifest": manifest.name, "rows": 1,
            "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
    assert len(seq.batch_rows(item)) == 1
    manifest.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        seq.batch_rows(item)
    seq.save_snapshot(tmp_path / "checkpoint.json", {"schema_version": 1, "plan_sha256": "old"})
    with pytest.raises(ValueError, match="different plan"):
        seq.load_snapshot(tmp_path / "checkpoint.json", "new")


def test_runner_lock_prevents_parallel_run(tmp_path):
    with seq.single_runner_lock(tmp_path / "runner.lock"):
        with pytest.raises(RuntimeError, match="Another EDGAR sequential runner"):
            with seq.single_runner_lock(tmp_path / "runner.lock"):
                pass
