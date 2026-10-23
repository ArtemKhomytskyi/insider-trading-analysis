# Final EDGAR ingestion integrity audit

## Scope and result

This audit covers the pinned three-security cohort in
`data/exports/market_data/edgar_full_6067_manifest.csv`. It is an offline audit:
no SEC request is made and no raw evidence is changed.

- Expected unique accessions: **6,067**
- `VERIFIED_RAW`: **6,067**
- Controlled retry remaining: **0**
- Cohort: CRM 5,022; NFLX 1,009; TSLA 36
- Evidence databases: 63
- Recorded HTTP attempts: 6,088
- Orphan evidence: 0
- Contradictory/cross-database evidence: 0
- Batch-plan SHA-256: `87b0692201bed73890019143292543dde3e35cfd679ced5320de1a3fca0b1a17`

The immutable sequential checkpoint records the original 6,066 verified normal
ingestions plus one controlled-retry queue item. The final audit reconciles it
with the separately authorized one-attempt controlled delta, producing 6,067
verified filings without rewriting the checkpoint or pinned batch plan.

## Checks

For every verified accession, the audit confirms:

1. membership in the unique pinned manifest and approved NFLX/CRM/TSLA cohort;
2. one coherent terminal evidence location;
3. an immutable local `evidence.raw_response` SQLite BLOB;
4. SHA-256 recomputation over that exact BLOB;
5. exact accession, form, filing date, issuer CIK, and every expected reporting-owner CIK in the SEC header;
6. exact canonical SEC archive URL;
7. exactly one parseable `<ACCEPTANCE-DATETIME>` value;
8. absence of orphan records, silent cross-database duplicates, and evidence appended after verification;
9. consistency with the sequential checkpoint and total attempt count.

Raw filings are deliberately retained inside append-only SQLite evidence
databases rather than copied into a second file tree. “Local raw document” in
the audit therefore means a non-null raw BLOB whose recorded SHA-256 has been
recomputed successfully.

## Controlled retry evidence

`0001305763-06-000002` was not corrupted or identity-conflicted. Its pilot
record is `FAILED`, detail `transport_URLError`, with no HTTP status and three
consumed attempts. The first two legacy attempt details were not captured by
the older implementation; the third is recorded as `URLError`. There is no raw
response and therefore no defensible header, hash, or timestamp to verify at
that historical stage.

The authorized retry subsequently returned HTTP 200 and `VERIFIED_RAW` in one
attempt. Its canonical URL, raw BLOB SHA-256, accession, form, filing date,
issuer CIK, reporting-owner CIK, and acceptance value all pass the same normal
verification rules. The historical failure remains append-only evidence; the
verified retry is the effective final status. This exact two-record chain is
recognized only for the pinned retry accession and is not a contradiction.

The normal batch and sequential runners intentionally exclude this accession.
The dedicated command below uses the pinned one-row retry manifest, permits
only this accession, requires a contact email in the SEC User-Agent, stops on
policy responses, and has exactly one fresh attempt in a separate append-only
evidence database. Re-running it cannot create a second request.

The command used for the completed, separately authorized attempt was:

```powershell
Set-Location 'D:\projects\INSIDER ALPHA LAB\insider-research'; .\.venv\Scripts\python.exe -m src.market_data.edgar_remaining controlled-retry --accession 0001305763-06-000002 --user-agent $env:SEC_USER_AGENT
```

`SEC_USER_AGENT` must identify the research organization and contain a real
contact email. Do not put credentials in source control. The command cannot
consume another attempt. Regenerate the offline outputs with:

```powershell
Set-Location 'D:\projects\INSIDER ALPHA LAB\insider-research'; .\.venv\Scripts\python.exe -m src.market_data.edgar_final_audit
```

## Event-study metadata

`data/exports/market_data/edgar_filing_event_metadata.csv` contains one row per
pinned accession with this schema:

| Column | Meaning |
|---|---|
| `accession_number` | Pinned SEC accession |
| `issuer_cik` | Zero-padded expected issuer CIK |
| `reporting_owner_cik` | One or more zero-padded CIKs separated by `|` |
| `form_type` | Verified filing form |
| `filing_date` | SEC filing date (`YYYY-MM-DD`) |
| `acceptance_datetime_raw` | Exact 14-digit SEC header value, if verified |
| `acceptance_datetime_utc` | DST-aware conversion to UTC |
| `acceptance_datetime_new_york` | Zoned America/New_York representation |
| `source_url` | Canonical SEC archive URL |
| `raw_sha256` | SHA-256 of the stored raw response |
| `verification_status` | Effective final verification status |

The conversion interprets the raw acceptance wall clock in the IANA
`America/New_York` zone and applies historical EST/EDT rules. Ambiguous or
nonexistent DST wall times are left blank and reported rather than guessed.
All 6,067 verified timestamps converted without ambiguity.

**Research limitation:** EDGAR `ACCEPTANCE-DATETIME` is an administrative SEC
timestamp. This audit does not prove that it is the instant of public
dissemination or market availability. Event-study code must preserve that
distinction and define its timing convention in advance.

## Reproduction

```powershell
Set-Location 'D:\projects\INSIDER ALPHA LAB\insider-research'; .\.venv\Scripts\python.exe -m src.market_data.edgar_final_audit
```

The command rewrites only derived summary/discrepancy/metadata exports via
temporary files. It opens evidence databases read-only and is idempotent with
respect to their contents. `generated_at_utc` is run metadata and will change.

The EDGAR raw-ingestion coverage blocker for Phase 6 is closed at 6,067/6,067.
The acceptance-time dissemination limitation still applies. No trading
signals, returns, or benchmark-adjusted results are created by this audit.
