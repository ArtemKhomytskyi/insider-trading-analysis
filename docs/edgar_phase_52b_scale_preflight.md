# Phase 5.2B EDGAR recovery: offline scale preflight

No additional EDGAR filing requests were made in this preparation. The first 100-file batch requires separate owner authorization.

## Pilot evidence validation

The isolated 20-filing pilot contains 19 `VERIFIED_RAW` records and one earlier transport failure (`SKIPPED_EXHAUSTED`). All 19 raw responses pass SHA-256 integrity, exact accession, issuer CIK, all locally recorded reporting-owner CIKs, Form type, filing date, source URL, and raw acceptance-value checks. There are no conflicts or missing required metadata among the 19. The SQLite integrity check is `ok`; cumulative pilot attempts are 22.

Raw acceptance values span `20060106170353` through `20170907194623`. Their clock-hour distribution is 11: 1; 16: 1; 17: 3; 18: 3; 19: 2; 20: 6; 21: 3. Eighteen of 19 are after 16:00 on the raw clock. All 19 raw acceptance dates match the recorded filing dates. Filing years: 2006: 4, 2007: 1, 2008: 2, 2009: 1, 2010: 1, 2012: 2, 2017: 8. The machine-readable per-accession report is `data/exports/market_data/edgar_timestamp_20_post_validation.json`.

The [SEC webmaster FAQ](https://www.sec.gov/files/about/webmaster-faq.htm) describes acceptance time as the time ("EST") when EDGAR accepted the submission, distinguishes it from `FILED AS OF DATE`, and says no timestamp identifies when filing content first became available on sec.gov. The [SEC filing-status guide](https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/determine-status-my-filing) uses Eastern Time for EDGAR hours and identifies Form 4/4/A as same-day filing-date exceptions after 5:30 p.m. The [SEC EDGAR data guide](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data) notes that some filings may appear in the next business day's index. Interpreting the raw header clock with `America/New_York` yields 15 UTC−4 and four UTC−5 dates in this sample, but this derived UTC remains an **inference** and is not written as verified evidence. Acceptance is not proof of first public dissemination.

## Full and first-batch manifests

The full manifest contains 6,067 distinct accessions tied to 33,986 transactions by exact approved historical security mapping IDs and transaction-date intervals: CRM 5,022; NFLX 1,009; TSLA 36. It includes two Form 5 filings and four filings whose filing date is after the 2017-11-10 market archive cutoff despite in-interval transaction dates. All local owner and raw submission checks passed. SHA-256: `c645ea536de65448825bd210ab8720c412611e6434caf999eecc682db3d8c164`.

The first batch has 100 distinct Form 4 filings: CRM 50, NFLX 30, TSLA 20. It excludes all 20 pilot accessions and all post-cutoff filing dates. SHA-256: `92e3e268f9ed67f85b32a0eb2b71a8445642a74fe204aca0a1f470a6083fa57c`. Archive-path CIKs remain **candidates** from recorded reporting owners; a 404 stays unresolved and causes no alternate-path search.

Batch recovery uses a separate SQLite database with a pinned manifest hash, a 300-attempt lifetime budget, at most three attempts per accession, atomic pre-request attempt reservations, per-attempt diagnostics, append-only raw evidence, minimum 1.25-second inter-filing pacing, bounded retry backoff, `Retry-After`, and fail-closed identity checks. Restart skips verified or terminally unresolved filings and verifies prior evidence integrity before skipping. No production table is written.

Planned first-batch requests: 100 distinct URLs, normally 100 GET attempts; hard ceiling 300 attempts across all runs. The pacing floor alone is about 124 seconds for 100 successful filings, before network latency. A practical successful run may take several minutes; repeated retries and 20-second timeouts could extend it toward roughly two hours. Scaling all 6,067 filings is **not authorized** by this batch and would require separate budgets and quality review.

## Proposed Windows PowerShell command — do not run before authorization

```powershell
cd 'D:\INSIDER ALPHA LAB\insider-research'
.\.venv\Scripts\python.exe -m src.market_data.edgar_batch recover `
  --manifest 'data/exports/market_data/edgar_batch_001_100_manifest.csv' `
  --pilot-manifest 'data/exports/market_data/edgar_timestamp_20_manifest.csv' `
  --batch-db 'data/processed/edgar_batch_001.db' `
  --expected-sha256 '92e3e268f9ed67f85b32a0eb2b71a8445642a74fe204aca0a1f470a6083fa57c' `
  --user-agent 'InsiderAlphaLab Research artkhomytskyi@gmail.com' `
  --report 'data/exports/market_data/edgar_batch_001_validation.csv'
```

Recommendation: **conditional GO** for a separately authorized 100-filing recovery batch from a Windows environment with working SEC DNS/HTTPS. **NO-GO** for automatic 6,067-filing scale-up or event-time return calculations until batch quality, archive paths, timezone interpretation, and disclosure-versus-dissemination timing are reviewed.
