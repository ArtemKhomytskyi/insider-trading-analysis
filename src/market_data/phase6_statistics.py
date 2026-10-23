"""Statistical interfaces for Phase 6; no claims or parameter optimization."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def descriptive_returns(values: pd.Series) -> dict[str, float | int]:
    sample = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if not len(sample):
        return {"count": 0}
    return {
        "count": int(len(sample)), "mean": float(np.mean(sample)), "median": float(np.median(sample)),
        "std": float(np.std(sample, ddof=1)) if len(sample) > 1 else float("nan"),
        "positive_frequency": float(np.mean(sample > 0)), "minimum": float(np.min(sample)),
        "maximum": float(np.max(sample)),
    }


def bootstrap_mean_ci(values: pd.Series, *, iterations: int = 2000,
                      confidence: float = .95, seed: int = 0) -> tuple[float, float]:
    sample = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if not len(sample) or iterations < 1 or not 0 < confidence < 1:
        raise ValueError("A nonempty sample, positive iterations, and valid confidence are required")
    rng = np.random.default_rng(seed)
    means = rng.choice(sample, size=(iterations, len(sample)), replace=True).mean(axis=1)
    tail = (1 - confidence) / 2
    return float(np.quantile(means, tail)), float(np.quantile(means, 1 - tail))


def exact_sign_test_pvalue(values: pd.Series) -> float:
    sample = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    sample = sample[sample != 0]
    if not len(sample):
        return 1.0
    successes, n = int(np.sum(sample > 0)), len(sample)
    tail = sum(math.comb(n, k) for k in range(0, min(successes, n - successes) + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def validate_benchmark_mapping(event_date: str, identifier: str | None,
                               valid_from: str | None, valid_to: str | None,
                               rationale: str | None) -> None:
    if not identifier or not valid_from or not rationale:
        raise ValueError("Benchmark identifier, inception/valid-from date, and rationale are required")
    if event_date < valid_from:
        raise ValueError("Benchmark did not exist for the event date")
    if valid_to and event_date > valid_to:
        raise ValueError("Benchmark mapping was no longer valid for the event date")
