# Phase 5.2A — offline archive pilot

No archive was supplied or downloaded for this implementation. The importer does **not** claim the uploader's CC0 label establishes upstream redistribution rights. No live import, return, or alpha calculation has been run. The frozen candidate ends **2017-11-10**; this code excludes later bars.

## Manual workflow (PowerShell, from repository root)

Keep the original ZIP or extracted directory unchanged and outside the production database path. First inspect the **actual** CSV headers and hashes:

```powershell
.venv\Scripts\python.exe -m src.market_data.archive_pilot inspect --source "D:\local\manual\archive.zip"
```

After reading that output, manually create three local files:

1. `schema.json`: `{"observed_headers":[["Date","Open","High","Low","Close","Volume","OpenInt"]],"columns":{"date":"Date","open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"},"price_basis":"UNKNOWN","methodology_evidence":""}`. Change the header and map to **observed** values, not the example. `RAW` or `ADJUSTED` requires documented methodology evidence; neither is inferred from the uploader's description. No action or adjusted-close fields are assumed.
2. `rights.json`: `{"source_url":"https://www.kaggle.com/datasets/borismarjanovic/price-volume-data-for-all-us-stocks-etfs","license_label":"CC0 (uploader claim)","provenance_limitations":"Upstream market-data source/rights unverified","reviewed_at":"YYYY-MM-DD"}`. This records the limitation; it does not waive it.
3. `manifest.csv` with `security_id,issuer_cik,ticker,mapping_id,security_class,valid_from,valid_to,identity_status,identity_evidence`. Use only historically evidenced, manually `APPROVED` rows whose IDs/classes/intervals match the read-only production mapping. Existing `PENDING` Yahoo manifests **cannot** be imported as approved identities. Do not infer approval from matching filenames.

The exact local-only import command, once those files and an archive exist, is:

```powershell
.venv\Scripts\python.exe -m src.market_data.archive_pilot import --source "D:\local\manual\archive.zip" --schema "D:\local\manual\schema.json" --manifest "D:\local\manual\manifest.csv" --rights-record "D:\local\manual\rights.json" --source-db "data\processed\insider_research.db" --pilot-db "data\processed\archive_pilot_2017.db"
```

The production DB opens with SQLite `mode=ro&immutable=1`; only a **new**, separate pilot DB is created. The importer refuses to overwrite an existing pilot DB. The pilot DB contains source-member SHA-256 hashes, archive hash for ZIP input, provenance/rights/schema records, matched bars with original source numeric strings and `adjustment_semantics`, findings, all **81** Phase 5.1 QA transaction IDs in `qa_quarantine`, and a by-security/year/transaction-code cohort table. A changed QA count fails closed. Original source files are never extracted, altered, or copied by the importer. Back up the source independently if long-term preservation is needed.

## Validation and interpretation

- Confirm observed headers and explicit one-to-one Date/OHLCV map before import. Unsupported ZIP/extracted contents, missing inputs, unsupported/unapproved mappings, ticker reuse/share-class conflict, and mapping-interval extension fail.
- Bars outside the mapping interval never join a security; their `UNRESOLVED_IDENTITY` finding remains visible. Later-than-cutoff bars are ignored. Negative/zero prices, negative volume, weekend dates, and inconsistent OHLC are rejected; duplicates are recorded. Gaps over seven calendar days and close-to-close jumps of at least 50% are flagged for review (heuristics, **not** proof of missing days or actions).
- `price_basis=UNKNOWN` bars are unsuitable for adjusted or total-return claims. Even if `ADJUSTED` is supported later, this archive has no separately established split/dividend event ledger, raw OHLCV, delisting proceeds, or verified adjustment factors. Review every suspicious discontinuity and material action against independent records before event returns.
- `cohort` counts SEC transactions by mapped security, transaction year and code; `price_window_covered` currently means **a bar exists on the transaction date**, not that a filing-time entry or full outcome horizon is covered. This is coverage screening only, not an event-study readiness flag. Event entry must be based on public filing acceptance timing in a later stage.

**GO for an exploratory, restricted-universe event study only after**: owner accepts unresolved provenance risk; source/archive and schema hashes are frozen; all included identities are independently approved by dated interval/share class; all QA findings are excluded; missingness and exclusions are reported by security/year/cohort; price basis and corporate actions are reconciled for each event window; terminal/delisted outcomes are evidenced or explicitly censored; study window/horizons end no later than 2017-11-10.

**NO-GO** if upstream-use rights are unacceptable, any identity is ticker-only or ambiguous, QA count drifts, raw-versus-adjusted treatment remains unknown for a proposed total-return estimate, action/delisting outcomes are silently missing, or results are generalized to the 2006–2026 US market. The archive's selection/survivorship bias and source-level adjustment uncertainty must appear prominently in any eventual findings.
