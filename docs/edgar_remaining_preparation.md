# Phase 5.2B remaining accession recovery

Preparation only. No additional SEC requests are authorized by this document.

## Pinned inputs and accession accounting

The full manifest contains 6,067 distinct accession numbers and 33,986 locally
mapped transactions. It is pinned to SHA-256
`c645ea536de65448825bd210ab8720c412611e6434caf999eecc682db3d8c164`.
The 20-filing pilot and 100-filing Batch 001 manifests retain their earlier
preflight hashes. The recovery planner reads these files and isolated evidence
databases only; it never opens the production SQLite database.

All 119 `VERIFIED_RAW` filings from the pilot (19) and Batch 001 (100) passed
read-only SQLite integrity, original-response SHA-256, exact accession,
issuer/owner CIK, form type, filing date, source-URL, and raw acceptance-value
checks. None is put in a new request batch. The pilot accession
`0001305763-06-000002` remains unresolved after three transport attempts. It
appears only in `edgar_exhausted_pilot_controlled_retry.csv`, with
`SEPARATE_OWNER_AUTHORIZATION_REQUIRED` in the plan. It is not in the normal
batch sequence and has not been marked recovered.

The remaining 5,947 normal accessions are copied unchanged from the pinned
full manifest to `edgar_remaining_standard_manifest.csv`. Sixty immutable,
SHA-256-pinned manifests partition that queue: `edgar_batch_002_100_manifest.csv`
through `edgar_batch_061_047_manifest.csv` (59 batches of 100 and one of 47).
The plan and hashes are in `edgar_remaining_plan.json`. Accessions are unique
across all batches and are matched by exact accession, historical security ID,
mapping ID, issuer CIK, and approved transaction-date interval. A filename,
ticker, or issuer CIK alone is never a match key. The original full manifest
has four filings after the market-price archive's 2017-11-10 cutoff; timestamp
recovery does not make those filings price-covered.

The consolidated `edgar_remaining_accession_status.csv` has one row per
accession. `edgar_remaining_coverage.csv` groups accessions and locally mapped
transactions by security, filing year, form type, and recovery status. Filing
year is the year of the recorded filing date, not the insider transaction
year. Both reports can be refreshed offline after any batch.

## Resume and safety behavior

Use `src.market_data.edgar_remaining recover` for one numbered batch only.
Before opening HTTP, it rechecks the pinned full and batch manifests, every
existing isolated evidence database, raw hashes, and filing identities. An
accession already verified in another database fails preflight; a verified
filing in the same batch is integrity-checked and skipped on restart. Failed,
unresolved, or conflicting evidence stays visible and is not silently repaired.

Each batch uses its own isolated `data/processed/edgar_batch_NNN.db`, bound to
the exact manifest SHA-256. An HTTP attempt is reserved and committed before
the request. The cumulative lifetime limit is 300 attempts per batch and three
per accession, including interrupted runs. Evidence is append-only and retains
the raw SEC response, URL, hash, retrieval time, raw acceptance value, and
verification status. `parsed_utc` remains null and timezone status remains
`UNVERIFIED`; the raw header clock is not treated as a verified UTC instant or
as proof of first public dissemination.

Requests use official `www.sec.gov` archive HTTPS URLs only, a contact-bearing
User-Agent, 20-second timeout, no redirects, a 10 MB response cap, 1.25-second
inter-filing pacing (including across batches), bounded retry backoff, and
`Retry-After` handling. HTTP 401/403/429, identity conflicts, and corrupt
prior evidence halt or remain unresolved according to the existing fail-closed
recovery rules. Every completed filing emits progress with cumulative attempts;
attempt reservations survive interruption. Per-batch validation and global
coverage reports are refreshed on exit, including an interrupted run.

The next batch plans 100 GET URLs. Normal success uses 100 HTTP attempts;
the hard cumulative ceiling across all resumes of Batch 002 is 300. Its
1.25-second pacing floor is about two minutes, plus network latency. Across
the 5,947 normal queued filings, the pacing floor is about 2 hours 3 minutes
over 60 batches, before network latency and retries. Actual elapsed time can
be much longer; the theoretical retry ceiling is 17,841 attempts. This plan
does not authorize unattended execution of 60 batches.

## Exact Windows PowerShell commands

Run the offline status check at any time:

```powershell
Set-Location 'D:\projects\INSIDER ALPHA LAB\insider-research'
.\.venv\Scripts\python.exe -m src.market_data.edgar_remaining status
```

Only after separate authorization for Batch 002, run the following from a
Windows environment with working SEC DNS/HTTPS:

```powershell
Set-Location 'D:\projects\INSIDER ALPHA LAB\insider-research'
.\.venv\Scripts\python.exe -m src.market_data.edgar_remaining recover `
  --batch 002 `
  --user-agent 'InsiderAlphaLab Research artkhomytskyi@gmail.com'
```

Do not run the controlled retry queue with this command. It requires a
separate review of its earlier transport failure, a fresh explicit attempt
budget, and separate owner approval. No price or return calculations belong
to this recovery workflow.
