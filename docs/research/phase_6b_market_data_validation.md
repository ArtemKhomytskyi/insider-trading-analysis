# Phase 6B market-data validation

## Result

Provider-independent identity, action-validation, content-addressed storage,
quality gates and Phase 6 return adapter are implemented. No real market-price
series is approved for return research. Synthetic adapter tests pass; real
return and benchmark files remain unavailable.

## Provider assessment

Status is `UNVERIFIED` unless a cited source and the intended rights have been
reviewed for the current acquisition. Existing repository research is recorded
in `phase_5_2_zero_budget_market_data_strategy.md`.

| Source | Current assessment | Reason |
|---|---|---|
| Local Kaggle/Stooq-derived archive | UNSUITABLE | Adjustment basis unknown, upstream provenance unresolved, no SPY; owner approval excludes return research. |
| Yahoo/yfinance | UNSUITABLE for automated bulk use | Existing repository rights review finds automation/publication restrictions; adjusted close alone does not establish total-return semantics. |
| Tiingo Starter | UNSUITABLE | Repository rights review finds persistent-storage restrictions. |
| Stooq direct | UNVERIFIED | Bulk/storage and adjustment terms are not established. |
| Alpha Vantage free | UNSUITABLE for target period | Repository review found free daily depth insufficient. |
| FMP | UNVERIFIED | Current historical entitlement, storage and publication rights require direct review. |
| Nasdaq Data Link | UNVERIFIED | Dataset-specific coverage and rights must be assessed. |
| Massive/Polygon free | UNSUITABLE for target period | Repository review found limited free historical depth; official provider guidance confirms split-adjusted aggregates are not dividend-adjusted. |
| EODHD free | UNSUITABLE for target period | Repository review found limited free history and call allowance. |
| Institutional licensed source | CONDITIONALLY_SUITABLE | Could provide permanent identifiers, actions and delisting coverage; cost and license not approved. |

Zero-cost candidate for further review: a separately licensed, documented
institutional or public research dataset. No current zero-cost source passes
all rights and quality gates. Low-cost and institutional choices cannot be
ranked definitively without current quotes and contract review. No purchases
or market-data downloads occurred.

Primary-source feature checks: [Massive/Polygon adjustment convention](https://polygon.io/knowledge-base/article/is-polygons-stock-data-adjusted-for-splits-or-dividends), [CRSP security and delisting data](https://www.crsp.org/crsp_pdf/crsp-us-stock-indexes-databases-data-descriptions-guide-crspaccess/), and [Nasdaq corporate-action dataset](https://data.nasdaq.com/databases/WCA). These establish feature differences, not contract rights or pilot coverage. Licensing, historical API entitlement, redistribution and local retention remain unresolved with each vendor.

## Historical coverage requirement

The pinned filing-event export and `issuer_session_events.csv` define required
entry dates. The 180-session exit must be observed from a validated calendar,
and each benchmark must cover the same entry/exit sessions. Current pinned
EDGAR cohort: CRM 5,022 filings, NFLX 1,009, TSLA 36; 2,564 issuer-session
events total. Approved SEC identity intervals in the existing archive manifest
start CRM 2006-01-01, NFLX 2006-01-05, TSLA 2017-02-01 and end 2017-11-10;
these are identity evidence intervals, not proof of price availability.

| Security | Issuer CIK | Required benchmark | Availability |
|---|---|---|---|
| CRM | 0001108524 | SPY and dated sector benchmark | BLOCKED |
| NFLX | 0001065280 | SPY and dated sector benchmark | BLOCKED |
| TSLA | 0001318605 | SPY and dated sector benchmark | BLOCKED |
| SPY | ETF identity to verify | None | BLOCKED: no validated series |

## Security master and benchmark mapping

`SecurityIdentity` has stable local security ID, issuer CIK, historical and
provider symbols, dated validity, currency, exchange, type and evidence.
Resolution requires exactly one dated match. `BenchmarkMapping` records dated
sector assignment, benchmark ID, inception, evidence and approval. Lookup
rejects overlap, missing evidence and pre-inception events. No modern sector
classification is backfilled for CRM, NFLX or TSLA. SPY and sector histories
remain unapproved.

## Corporate actions

The validator recognizes split ratios and cash dividends with evidence
fields. Invalid ratios, negative dividends, duplicates, missing
evidence and unsupported action types produce findings. It does not yet
reconstruct an independent total-return series or cross-check action factors
against a second provider. Other actions such as
mergers, spinoffs and delistings remain unresolved and block the action gate.
No real pilot split or SPY dividend is claimed independently validated. The
adapter accepts only an explicitly validated total-return convention; it does
not apply a split twice or synthesize dividend factors.

## Storage and gates

Raw input is stored by SHA-256 under a versioned snapshot directory alongside
a manifest. Repeated identical writes reuse the same snapshot only if the
manifest also matches; hash mismatch fails. Atomic writes and immutable
retention controls are not implemented. Normalized validated prices are passed to the existing
`forward_returns` engine only after ten gates pass:

G1 provenance, G2 rights, G3 identity, G4 sessions, G5 actions, G6 prices,
G7 coverage, G8 benchmark, G9 reproducibility and G10 owner authorization.
Each returns a status and reason. The current archive fails G2, G5, G7, G8
and G10, so it cannot enter the adapter. The test fixture passes the gates
using explicit synthetic authorization and adjustment evidence.

## Reproducibility and limits

The fixture validates snapshot reuse, exact hash checking, dated identity,
ETF inception and integration with one-session returns. Missing or duplicate
bars, unsupported actions and incomplete rights fail closed. The existing
Phase 6 event timing policies and SEC evidence were not changed.

The adapter rejects missing calendar sessions, missing entry/180-session exit
coverage and benchmark/session mismatches rather than letting row-count
horizons drift. This is an offline proof with synthetic data. Vendor-grade
reconciliation, independent corporate-action validation, artifact-level
validated-layer persistence, rights evidence and real SPY/sector coverage
are still required before market-data infrastructure can be declared fully
ready for empirical return research.

Phase 6C requires an approved source with raw and adjusted observations,
independent corporate-action records, SPY, dated sector classification,
documented storage and research rights, and a validated full coverage matrix.
Cost for the full SEC universe is unknown until vendor quotes and an explicit
universe size/coverage requirement are established.
