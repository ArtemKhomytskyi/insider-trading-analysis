"""Bounded sequential EDGAR batch runner; LIVE only when ``run`` is invoked.

The runner never creates manifests or changes production data. Every batch is
processed through the existing resume-safe, accession-level recovery path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.market_data import edgar_remaining as remaining

MAX_BATCHES_PER_RUN = 10
PROGRESS = remaining.EXPORTS / "edgar_sequential_progress.json"
LOCK = remaining.PROCESSED / "edgar_sequential.lock"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def single_runner_lock(path: Path):
    """Hold an OS byte-range lock; a crashed process releases it automatically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
        except OSError as exc:
            raise RuntimeError("Another EDGAR sequential runner holds the lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.lockf(handle.fileno(), fcntl.LOCK_UN, 1)


def batch_rows(item: dict) -> list[dict[str, str]]:
    path = remaining.EXPORTS / item["manifest"]
    remaining.require_hash(path, item["sha256"])
    rows = remaining.read_rows(path)
    if len(rows) != item["rows"]:
        raise ValueError(f"Batch {item['batch']} manifest row count changed")
    return rows


def batch_state(item: dict, evidence: dict[str, dict]) -> dict:
    rows = batch_rows(item)
    statuses = [evidence.get(r["accession_number"], {}).get("status", "QUEUED") for r in rows]
    verified = statuses.count("VERIFIED_RAW")
    return {"batch": item["batch"], "expected": len(rows), "verified": verified,
            "status": "COMPLETE" if verified == len(rows) else "INCOMPLETE",
            "terminal": [r["accession_number"] for r, state in zip(rows, statuses)
                         if state in {"FAILED", "UNRESOLVED", "CONFLICT"}],
            "exhausted": [r["accession_number"] for r in rows
                          if evidence.get(r["accession_number"], {}).get("attempts", 0) >= 3 and
                          evidence.get(r["accession_number"], {}).get("status") != "VERIFIED_RAW"],
            "unverified": [r["accession_number"] for r, state in zip(rows, statuses) if state != "VERIFIED_RAW"]}


def validate_boundaries(plan: dict, start: str, end: str, maximum: int) -> list[dict]:
    if not re.fullmatch(r"\d{3}", start) or not re.fullmatch(r"\d{3}", end):
        raise ValueError("Batch boundaries must be three-digit numbers")
    if not 1 <= maximum <= MAX_BATCHES_PER_RUN:
        raise ValueError(f"Maximum batches must be 1..{MAX_BATCHES_PER_RUN}")
    numbers = [item["batch"] for item in plan["batches"]]
    if start not in numbers or end not in numbers or numbers.index(start) > numbers.index(end):
        raise ValueError("Start/end batch not in the pinned plan or reversed")
    return plan["batches"][numbers.index(start):numbers.index(end) + 1]


def validate_predecessors(plan: dict, start: str, evidence: dict[str, dict]) -> None:
    for item in plan["batches"]:
        if item["batch"] == start:
            break
        state = batch_state(item, evidence)
        if state["status"] != "COMPLETE":
            raise RuntimeError(f"Earlier batch {item['batch']} is incomplete; sequential advance blocked")


def validate_global(plan: dict, full: list[dict[str, str]], evidence: dict[str, dict]) -> dict:
    if len(full) != 6067 or len({r["accession_number"] for r in full}) != 6067:
        raise ValueError("Full accession coverage changed")
    exhausted = evidence.get(remaining.EXHAUSTED_ACCESSION)
    if not exhausted or exhausted["status"] != "FAILED" or exhausted["attempts"] != 3:
        raise ValueError("Exhausted pilot accession left its controlled unresolved queue")
    verified = sum(x["status"] == "VERIFIED_RAW" for x in evidence.values())
    if verified > len(full) - 1:
        raise ValueError("Verified coverage exceeds controlled universe")
    return {"verified_total": verified, "remaining_normal": len(full) - verified - 1,
            "controlled_retry": 1,
            "http_attempts_total": sum(x.get("attempts", 0) for x in evidence.values())}


def global_batch_progress(plan: dict, evidence: dict[str, dict]) -> dict:
    states = [(item["batch"], batch_state(item, evidence)["status"]) for item in plan["batches"]]
    return {"completed_batches": [number for number, state in states if state == "COMPLETE"],
            "next_batch": next((number for number, state in states if state != "COMPLETE"), None)}


def load_snapshot(path: Path, plan_sha: str) -> dict | None:
    if not path.is_file():
        return None
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("schema_version") != 1 or saved.get("plan_sha256") != plan_sha:
        raise ValueError("Existing global checkpoint belongs to a different plan")
    return saved


def save_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def preflight() -> dict:
    """Offline-only evidence audit and durable global starting checkpoint."""
    with single_runner_lock(LOCK):
        plan, full = remaining.load_plan()
        evidence, _ = remaining.evidence_state(full, plan)
        validate_predecessors(plan, "003", evidence)
        totals = validate_global(plan, full, evidence)
        plan_sha = remaining.digest(remaining.PLAN)
        prior = load_snapshot(PROGRESS, plan_sha)
        if prior and prior.get("verified_total", 0) > totals["verified_total"]:
            raise ValueError("Verified count regressed since previous checkpoint")
        snapshot = {"schema_version": 1, "plan_sha256": plan_sha, "updated_at_utc": utc_now(),
                    "state": "READY", "current_batch": None, **global_batch_progress(plan, evidence), **totals}
        save_snapshot(PROGRESS, snapshot)
        return snapshot


def run_sequential(start: str, end: str, max_batches: int, user_agent: str, emit=print) -> dict:
    """Run at most ten incomplete batches, in order; stop on any failed row."""
    with single_runner_lock(LOCK):
        plan, full = remaining.load_plan()  # checks all pinned manifest hashes
        selected = validate_boundaries(plan, start, end, max_batches)
        plan_sha = remaining.digest(remaining.PLAN)
        prior = load_snapshot(PROGRESS, plan_sha)
        evidence, _ = remaining.evidence_state(full, plan)
        validate_predecessors(plan, start, evidence)
        totals = validate_global(plan, full, evidence)
        if prior and prior.get("verified_total", 0) > totals["verified_total"]:
            raise ValueError("Verified count regressed since previous checkpoint")
        snapshot = {"schema_version": 1, "plan_sha256": plan_sha, "updated_at_utc": utc_now(),
                    "state": "READY", "requested_start": start, "requested_end": end,
                    "max_batches": max_batches, "current_batch": None, "completed_this_run": [],
                    **global_batch_progress(plan, evidence), **totals}
        save_snapshot(PROGRESS, snapshot)
        attempted = 0
        try:
            for item in selected:
                number = item["batch"]
                current = batch_state(item, evidence)
                if current["status"] == "COMPLETE":
                    emit(json.dumps({"batch": number, "state": "SKIPPED_VERIFIED", **totals}), flush=True)
                    continue
                if current["terminal"] or current["exhausted"]:
                    raise RuntimeError(f"Batch {number} has prior terminal or exhausted filings; manual review required")
                if attempted >= max_batches:
                    break
                attempted += 1
                snapshot.update(state="RUNNING", current_batch=number, updated_at_utc=utc_now())
                save_snapshot(PROGRESS, snapshot)
                emit(json.dumps({"batch": number, "state": "START", "already_verified": current["verified"],
                                 "expected": current["expected"]}), flush=True)
                result = remaining.recover_planned(number, user_agent, progress=emit, halt_on_429=True)
                evidence, _ = remaining.evidence_state(full, plan)
                completed = batch_state(item, evidence)
                totals = validate_global(plan, full, evidence)
                if completed["status"] != "COMPLETE" or any(key.startswith("HALTED") or key == "BUDGET_EXHAUSTED"
                                                                  for key in result):
                    raise RuntimeError(f"Batch {number} did not validate: {len(completed['unverified'])} unresolved; {result}")
                report = remaining.status(plan)
                if (report["accessions"] != 6067 or report["transactions"] != 33986 or
                        report["status"].get("VERIFIED_RAW", 0) != totals["verified_total"] or
                        report["status"].get("CONTROLLED_RETRY", 0) != 1):
                    raise RuntimeError(f"Batch {number} global coverage does not reconcile")
                snapshot["completed_this_run"].append(number)
                snapshot.update(state="CHECKPOINT", current_batch=None, updated_at_utc=utc_now(),
                                **global_batch_progress(plan, evidence), **totals)
                save_snapshot(PROGRESS, snapshot)
                emit(json.dumps({"batch": number, "state": "VERIFIED_RAW", "expected": completed["expected"],
                                 "verified": completed["verified"], "http_attempts_cumulative": result.get("ATTEMPTS_CUMULATIVE"),
                                 **totals}), flush=True)
            finished_range = all(batch_state(item, evidence)["status"] == "COMPLETE" for item in selected)
            snapshot.update(state="COMPLETED" if finished_range else "LIMIT_REACHED",
                            current_batch=None, updated_at_utc=utc_now(),
                            **global_batch_progress(plan, evidence), **totals)
            save_snapshot(PROGRESS, snapshot)
            return snapshot
        except KeyboardInterrupt:
            try:
                latest, _ = remaining.evidence_state(full, plan)
                snapshot.update(**global_batch_progress(plan, latest), **validate_global(plan, full, latest))
            except Exception as audit_exc:
                snapshot["checkpoint_audit_error"] = f"{type(audit_exc).__name__}: {audit_exc}"
            snapshot.update(state="INTERRUPTED", updated_at_utc=utc_now())
            save_snapshot(PROGRESS, snapshot)
            raise
        except BaseException as exc:
            try:
                latest, _ = remaining.evidence_state(full, plan)
                snapshot.update(**global_batch_progress(plan, latest), **validate_global(plan, full, latest))
            except Exception as audit_exc:
                snapshot["checkpoint_audit_error"] = f"{type(audit_exc).__name__}: {audit_exc}"
            snapshot.update(state="STOPPED", stop_reason=f"{type(exc).__name__}: {exc}", updated_at_utc=utc_now())
            save_snapshot(PROGRESS, snapshot)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "run"], help="preflight is OFFLINE; run is LIVE")
    parser.add_argument("--start-batch")
    parser.add_argument("--end-batch")
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--user-agent")
    args = parser.parse_args()
    if args.command == "preflight":
        if any(x is not None for x in (args.start_batch, args.end_batch, args.max_batches, args.user_agent)):
            parser.error("preflight accepts no live recovery options")
        print(json.dumps(preflight(), indent=2))
        return
    if any(x is None for x in (args.start_batch, args.end_batch, args.max_batches, args.user_agent)):
        parser.error("run requires start/end batch, max batches, and user-agent")
    try:
        result = run_sequential(args.start_batch, args.end_batch, args.max_batches, args.user_agent)
    except KeyboardInterrupt:
        print("Interrupted; attempt reservations and global checkpoint are preserved.")
        raise SystemExit(130)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
