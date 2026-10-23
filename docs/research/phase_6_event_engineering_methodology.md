# Phase 6 event engineering methodology

## Status and scope

The SEC event layer is implemented for the pinned CRM, NFLX, and TSLA pilot.
It contains 33,986 transaction rows, 6,067 verified filing disclosures, and
2,564 issuer-entry-session events. The provider-independent return engine is
implemented and validated with hand-calculated synthetic prices.

Real return and benchmark computation is **blocked**. The local 2017 archive
has unknown adjustment semantics, unresolved upstream provenance, no SPY or
sector benchmark series, and an owner-acceptance record that explicitly does
not authorize return/alpha computation. No archive close is silently presented
as a validated adjusted total-return price.

Final classification: **EVENT_ENGINE_READY**.

## Three data layers

1. **Transaction:** one cleaned, non-derivative reported transaction line. The
   source transaction ID remains the reconstruction key. A transaction is not
   assumed to be an independent information event.
2. **Disclosure:** one verified EDGAR accession. A filing may contain several
   transaction lines and reporting owners. The accession is the primary event
   unit because it represents one disclosure package.
3. **Event:** either a filing-level disclosure or an issuer-entry-session
   aggregate. The latter combines all accessions for one issuer that map to the
   same conservative entry session and preserves the contributing accessions.

Deterministic IDs are SHA-256-derived from the stable source identity and an
explicit aggregation prefix. Every aggregate can be reconstructed from its
filing accessions and transaction IDs.

## Eligibility and economic interpretation

The pilot is pinned by the approved historical security mappings and verified
6,067-accession EDGAR manifest. Every pinned transaction is retained for lag
accounting. Gross purchase/sale values are aggregated only when the row is an
open-market purchase or sale with positive shares, positive price, and a valid
transaction value. Derivative exercises, gifts, tax dispositions, missing
prices, and other codes are not silently treated as discretionary P/S value.

The acquisition/disposition code, transaction code/category, insider role,
form type, amendment flag, and source transaction remain available. “Insider
transaction” here means a cleaned SEC-reported transaction row, not necessarily
an economically discretionary trade.

## Filing lag

`calendar_days_to_disclose` is filing date minus transaction date at date
precision. `business_days_to_disclose` uses a documented SEC/federal-business
day convention: weekdays excluding observed US federal holidays. It is not an
exchange-session count; Good Friday is an exchange closure but not generally a
federal holiday, while Columbus Day and Veterans Day are federal holidays but
NYSE sessions. The distinction is intentional and tested.

Negative lags are flagged. Missing dates and lags over 30 calendar days receive
quality reason codes but are preserved. Long lags can be legitimate historical
reporting and are not automatically errors. Pre-specified business-day buckets
are `0`, `1`, `2`, `3-5`, `6-10`, and `>10`, plus `NEGATIVE` and `MISSING`.

Form 4 generally must be filed by the end of the second business day after the
transaction. This is a regulatory reference point, not an automatic exclusion
rule. See the [SEC Form 4 instructions](https://www.sec.gov/files/form4.pdf).

## Event timing

Transaction date is never the primary tradeable event date. The baseline
policy is `NEXT_SESSION_AFTER_FILING_DATE_CLOSE`:

1. take the official filing date;
2. find the first valid NYSE regular session strictly after it;
3. enter at that session's adjusted close;
4. count forward outcomes in valid sessions from that close.

This conservative rule avoids claiming same-day executability and intentionally
forgoes possible same-day reaction. It does not claim to identify the earliest
publicly executable instant.

The acceptance sensitivity policy is
`NEXT_FULL_SESSION_AFTER_ACCEPTANCE_DATE_CLOSE`. Verified acceptance values are
interpreted in `America/New_York`, with historical EST/EDT rules. They are
classified as before open, regular hours, after close, or non-session. Entry is
still the next full session, never the acceptance session.

Acceptance time is an EDGAR administrative timestamp, not proof of public
dissemination. The SEC also distinguishes acceptance and official filing date.
Section 16 Forms 3/4/5 have special same-day treatment through 10 p.m.; see the
[SEC EDGAR overview](https://www.sec.gov/info/edgar/regoverview.htm),
[EDGAR Filer Manual](https://www.sec.gov/info/edgar/specifications/edgarfm-vol2-v53.pdf),
and [Form 3/4/5 electronic-filing rule](https://www.sec.gov/files/rules/final/33-8230.htm).

## Trading sessions

The offline pilot calendar models NYSE weekdays, regular holidays, Good Friday,
Juneteenth from its applicable era, known exceptional full closures relevant
to the supported period (including 2007 presidential mourning and Hurricane
Sandy), and common 13:00 ET early closes. Session entry uses valid sessions,
never calendar offsets. Early closes affect acceptance classification but do
not become half observations.

The in-repository calendar is bounded research infrastructure, not an exchange-
licensed perpetual calendar. Before expanding beyond the tested era, compare it
against an authoritative exchange calendar. NYSE publishes current holiday and
early-close schedules on its official market-hours calendar.

## Amendments and repeated disclosures

Form 4/A is a separate disclosure version at its own filing/acceptance time. It
does not retroactively replace the information set at the original filing.
Original and amendment records remain linked only through preserved source
identities; no unproven underlying-transaction match is invented. Ambiguous
amendment relationships must be quarantined before inferential use.

Multiple transaction lines within a filing collapse to one filing event.
Multiple accessions for the same issuer and entry session collapse to one
issuer-session event while retaining every accession. Mixed purchase/sale
filings preserve gross sides and are classified from validated net reported
value only.

## Forward returns

For horizon `h` in 1, 5, 20, 60, 90, and 180 sessions:

`R(h) = adjusted_close(entry+h) / adjusted_close(entry) - 1`.

The price validator requires unique `(identifier, trading_date)` rows and
finite positive adjusted closes. Missing entry bars are distinct from
non-trading days. Prices are never forward-filled. If the series ends before a
horizon, that horizon is `RIGHT_CENSORED`; shorter complete horizons remain.

An acceptable adjusted series must have documented, reproducible split and
cash-dividend treatment. A column name is not proof. Split-adjusted-only and
total-return-compatible series cannot be mixed. Delistings, cash mergers,
spinoffs, symbol changes, currencies, and terminal values require explicit
evidence. The current archive fails this gate.

## Benchmarks

SPY and stock returns must use exactly the same entry and exit sessions and the
same adjustment convention. `stock return - SPY return` is called market-
adjusted or benchmark-relative return, not factor-model alpha.

Sector mapping must be point-in-time. Present-day classifications cannot be
backfilled. ETF inception dates are hard availability boundaries; a missing
historical ETF is `UNAVAILABLE`, not synthetically backfilled. CRM, NFLX, and
TSLA each require dated sector-classification evidence and a benchmark with
licensed, validated adjusted history. None is presently approved.

Interfaces can later support market-model, beta/CAPM, factor, and calendar-time
models, but these are not results of this phase.

## Dependence, overlap, and inference

The output reports transaction, filing, and issuer-session counts separately.
For each horizon it flags same-issuer event windows that overlap. All events are
retained for descriptive analysis; a later sensitivity sample may select
non-overlapping observations using a pre-specified chronological rule.

Inference must address issuer and calendar-time dependence, confidence
intervals, sign tests, bootstrap sensitivity, and outliers. With only three
issuer clusters, conventional cluster-robust inference is unreliable. Naive
independent-row t-tests are insufficient. Multiple horizons, directions,
roles, lag buckets, timing policies, tickers, and benchmarks create a serious
multiple-testing problem; exploratory results must be labeled and cannot be
optimized on the reporting sample.

The design follows the event-definition, sample-selection, normal-return, and
event-window principles described by MacKinlay, “Event Studies in Economics and
Finance,” *Journal of Economic Literature* 35(1), 1997,
[bibliographic record](https://ideas.repec.org/a/aea/jeclit/v35y1997i1p13-39.html).

## Biases and permissible conclusions

The three successful firms are not a representative US market sample. The
pilot has severe issuer/sector concentration, survivorship and selection risk,
historical ticker/action uncertainty, and possible missing delisting/terminal
outcomes. The archive ends in 2017 and cannot support 2018-2026 returns. Results
cannot be generalized to all insiders or listed companies.

This phase can establish reproducible event timing and calculation mechanics.
It cannot establish insider alpha, robust statistical significance, publication
rights for market data, or a production trading strategy.

## Outputs and reproducibility

Run offline:

```powershell
.\.venv\Scripts\python.exe -m src.market_data.phase6_pipeline
```

Derived outputs live under `data/exports/research/phase_6/`. SEC and EDGAR raw
tables/evidence are opened read-only and never changed. CSV is used because
`pyarrow` is not an approved dependency. Empty return/benchmark files and
coverage reports deliberately preserve the blocked state rather than fabricate
performance.
