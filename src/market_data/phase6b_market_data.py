"""Offline, fail-closed market-data catalog and Phase 6 adapter."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.market_data.phase6_engine import HORIZONS, UsEquityCalendar, benchmark_adjusted, forward_returns


@dataclass(frozen=True)
class SecurityIdentity:
    security_id: str
    issuer_cik: str
    symbol: str
    provider_symbol: str
    valid_from: str
    valid_to: str | None
    currency: str
    exchange: str
    security_type: str
    evidence: str

    def applies(self, day: str) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True)
class BenchmarkMapping:
    issuer_cik: str
    sector: str
    benchmark_id: str
    valid_from: str
    valid_to: str | None
    benchmark_inception: str
    evidence: str
    status: str


def resolve_identity(identities: list[SecurityIdentity], issuer_cik: str, day: str) -> SecurityIdentity:
    matches = [item for item in identities if item.issuer_cik == issuer_cik and item.applies(day)]
    if len(matches) != 1 or not matches[0].evidence:
        raise ValueError("Security identity is missing or ambiguous at event date")
    return matches[0]


def resolve_benchmark(mappings: list[BenchmarkMapping], issuer_cik: str, day: str) -> str:
    matches = [item for item in mappings if item.issuer_cik == issuer_cik and item.valid_from <= day
               and (item.valid_to is None or day <= item.valid_to)]
    if len(matches) != 1 or matches[0].status != "APPROVED" or not matches[0].evidence:
        raise ValueError("Point-in-time sector benchmark mapping unavailable")
    if day < matches[0].benchmark_inception:
        raise ValueError("Benchmark predates inception")
    return matches[0].benchmark_id


def validate_actions(actions: pd.DataFrame, prices: pd.DataFrame) -> list[dict]:
    issues: list[dict] = []
    if actions.duplicated(["security_id", "event_date", "action_type"]).any():
        issues.append({"code": "DUPLICATE_ACTION", "severity": "FAIL"})
    for row in actions.itertuples(index=False):
        if row.action_type == "SPLIT":
            if not math.isfinite(float(row.ratio)) or float(row.ratio) <= 0:
                issues.append({"code": "INVALID_SPLIT_RATIO", "severity": "FAIL", "date": row.event_date})
            if not getattr(row, "evidence", None):
                issues.append({"code": "SPLIT_EVIDENCE_MISSING", "severity": "BLOCKED", "date": row.event_date})
        elif row.action_type == "CASH_DIVIDEND":
            if not math.isfinite(float(row.cash)) or float(row.cash) < 0:
                issues.append({"code": "INVALID_DIVIDEND", "severity": "FAIL", "date": row.event_date})
            if not getattr(row, "evidence", None):
                issues.append({"code": "DIVIDEND_EVIDENCE_MISSING", "severity": "BLOCKED", "date": row.event_date})
        else:
            issues.append({"code": "UNSUPPORTED_ACTION", "severity": "BLOCKED", "date": row.event_date})
    return issues


CRITICAL_GATES = ("G1_PROVENANCE", "G2_RIGHTS", "G3_IDENTITY", "G4_SESSIONS", "G5_ACTIONS",
                  "G6_PRICES", "G7_COVERAGE", "G8_BENCHMARK", "G9_REPRODUCIBILITY", "G10_AUTHORIZATION")


def evaluate_gates(manifest: dict, prices: pd.DataFrame, identities: list[SecurityIdentity],
                   actions: pd.DataFrame, benchmark_required: bool = False) -> dict[str, dict]:
    def gate(passed: bool, reason: str) -> dict:
        return {"status": "PASS" if passed else "BLOCKED", "reason": reason}

    required = ("provider", "retrieved_at_utc", "source", "snapshot_sha256", "schema_version",
                "coverage_start", "coverage_end", "currency", "exchange", "adjustment_policy")
    results = {"G1_PROVENANCE": gate(all(manifest.get(k) for k in required), "Pinned source metadata required"),
               "G2_RIGHTS": gate(manifest.get("storage_rights") is True and manifest.get("research_rights") is True,
                                 "Storage and research use must be documented"),
               "G3_IDENTITY": gate(bool(identities) and all(x.evidence for x in identities), "Dated identity evidence required"),
               "G5_ACTIONS": gate(manifest.get("actions_validated") is True and not validate_actions(actions, prices),
                                  "Corporate actions need independent validation"),
               "G8_BENCHMARK": gate(not benchmark_required or manifest.get("benchmark_validated") is True,
                                    "Benchmark coverage and adjustment validation required"),
               "G9_REPRODUCIBILITY": gate(manifest.get("snapshot_hash_verified") is True,
                                          "Content hash must match pinned snapshot"),
               "G10_AUTHORIZATION": gate(manifest.get("owner_authorized") is True,
                                         "Research use must be authorized")}
    expected_columns = {"identifier", "trading_date", "adjusted_close", "currency"}
    columns_ok = expected_columns <= set(prices.columns)
    duplicates = columns_ok and prices.duplicated(["identifier", "trading_date"]).any()
    values = pd.to_numeric(prices["adjusted_close"], errors="coerce") if columns_ok else pd.Series(dtype=float)
    valid_values = columns_ok and bool(len(values)) and bool(values.notna().all()) and bool((values > 0).all()) and bool(values.map(math.isfinite).all())
    results["G6_PRICES"] = gate(valid_values and not duplicates, "Unique finite positive adjusted closes required")
    calendar = UsEquityCalendar()
    sessions_ok = columns_ok and all(calendar.is_session(date.fromisoformat(str(x))) for x in prices["trading_date"])
    results["G4_SESSIONS"] = gate(sessions_ok, "All bars must map to valid exchange sessions")
    results["G7_COVERAGE"] = gate(manifest.get("coverage_validated") is True, "Event entry and exit coverage required")
    if manifest.get("adjustment_policy") != "VALIDATED_TOTAL_RETURN":
        results["G5_ACTIONS"] = gate(False, "Unknown or non-total-return adjustment convention")
    return results


def store_snapshot(root: Path, raw_bytes: bytes, manifest: dict) -> Path:
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if manifest.get("snapshot_sha256") != digest:
        raise ValueError("Snapshot content hash mismatch")
    folder = root / digest
    if folder.exists():
        if (folder / "raw.csv").read_bytes() != raw_bytes or json.loads(
            (folder / "manifest.json").read_text(encoding="utf-8")) != manifest:
            raise ValueError("Existing snapshot differs")
        return folder
    folder.mkdir(parents=True)
    (folder / "raw.csv").write_bytes(raw_bytes)
    (folder / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return folder


def validated_returns(events: pd.DataFrame, prices: pd.DataFrame, manifest: dict,
                      identities: list[SecurityIdentity], actions: pd.DataFrame,
                      benchmark: pd.DataFrame | None = None,
                      benchmark_mappings: list[BenchmarkMapping] | None = None) -> pd.DataFrame:
    gates = evaluate_gates(manifest, prices, identities, actions, benchmark is not None)
    blocked = [name for name, result in gates.items() if result["status"] != "PASS"]
    if blocked:
        raise ValueError("Market-data gates blocked: " + ", ".join(blocked))
    event_rows = events.copy()
    event_rows["identifier"] = [resolve_identity(identities, str(row.issuer_cik), str(row.entry_session)).security_id
                                for row in event_rows.itertuples(index=False)]
    if set(prices["currency"]) != {manifest["currency"]}:
        raise ValueError("Currency mismatch")
    calendar = UsEquityCalendar()
    for identifier, group in prices.groupby("identifier"):
        dates = sorted(date.fromisoformat(str(value)) for value in group["trading_date"])
        if dates != calendar.sessions(dates[0], dates[-1]):
            raise ValueError(f"Missing exchange session in price series: {identifier}")
    for event in event_rows.itertuples(index=False):
        group = prices.loc[prices["identifier"] == event.identifier]
        available = set(group["trading_date"].astype(str))
        if event.entry_session not in available:
            raise ValueError(f"Missing entry price for event: {event.event_id}")
        sessions = calendar.sessions(date.fromisoformat(event.entry_session),
                                     date.fromisoformat(str(group["trading_date"].max())))
        if len(sessions) <= max(HORIZONS):
            raise ValueError(f"Insufficient exit coverage for event: {event.event_id}")
    calculated = forward_returns(event_rows, prices[["identifier", "trading_date", "adjusted_close"]])
    if benchmark is not None:
        if not benchmark_mappings:
            raise ValueError("Dated benchmark mappings required")
        calculated["benchmark_identifier"] = [resolve_benchmark(benchmark_mappings, str(row.issuer_cik),
                                                                  str(row.entry_session))
                                              for row in calculated.itertuples(index=False)]
        if {"identifier", "trading_date", "adjusted_close", "currency"} - set(benchmark.columns):
            raise ValueError("Benchmark bars lack required provenance fields")
        if set(benchmark["currency"]) != {manifest["currency"]}:
            raise ValueError("Benchmark currency mismatch")
        for row in calculated.itertuples(index=False):
            stock_dates = prices.loc[prices["identifier"] == row.identifier, "trading_date"].astype(str).tolist()
            bench_dates = benchmark.loc[benchmark["identifier"] == row.benchmark_identifier,
                                         "trading_date"].astype(str).tolist()
            start = row.entry_session
            required = [day.isoformat() for day in calendar.sessions(date.fromisoformat(start),
                        date.fromisoformat(start) + timedelta(days=400))[:max(HORIZONS) + 1]]
            if not set(required) <= set(stock_dates) or not set(required) <= set(bench_dates):
                raise ValueError(f"Benchmark/stock session coverage mismatch: {row.event_id}")
        calculated = benchmark_adjusted(calculated, benchmark[["identifier", "trading_date", "adjusted_close"]])
    return calculated
