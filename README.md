# Insider Research

Research platform for SEC insider trading filings (Forms 3, 4, and 5). The project ingests official SEC quarterly datasets, stores an immutable raw layer in SQLite, and will later support cleaning, normalization, enrichment, analytics, and scoring.

## Current phase: Phase 5.2 — controlled historical market-data pilot preparation

Phase 5.2A now includes a separate **offline-only historical archive importer**. It accepts only a manually supplied local ZIP or extracted directory, requires observed-schema review and manually approved historical identities, and writes only to a new pilot SQLite file. No archive has been downloaded or imported. See [the offline archive pilot instructions](docs/research/phase_5_2a_offline_archive_pilot.md) and [the strict $0 strategy](docs/research/phase_5_2_zero_budget_market_data_strategy.md). The candidate archive stops on 2017-11-10, its upstream market-data provenance is unresolved, and adjusted/total-return semantics must not be assumed.

Phase 1.1 ingests raw SEC data. Phase 1.2 audits file headers. Phase 2 profiles the SQLite database and builds taxonomies. **Phase 3** creates `clean_insider_transactions` — a normalized, enriched table with quality flags and an MVP alpha universe marker.

Phase 4 and Phase 5.1b production builds are validated. Phase 5.2 has an isolated pilot path; no production price download has been run. Returns, alpha, and insider scores are not implemented yet.

## Expected raw folder structure

Place each SEC quarter in its own folder under `data/raw/sec/`:

```
data/raw/sec/
├── 2006_Q1/
│   ├── SUBMISSION.tsv
│   ├── REPORTINGOWNER.tsv
│   ├── NONDERIV_TRANS.tsv
│   ├── NONDERIV_HOLDING.tsv
│   ├── DERIV_TRANS.tsv
│   ├── DERIV_HOLDING.tsv
│   ├── FOOTNOTES.tsv
│   └── OWNER_SIGNATURE.tsv
├── 2006_Q2/
│   └── ...
└── 2025q1_form345/
    └── ...
```

Folder names may use flexible period patterns, for example:

- `2006_Q1`, `2006q1`, `2006-Q1`, `2006 Q1`, `2006_1`
- `2025q1_form345` (year/quarter parsed from the folder name)

Unknown `.tsv` files are ignored.

## Source metadata columns

Every ingested row includes:

| Column | Description |
|--------|-------------|
| `source_file` | Path relative to `data/raw/sec/` (e.g. `2006_Q1/SUBMISSION.tsv`) |
| `source_folder` | Quarter folder name (e.g. `2006_Q1`) |
| `source_year` | Parsed filing period year |
| `source_quarter` | Parsed filing period quarter (1–4) |
| `ingested_at` | UTC timestamp when the row was loaded |
| `row_hash` | SHA-256 of row values + `source_file` + `source_folder` (deduplication key) |

`source_year` / `source_quarter` come from the folder name, not from filing content. They identify which SEC quarterly dataset the row came from.

## Immutable raw layer

- Raw tables store SEC values as `TEXT` without cleaning or filtering.
- Do **not** manually edit rows in `data/processed/insider_research.db`.
- Re-runs are safe: duplicate rows are skipped via `INSERT OR IGNORE` on `row_hash`.
- Use `--reset` only when you intentionally want to drop and rebuild all raw tables.

## Setup

```bash
cd insider-research
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Download quarterly ZIP files from the SEC:
https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets

Extract each quarter into `data/raw/sec/<period_folder>/`.

## Initialize database (optional)

Schema is applied automatically before ingestion. To initialize manually:

```bash
python -m src.utils.init_db
```

## Run full SEC ingestion

From the project root:

```bash
python -m src.ingestion.load_all_sec_datasets
```

Or:

```bash
python src/ingestion/load_all_sec_datasets.py
```

Options:

| Flag | Description |
|------|-------------|
| `--raw-dir PATH` | Raw SEC root (default: `data/raw/sec`) |
| `--db-path PATH` | SQLite file (default: `data/processed/insider_research.db`) |
| `--reset` | Drop raw tables, recreate schema, reload everything |
| `--chunk-size N` | Pandas chunk size (default: `50000`) |
| `--verbose` | Debug logging |

### Full reload example

```bash
python -m src.ingestion.load_all_sec_datasets --reset
```

### Custom paths example

```bash
python -m src.ingestion.load_all_sec_datasets ^
  --raw-dir data/raw/sec ^
  --db-path data/processed/insider_research.db
```

After ingestion, the script prints per-table insert/skip counts, folder count, year range, missing files per folder, and total runtime.

## Phase 1.2 — SEC file structure audit

### Why this matters

SEC quarterly ZIPs span many years. Column names, order, or presence can drift between periods. Catching that at the **file header** level is fast and prevents silent data loss when we build cleaned tables.

The audit reads **only the first line** of each TSV (no data rows, no database writes, no file modifications).

### Run the audit

```bash
python -m src.ingestion.audit_sec_file_structure
```

Options:

| Flag | Description |
|------|-------------|
| `--raw-dir PATH` | Raw SEC root (default: `data/raw/sec`) |
| `--out-dir PATH` | Report output (default: `data/exports/schema_audit`) |
| `--verbose` | Debug logging |

### Output files

Written to `data/exports/schema_audit/`:

| File | Contents |
|------|----------|
| `schema_audit_summary.csv` | One row per file type (SUBMISSION.tsv, etc.) with counts and status |
| `schema_audit_details.csv` | One row per file with missing/extra columns, order diffs, read status |
| `canonical_columns.json` | Canonical schema per file type, all columns seen, schema versions, rename hints |

### Interpreting status

| Status | Meaning |
|--------|---------|
| **OK** | One schema version across all folders; no missing/extra columns, order issues, empty files, or unreadable files |
| **REVIEW** | Any drift detected — inspect `schema_audit_details.csv` and `canonical_columns.json` before cleaning |

Check `schema_audit_details.csv` for columns like `missing_columns`, `extra_columns`, `order_differs`, and `duplicate_columns`. Use `renamed_column_candidates` in the JSON when a column may have been renamed (e.g. `AFF10B5ONE` vs `AFF_10B5ONE`).

## Phase 2 — initial raw data audit

### Why this matters

Before building a clean transaction layer, we need to understand what landed in SQLite: dominant Form types, transaction codes, title noise, missing fields, filing lag, and data-quality edge cases.

### Run the audit notebook

```bash
jupyter notebook notebooks/01_raw_data_audit.ipynb
```

Or run all exports programmatically:

```python
from src.analytics.raw_data_audit import run_full_audit
from src.utils.db import get_connection, get_database_path

conn = get_connection(get_database_path())
run_full_audit(conn)
conn.close()
```

### Outputs

Written to `data/exports/raw_data_audit/`:

| File | Description |
|------|-------------|
| `table_overview.csv` | Row counts, date ranges, year coverage |
| `filing_type_distribution.csv` | Document types and filings by year/quarter |
| `transaction_code_distribution.csv` | Code frequencies (nonderiv + deriv) |
| `security_type_distribution.csv` | Top `SECURITY_TITLE` values |
| `ownership_distribution.csv` | Direct/indirect and nature of ownership |
| `officer_title_distribution.csv` | Raw officer title frequencies |
| `missing_value_report.csv` | Null rates for key fields |
| `distribution_summary.csv` | Quantiles for shares, price, implied value, lag |
| `filing_lag_analysis.csv` | Filing lag buckets and yearly averages |
| `yearly_activity_summary.csv` | Filings, buys, sells, unique issuers per year |
| `data_quality_flags.csv` | Negative shares, future dates, duplicates, etc. |
| `executive_summary.md` | Key findings and recommended next-step rules |
| `charts/` | Matplotlib PNGs (no seaborn) |

This phase is **read-only** — no rows are deleted or transformed in the database.

## Phase 2.5 — audit fixes and cleaning preparation

### Why filing lag was fixed

SEC raw dates use **`DD-MON-YYYY`** (e.g. `01-APR-2008`, `29-MAR-2006`). The original SQL date parser only handled `YYYY-MM-DD` / `YYYYMMDD`, so filing lag returned zero rows. Phase 2.5 parses dates in Python via `src/utils/sec_parsing.py`.

### Date parsing (`parse_sec_date`)

Supports:

- `YYYY-MM-DD`
- `YYYYMMDD`
- `DD-MON-YYYY` / `DD-MON-YY`
- Blank → `None`

### Numeric audit (`audit_numeric_fields`)

Audits `TRANS_SHARES`, `TRANS_PRICEPERSHARE`, `SHRS_OWND_FOLWNG_TRANS`, `VALU_OWND_FOLWNG_TRANS` for parseable vs footnote text, zeros, negatives, and quantiles. Exports:

- `numeric_casting_audit.csv`
- `numeric_unparseable_samples.csv`

### Normalization maps (draft)

| File | Purpose |
|------|---------|
| `configs/normalization/security_title_map.csv` | Raw `SECURITY_TITLE` → normalized type |
| `configs/normalization/officer_title_map.csv` | Raw `RPTOWNER_TITLE` → role + weight |

These are **draft** lookup tables, not applied to raw data yet.

### Commands

```bash
# Inspect raw field samples
python -m src.analytics.debug_raw_fields

# Fixed filing lag only (~7M rows; use --sample-mode for quick test)
python -m src.analytics.run_filing_lag_audit

# Numeric casting audit
python -m src.analytics.raw_data_audit --section numeric

# Re-run filing lag + numeric via audit module
python -m src.analytics.raw_data_audit --section filing_lag
```

### What is still NOT cleaned

- No `clean_insider_transactions` table
- No P/S filtering
- No title normalization applied to rows
- No alpha, scores, or dashboard

## Phase 2.2b — transaction code taxonomy

### Why raw SEC codes are noisy

Form 4 filings use single-letter **transaction codes** for many event types. Raw counts are dominated by **A** (grants), **M** (exercises), **F** (tax withholding), and **S** (sales) — not open-market purchases. Treating every row as an "insider trade signal" would mix compensation and mechanical events with discretionary buys/sells.

### Informational vs administrative filings

| Type | Examples | Research use |
|------|----------|----------------|
| Informational / discretionary | **P**, **S** (open market) | Primary MVP signal universe |
| Compensation / plan | **A**, **F** | Low signal; exclude from MVP |
| Derivative / mechanical | **M**, **C**, **X**, **O** | Low signal; often precedes sales |
| Transfer / corporate | **G**, **D**, **U**, **W** | Very low signal |

### Config files

| File | Purpose |
|------|---------|
| `configs/normalization/transaction_code_map.csv` | Semantic taxonomy (category, signal quality, MVP flag) |
| `data/exports/reference/sec_transaction_code_reference.csv` | SEC official descriptions (reference) |

### Helpers (`src/utils/sec_taxonomy.py`)

- `load_transaction_code_map()`
- `get_transaction_code_metadata(code)`
- `is_open_market_code(code)`
- `is_high_signal_code(code)`

### Run taxonomy analysis

```bash
python -m src.analytics.raw_data_audit --section taxonomy
```

Exports:

- `data/exports/raw_data_audit/transaction_code_taxonomy_coverage.csv`
- `charts/transaction_code_categories.png`
- `charts/signal_quality_distribution.png`

**Phase 3** will use `include_clean_mvp` to filter rows — the raw database is not filtered in this phase.

## Phase 2.3 — security  type taxonomy

### Why SECURITY_TITLE is noisy

The same economic security appears as `Common Stock`, `COMMON STOCK`, `Class A Common Stock`, `Ordinary Shares`, etc. Compensation and derivative labels (`Restricted Stock Units`, `Stock Option`, `Phantom Stock`) are mixed into non-derivative tables and must not be treated as common equity alpha signals.

### MVP universe: common equity

Phase 3 clean layer will target **COMMON_EQUITY** rows with `include_clean_mvp=true` — discretionary open-market activity in ordinary/common shares.

Excluded from MVP (but retained in raw data):

- **STOCK_COMPENSATION** — RSUs, restricted stock, performance units
- **OPTION_DERIVATIVE** — options and rights to buy
- **PHANTOM_OR_DEFERRED** — phantom and deferred units
- **PREFERRED_EQUITY** — preferred share classes
- **COMMON_UNITS_OR_PARTNERSHIP** — LP/LLC units (`requires_review=true`)
- **WARRANT**, **DEBT_OR_NOTE**, **RIGHT_OR_UNIT**, **UNKNOWN**

### Config and helpers

| File | Purpose |
|------|---------|
| `configs/normalization/security_title_map.csv` | Ordered patterns → normalized type (first match wins) |
| `src/utils/security_taxonomy.py` | `normalize_security_title()`, `classify_security_title()`, `is_common_equity()` |

Edit the CSV to add patterns or change MVP flags. More specific patterns must appear **above** broad patterns (e.g. `RESTRICTED STOCK UNIT` before `COMMON STOCK`).

### Run analysis

```bash
python -m src.analytics.raw_data_audit --section security-taxonomy
```

Exports:

- `data/exports/raw_data_audit/security_type_taxonomy_coverage.csv`
- `data/exports/raw_data_audit/security_type_taxonomy_summary.csv`
- `data/exports/raw_data_audit/security_type_unmapped_or_review.csv`
- `charts/security_type_taxonomy_distribution.png`
- `charts/security_type_mvp_vs_excluded.png`

Raw `SECURITY_TITLE` values are never modified in SQLite.

## Phase 2.4 — insider role taxonomy

### Why insider roles matter

The same insider may appear as `CEO`, `Chief Executive Officer`, or `President and CEO`. Role hierarchy affects how we interpret trades: C-suite executives often have greater information access than directors or 10% passive owners — a hypothesis tested later, not assumed as alpha.

### Why raw SEC titles are noisy

`RPTOWNER_TITLE` is free text. `RPTOWNER_RELATIONSHIP` provides structured flags (`Director`, `Officer`, `TenPercentOwner`) but must be parsed separately.

### Config and helpers

| File | Purpose |
|------|---------|
| `configs/normalization/officer_title_map.csv` | Ordered title patterns → normalized role, group, weight |
| `src/utils/role_taxonomy.py` | `classify_role()`, `parse_relationship_flags()`, `get_role_weight()` |

Role weights (e.g. CEO=1.00, CFO=0.90, Director=0.60) are **provisional informational priors** for future scoring — not return forecasts.

### Run analysis

```bash
python -m src.analytics.raw_data_audit --section role-taxonomy
```

Exports:

- `role_taxonomy_coverage.csv`
- `role_taxonomy_summary.csv`
- `role_taxonomy_unmapped_or_review.csv`
- `charts/role_group_distribution.png`
- `charts/executive_vs_nonexecutive.png`
- `charts/role_weight_distribution.png`

## Phase 3 — core cleaning layer

### Raw vs clean

- **Raw tables** (`raw_nonderiv_trans`, `raw_submission`, `raw_reporting_owner`, …) remain the immutable source of truth. They are never deleted, mutated, or filtered in place.
- **`clean_insider_transactions`** is a new derived table: parsed dates and numbers, taxonomy labels, filing lag, transaction value, data-quality flags, and `include_mvp_alpha`.

Form 3, Form 5, amendments, non-P/S transaction codes, and non-common-equity securities **stay in the clean table** with `include_mvp_alpha = 0` so future research is not blocked. MVP is a boolean flag only.

### MVP alpha universe (`include_mvp_alpha`)

A row is in the initial research universe when **all** of the following hold:

- Form 4 (not an amendment, unless configured)
- Transaction code taxonomy `include_clean_mvp` (typically P and S)
- Security taxonomy `include_clean_mvp` (typically common equity)
- `is_valid_clean_event` (valid dates, positive shares and price, non-negative filing lag, plausible value)
- `transaction_value >= minimum_transaction_value` (default **$10,000**, configurable)

Config: `configs/cleaning.yaml`

### Build clean layer

```bash
python -m src.cleaning.build_clean_insider_transactions --reset
python -m src.cleaning.build_clean_insider_transactions --reset --limit 100000
```

Options: `--db-path`, `--config`, `--chunk-size`, `--limit`, `--verbose`

Exports: `data/exports/cleaning/`

Notebook: `notebooks/02_clean_transaction_layer.ipynb`

**Not in Phase 3:** market data download, forward returns, alpha, or insider scoring.

## Phase 3.5 — clean layer validation & QA

### Why validation exists

Before joining market prices, we verify that `clean_insider_transactions` is internally consistent: row counts, MVP integrity rules, survival funnel, distributions, filing lag, transaction values, duplicates, yearly coverage, and ticker readiness.

### What it checks

Read-only SQL and pandas audits grouped into sections A–K (see `src/analytics/validate_clean_layer.py`). Each check records **PASS**, **WARN**, or **FAIL** with metrics and recommended actions.

| Severity | Interpretation |
|----------|----------------|
| **PASS** | Check satisfied |
| **WARN** | Review recommended (e.g. high UNKNOWN role share, extreme filing lag) |
| **FAIL** | Fix clean build or config before Phase 4 (e.g. MVP integrity violations, duplicate `transaction_id`) |

Validation does **not** mutate raw or clean tables.

### Run validation

```bash
python -m src.analytics.validate_clean_layer
python -m src.analytics.validate_clean_layer --verbose
```

Options: `--db-path`, `--out-dir`, `--config`

### Outputs

`data/exports/validation/`:

- `validation_summary.csv` — master check list
- `row_count_summary.csv`, `filter_survival_funnel.csv`, `mvp_integrity_violations.csv`
- Transaction / security / role distributions
- Filing lag and transaction value summaries
- `yearly_mvp_activity.csv`, `ticker_readiness_summary.csv`, `top_tickers_mvp.csv`
- `charts/` — funnel, lag, value, role, yearly plots

Notebook: `notebooks/03_clean_layer_validation.ipynb`

Thresholds: `configs/cleaning.yaml` (`validation` section)

## Phase 3.6 — clean layer fixes & refinement

### Why these fixes matter

Phase 3.5 validation showed the MVP universe was internally consistent but had data-quality edge cases: invalid ticker placeholders (`NONE`, `n/a`), multi-symbol tickers, stale filings (>365 day lag), and transaction dates before the source folder year. Phase 3.6 adds explicit flags without deleting rows.

### Ticker cleaning

New fields: `ticker_raw`, `ticker_clean`, `is_valid_ticker`, `is_invalid_ticker_placeholder`, `is_multisymbol_ticker`.

`include_mvp_alpha` requires a valid, single-symbol ticker. Invalid placeholders remain in the clean table for research but are excluded from MVP.

### Filing quality

- `is_stale_filing` — filing lag > `stale_filing_lag_days` (default 365)
- `filing_lag_bucket` — SAME_DAY through OVER_365_DAYS

Stale filings are flagged, not removed by default. Set `exclude_stale_filings_from_mvp: true` in `configs/cleaning.yaml` to exclude them from MVP.

### Historical transaction dates

`is_pre_source_transaction_date` marks rows where `transaction_year < source_year` (economic date precedes the SEC quarterly dataset folder). This is expected for late-reported historical trades, not an ingestion error.

### Unknown role refinement

After rebuild, exports are written to:

- `data/exports/validation/unknown_role_refinement_candidates.csv`
- `data/exports/validation/top_unknown_roles_for_mapping.csv`

Use these to iteratively improve `configs/normalization/officer_title_map.csv` (not auto-modified).

### Rebuild clean layer

```bash
python -m src.cleaning.build_clean_insider_transactions --reset
python -m src.analytics.validate_clean_layer
```

## Phase 4 — Entity Normalization

Phase 4 moves the project from transaction-only analysis toward stable derived entities. SEC CIKs are the canonical keys because insider names, company names, and ticker symbols can change, collide, or be reused historically. The clean transaction table remains unchanged; entity tables are rebuilt from it.

New derived tables:

| Table | Purpose |
| --- | --- |
| `clean_insiders` | One row per insider CIK with deterministic canonical name, activity dates, counts, role summaries, and QA flags. |
| `insider_name_aliases` | Historical raw and normalized insider names. Same-name / different-CIK cases are reported, not merged. |
| `company_dimension` | One row per issuer CIK with latest observed company name/ticker and activity counts. |
| `company_identifier_history` | Historical ticker and company-name observations for each issuer CIK. |
| `insider_company_roles` | Observed role relationships by insider, company, role, and observed date range. |

Build the entity layer:

```bash
python -m src.entities.build_entity_dimensions --reset
```

For a bounded non-production smoke run:

```bash
python -m src.entities.build_entity_dimensions --reset --limit 100000 --no-export
```

Sample builds are marked `SAMPLE` in `entity_build_metadata` and validation will not treat them as production-complete.

Validate it:

```bash
python -m src.analytics.validate_entity_dimensions
```

Validation first checks `entity_build_metadata`. Only the latest build status `COMPLETE` is accepted as a production Phase 4 build; `IN_PROGRESS`, `FAILED`, and `SAMPLE` are rejected.

Roles are modeled as relationships between an insider, a company, and observed SEC filing dates. A person can be CFO and later CEO at the same company, or director at another company, without overwriting earlier observations. The dates are named `first_seen_date` and `last_seen_date` because SEC records do not prove exact employment boundaries.

Ticker is not a company key. Phase 4 preserves ticker/name observations in `company_identifier_history`, exports companies with multiple tickers, and exports ticker symbols associated with multiple issuer CIKs. These are QA and future market-data mapping inputs, not automatic merge rules.

`company_dimension` includes nullable fields for `sector`, `industry`, `exchange`, `market_cap`, `country`, and `currency`. Phase 4 does not populate or invent these values. The next phase is company metadata and market-data enrichment.

## Phase 5.1 - Historical Security Mapping

Phase 5.1 maps SEC insider transactions to historically observed tradable symbols without treating a ticker as a permanent company identifier. `issuer_cik` remains the canonical issuer key; ticker symbols are mutable, can change over time, and can be reused by different issuer CIKs.

New derived tables:

| Table | Purpose |
| --- | --- |
| `security_identifier_mapping` | Research-grade mapping from issuer CIK and observed ticker interval to market-data symbol, with provenance, confidence, ambiguity, ticker-change, reuse, and review flags. |
| `transaction_market_mapping` | One row per clean transaction with zero or one accepted point-in-time market symbol. Ambiguous cases remain unmapped for review. |

Build and validate:

```bash
python -m src.mapping.build_security_mapping --reset
python -m src.analytics.validate_security_mapping
```

The mapping uses transaction date for security identity. This is separate from filing date, which later governs public signal timing. Phase 5.1 does not calculate returns, alpha, or rankings.

Phase 5.1b adds deterministic ambiguity reduction without using fuzzy matching, current ticker lookups, cross-CIK merges, or share-class collapse. Transaction mappings now record `mapping_status`, raw and logical candidate counts, `resolution_method`, `ambiguity_reason`, and whether the source transaction ticker matched a date-valid candidate.

Accepted resolution methods are:

| Method | Meaning |
| --- | --- |
| `CIK_DATE_UNIQUE` | Exactly one logical issuer/date candidate exists. |
| `COLLAPSED_EQUIVALENT_CANDIDATES` | Multiple raw rows collapse to one identical logical issuer/date/ticker/symbol candidate. |
| `CIK_DATE_TICKER_EXACT` | Multiple logical issuer/date candidates exist, but exactly one matches the transaction's own cleaned ticker. |

Rows remain unmapped when no date-valid candidate exists, when multiple exact ticker candidates exist, when no exact ticker match breaks a multi-candidate tie, or when the underlying security mapping is itself ambiguous. Use the read-only diagnostic exporter for root-cause analysis:

```bash
python -m src.analytics.analyze_security_mapping_ambiguity --verbose
```

## Phase 5.2 - Market Data Acquisition

Phase 5.2 adds provider-neutral market-data infrastructure. No live provider is selected by default; `configs/market_data.yaml` starts with `provider: none`. API keys must come from environment variables and must not be committed.

New market-data tables:

| Table | Purpose |
| --- | --- |
| `market_prices_daily` | Daily raw and adjusted OHLCV with provider provenance. |
| `market_data_download_status` | Resumable symbol/date-range download ledger with status, retries, and errors. |
| `market_splits` / `market_dividends` | Optional provider-sourced corporate actions. |

Adjusted prices are required for research returns because raw close prices can be distorted by splits and dividends. Phase 5 preserves raw close and adjusted close where available and records whether full adjusted OHLC is supplied.

Validate market-data QA:

```bash
python -m src.analytics.validate_market_data
```

Phase 5 explicitly guards against look-ahead bias, survivorship bias, current-ticker bias, silent delisting exclusion, and corporate-action return distortion. Lower trustworthy coverage is preferred over falsely high coverage from guessed mappings.

### Phase 5.2 Yahoo Finance experiment (current MVP)

The ten-security Yahoo pilot is isolated from `src.market_data.download_market_data`. That production downloader and its `YahooProvider` placeholder are unchanged. The pilot reads `security_identifier_mapping` from the production SQLite file with a read-only immutable connection, and writes prices only to a separate pilot database. Selection does not contact Yahoo.

```powershell
.\.venv\Scripts\python.exe -m src.market_data.yahoo_pilot select --source-db data/processed/insider_research.db --manifest data/exports/market_data/yahoo_pilot_10_manifest.csv
```

The selection contains ten accepted mapping IDs, split between earlier and later periods and between ticker-change and other cases. It excludes known ticker reuse across issuer CIKs. The CSV includes the SEC ticker, translated Yahoo symbol, issuer CIK, observed interval, mapping confidence, and risk flags. Every row starts `PENDING`; `company_name_hint` is the latest SEC-derived name, not proof of identity at the interval. Before a run, independently verify the historical issuer, share class, Yahoo symbol, and ticker-change/delisting history for each row. Record the source in `identity_evidence` and mark only confirmed rows `APPROVED`. Matching ticker strings alone is insufficient. The run rechecks mapping IDs, CIKs, intervals, and known reuse against the read-only production mapping before any Yahoo call. Yahoo exposes no durable security identifier through this adapter, so a recycled ticker with plausible history remains a research risk.

The optional dependency is `yfinance` in `requirements.txt`; version 0.2.66 is installed in the existing project `.venv`. If rebuilding that environment, install it with `.\.venv\Scripts\python.exe -m pip install "yfinance>=0.2.66,<1.0"`. Synthetic tests do not contact Yahoo.

The [yfinance project](https://github.com/ranaroussi/yfinance/blob/main/README.md) describes the Yahoo Finance API as intended for personal use and directs users to Yahoo's terms. Review the [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) for the intended research use before any retrieval, storage, sharing, or automated schedule. There is no licensed research feed, uptime promise, stable historical identifier, or confirmed delisted coverage. Data availability and corporate-action history must be measured by the pilot rather than assumed.

After identity review and separate approval for external access, the exact bounded command is:

```powershell
.\.venv\Scripts\python.exe -m src.market_data.yahoo_pilot run --source-db data/processed/insider_research.db --manifest data/exports/market_data/yahoo_pilot_10_manifest.csv --pilot-db data/pilot/yahoo_pilot_10.db --start-date 2003-01-01 --end-date 2026-10-15 --rate-limit-seconds 2
.\.venv\Scripts\python.exe -m src.market_data.validate_pilot --pilot-db data/pilot/yahoo_pilot_10.db --out-dir data/exports/market_data/yahoo_pilot_10_validation
```

The manifest caps the run at ten identities. There is at most one retry per security, so the code invokes `history()` at most 20 times in a run; `yfinance` may make multiple underlying HTTP requests per invocation, so this is not an HTTP-request quota guarantee. Requests are sequential and clipped to the SEC-observed mapping intervals. The start date is inclusive; the adapter adds one calendar day to the inclusive end because `yfinance` treats `end` as exclusive. Completed intervals are skipped on rerun. Empty history is `NO_DATA`, exceptions become `FAILED` after retry, and rows outside the interval or large boundary gaps become `PARTIAL`. These statuses fail validation. The pilot never substitutes a current ticker for an unrecognized historical symbol.

`history(auto_adjust=False, actions=True, repair=False, interval="1d")` stores raw Open/High/Low/Close/Volume, vendor `Adj Close`, daily Dividends, and Stock Splits. It does not fabricate adjusted OHLC or adjusted volume. Yahoo reports zero split/dividend values on ordinary days; missing action columns or nonfinite data cause a failed request. Vendor `Adj Close` is recorded under `YAHOO_ADJ_CLOSE_VENDOR` and must not be mixed with raw OHLC as though they share an adjustment basis. The exact vendor adjustment chain and historical corrections are not independently verified. The pilot validator reports missing series, incomplete intervals, gaps, suspicious jumps, missing action fields, and identity flags. No events observed is a warning, not evidence that corporate actions never occurred. Delisting returns, mergers, and event-time forward returns remain unresolved research work.

The original ten-row pilot remains **NO-GO for download** because three rows were rejected and one needs review. Full-universe ingestion remains NO-GO.

#### Focused five-security preparation (no download yet)

The separately preserved `data/exports/market_data/yahoo_pilot_5_manifest.csv` contains only independently verified `NFLX`, `CRM`, `GTM`, `BKNG`, and `TSLA` rows; all remain `PENDING`. The original ten-row manifest is unchanged. [Identity evidence](data/exports/market_data/yahoo_pilot_10_identity_verification.md) establishes the issuer/ticker relationships. [Phase 5.1 QA findings](data/exports/security_mapping/phase_5_1_identity_qa_findings.md) quarantine `NLFX`, premature `HLIO`, and pre-IPO `CRWV` mappings without modifying production data.

The five SEC-observed intervals are `NFLX` 2006-01-05–2026-03-02, `CRM` 2005-02-01–2019-08-05, `GTM` 2025-05-14–2026-03-22, `BKNG` 2018-03-04–2026-03-16, and `TSLA` 2017-02-01–2026-03-06. `run_pilot` intersects a requested date window with each row's interval before calling the Yahoo adapter; a nonoverlapping row makes no request. Responses outside that intersection are not persisted and mark the request `PARTIAL`. The adapter passes an exclusive `end` one calendar day after the clipped inclusive end. A five-row approved manifest therefore permits at most **five `history()` calls without retries or ten with the one permitted retry**; internal HTTP requests made by yfinance are not bounded by that count. An all-`PENDING` manifest fails the review gate before any Yahoo call.

Only after separate approval of each row and of Yahoo usage, fill `identity_evidence` and set the chosen rows to `APPROVED` in a reviewed copy of the five-row manifest. The future live command would be:

```powershell
.\.venv\Scripts\python.exe -m src.market_data.yahoo_pilot run --source-db data/processed/insider_research.db --manifest data/exports/market_data/yahoo_pilot_5_manifest.csv --pilot-db data/pilot/yahoo_pilot_5.db --start-date 2005-02-01 --end-date 2026-03-22 --rate-limit-seconds 2
.\.venv\Scripts\python.exe -m src.market_data.validate_pilot --pilot-db data/pilot/yahoo_pilot_5.db --out-dir data/exports/market_data/yahoo_pilot_5_validation
```

Do not run these commands while approval remains `PENDING`. Identity verification does not establish Yahoo's coverage, corporate-action correctness, or suitability under Yahoo's terms; this phase remains **NO-GO for external retrieval**.

The local regression command is `.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest_phase52_run_01` (choose a fresh workspace-local basetemp path for each run on Windows). The 2026-10-16 preparation run passed all 164 tests; no Yahoo market-data request was made.

### Legacy Tiingo pilot (inactive)

The original `download_market_data` entry point is infrastructure only and must not be used for the pilot: it can select all mapped tickers and writes to the production database. The isolated pilot uses `src.market_data.pilot` and a separate SQLite file. It reads the production mapping database in read-only mode to create a 200-security manifest. No API is called by selection.

```powershell
python -m src.market_data.pilot select --source-db data/processed/insider_research.db --manifest data/exports/market_data/pilot_manifest.csv --count 200
```

Generate the row-level audit, then review every manifest row against provider metadata and the historical issuer, ticker, share class, ticker-change and corporate-action history:

```powershell
python -m src.market_data.audit_pilot_manifest --manifest data/exports/market_data/pilot_manifest.csv --source-db data/processed/insider_research.db --out data/exports/market_data/pilot_identity_audit.csv
```

Fill `verified_provider_name` with the exact Tiingo EOD metadata name reviewed for that security, verify `provider_ticker` (Tiingo uses a dash in share-class symbols), and change `review_status` from `PENDING` to `APPROVED` only after confirming the vendor symbol identifies the same security in that interval. The program rejects unapproved rows, blank reviewed names, repeated vendor symbols, and mismatched live metadata. Ambiguous and known reused SEC tickers are excluded automatically. The selection balances early/late observations and ticker-change cases. It **cannot verify sector, liquidity, delisting, merger, or share class** because these fields are absent or uncertain in the SEC-derived mapping. Add or replace cases manually if those strata are required. `is_delisted` in Phase 5.1 means "not the latest observed ticker," not confirmed exchange delisting. `security_class` is currently null. A recycled ticker with the same issuer name could still escape this check; a stable vendor security ID or independent point-in-time reference is needed for stronger assurance.

This legacy design is retained for reference. The project has no Tiingo subscription or API key, and this is not the active MVP path. The production Yahoo and Polygon/Massive adapters remain placeholders; only the separate Yahoo pilot adapter above is enabled for the experimental path. Tiingo's vendor symbol is still a ticker; `provider_security_id` is reserved for a verified durable vendor identifier, and remains null until one is available. Do not join a pre/post-change or merger series automatically.

Provider assessment (documentation reviewed October 2026; no live account verification):

| Provider | Documented strengths | Pilot limitation |
| --- | --- | --- |
| [Tiingo EOD](https://www.tiingo.com/documentation/end-of-day) | Historical raw and split/dividend adjusted OHLCV, daily split factor and cash dividend | [Symbology guidance](https://www.tiingo.com/documentation/appendix/symbology) says delisted data applies to tickers not yet recycled; durable price-series identity and account-specific coverage must be verified. Hourly/daily limits depend on account. |
| [Massive (formerly Polygon)](https://massive.com/docs/rest/stocks/overview) | Point-in-time ticker reference, split events, dividends and historical bars | [Aggregate bars are split-adjusted, not dividend-adjusted](https://massive.com/knowledge-base/article/is-massives-stock-data-adjusted-for-splits-or-dividends); total-return construction needs an additional corporate-action process. [Ticker events](https://massive.com/docs/rest/stocks/corporate-actions/ticker-events) are experimental and currently list ticker changes; subscription access requires verification. |
| Yahoo Finance | Separate ten-security experiment described above | Historical delisting, ticker identity, adjustment policy, limits, and research reproducibility remain unverified. |

After approval and manifest review, run only the selected sample:

```powershell
$env:TIINGO_API_KEY = '<key supplied outside the repository>'
python -m src.market_data.pilot run --manifest data/exports/market_data/pilot_manifest.csv --pilot-db data/pilot/market_pilot.db --start-date 2006-01-01 --end-date 2025-12-31 --rate-limit-seconds 1
python -m src.market_data.validate_pilot --pilot-db data/pilot/market_pilot.db --out-dir data/exports/market_data/pilot_validation
```

Each price request is clipped to the observed mapping interval. The run permits at most 200 reviewed symbols and two retries per operation. It makes one Tiingo EOD metadata request and one price request per eligible symbol, so the upper bound is **1,200 API requests** (400 first attempts plus 800 possible retries); mismatched or unknown metadata stops before prices. At one second between symbols, intentional pacing adds about 200 seconds; server time and retry backoff add more. Twenty years of daily prices has a rough upper bound near 1 million rows for 200 symbols (roughly 5,000 sessions each), but interval clipping should reduce it. Actual usage and data volume depend on the reviewed manifest and vendor coverage. The pilot database is separate from the 30 GB SEC database. Repeating the same completed request skips the API. Changing the requested window creates a new request and upserts overlapping dates by `(security_id, provider, trading_date)`.

The documented Tiingo EOD endpoint returns the requested date window as one JSON list; no pagination contract is documented. The adapter checks required fields and duplicate dates. A response whose first/last date does not reach the metadata-overlapping interval (seven-calendar-day tolerance for nontrading days) is recorded `PARTIAL`; internal gaps are flagged by validation. `UNKNOWN_SYMBOL` (404) and `IDENTITY_REVIEW` (metadata name/ticker mismatch) stop before a price request, store no prices for that symbol, and fail validation. Other request or parse errors are retried twice and then recorded `FAILED`; an empty valid response is `NO_DATA`. Since metadata is a current vendor snapshot, this is a guard, not proof of point-in-time identity.

Pilot data stores raw OHLCV separately from Tiingo `adjOpen`, `adjHigh`, `adjLow`, `adjClose`, and `adjVolume`, plus `splitFactor` and `divCash` where returned. The `adjustment_basis` field records that Tiingo's adjusted series includes split and dividend adjustments. Raw OHLC must be checked against raw OHLC only; adjusted fields must be compared within their own basis. Split factor and dividend cash are observations, not a complete merger/delisting return model. The older `market_prices_daily` table has only `(symbol, date, provider)` identity and is unsuitable for this pilot's historical identity checks. No production migration is performed.

The pilot validator writes `pilot_validation_summary.csv` (PASS/WARN/FAIL) and `pilot_security_diagnostics.csv`. It checks duplicates, raw OHLC, prices, volume, identity, adjustment basis, corporate-action values, provider outcomes, long calendar gaps, large adjusted-price jumps, rough date coverage, and available series length for 5/20/60/120-session horizons. Calendar-gap and coverage checks are screening heuristics; holidays, halts, IPOs, ticker changes, and delistings need human review. A complete request status means the response passed the boundary checks and was persisted; it does not certify full trading-calendar coverage. Readiness by series length does not prove that a particular filing has the full forward horizon.

For event-study returns, use SEC filing **acceptance timestamp**, not transaction date, to determine when information became public. An after-close or non-trading-day acceptance enters no earlier than the next eligible trading session; an intraday acceptance needs a conservative executable entry rule. Then measure 5, 20, 60, and 120 subsequent trading sessions on the same adjusted-price basis, using a point-in-time benchmark for excess returns. Preserve missing forward windows and terminal delisting outcomes rather than dropping them. Verify exchange calendars and provider correction timing. This event-time join and return calculation are future work.

Full-universe ingestion remains **NO-GO** until identity review, vendor entitlement/cost approval, pilot QA with no unresolved FAIL conditions, coverage stratified by era and known delistings, documented corporate-action reconciliation, and a defensible treatment of ticker changes, mergers, and missing terminal returns. The pilot itself is ready for manual manifest review and explicit approval before the first external API call.

## Tests

```bash
pip install pytest
pytest tests/ -q
```

## Legacy loader

`src/ingestion/load_sec_raw.py` is the earlier single-pass loader. Prefer `load_all_sec_datasets.py` for Phase 1.1 (chunked reads, `row_hash` dedup, full summary).

## Design notes

- No transaction-code cleaning, Form 4 filtering, or value calculations at ingest time.
- Chunked `pandas.read_csv(..., chunksize=...)` limits memory use on large quarters.
- UTF-8 input with replacement for invalid bytes; bad lines are warned, not fatal.
