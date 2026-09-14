# Insider Research

Research platform for SEC insider trading filings (Forms 3, 4, and 5). The project ingests official SEC quarterly datasets, stores an immutable raw layer in SQLite, and will later support cleaning, normalization, enrichment, analytics, and scoring.

## Current phase: Phase 4 — Entity Normalization

Phase 1.1 ingests raw SEC data. Phase 1.2 audits file headers. Phase 2 profiles the SQLite database and builds taxonomies. **Phase 3** creates `clean_insider_transactions` — a normalized, enriched table with quality flags and an MVP alpha universe marker.

Market returns, alpha, insider scores, and market-data enrichment are **not** implemented yet.

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
