# Phase 5.2A local archive inspection — 2026-10-16

Read-only inspection of manually supplied `data/raw/archive(1).zip`. No extraction, import, return calculation, or production DB write occurred.

## Archive integrity and layout

| Item | Observation |
|---|---|
| Size | **515,591,518 bytes** (491.7 MiB) |
| SHA-256 | `d9317c8fb2d63b9b00db5f933b6c9639d2bf7ea3b918169bb5cec5903dce85a1` |
| ZIP test | `ZipFile.testzip()` returned `None` (no corrupt member detected) |
| Members | **17,078** `.txt` files; no explicit directory entries; uncompressed total **1,618,046,652 bytes** |
| Layout | `Data/Stocks/` 7,195; `Data/ETFs/` 1,344; mirrored `Stocks/` 7,195 and `ETFs/` 1,344 |
| Mirror check | 8,539 `Data/...`/top-level pairs have matching ZIP CRC and uncompressed size; sampled `NFLX` pair is byte-identical. CRC/size is not a cryptographic proof for every pair. |
| Headers | 17,014 nonempty files have exactly `Date,Open,High,Low,Close,Volume,OpenInt`; 64 members (32 mirror pairs) are zero-byte files, with no header or data. |

Files such as `Data/Stocks/nflx.us.txt` are comma-delimited despite the `.txt` suffix. The archive includes stock and ETF directories but no corporate-action directory or separate action files. The revised offline importer records hashes for all members, ingests only canonical `Data/Stocks/*.us.txt` for selected tickers, and ignores its top-level mirror and ETFs for price matching. No auto-download or extraction was added.

## Sampled rows and fields

Sampled `NFLX`, `CRM`, `TSLA`, `PCLN`, `AAPL`, and ETF `SPY` directly from the ZIP. Dates are `YYYY-MM-DD`; OHLC are decimal numeric text, `Volume` and `OpenInt` are integer text. Across `NFLX`, `CRM`, `TSLA`, `PCLN`, and `AAPL`, none of the seven fields was blank. In the four pilot-relevant stock files checked for row validity (`NFLX`, `CRM`, `TSLA`, `PCLN`), there were no duplicate dates, weekend dates, nonpositive OHLC values or inconsistent OHLC bars. `TSLA` has one zero-volume row, **2010-06-28**; zero volume is not automatically an error, but needs review if used. `OpenInt` was `0` for every row of those five sampled stocks. These checks are samples, **not** full-archive quality clearance.

There is no `Adj Close`, adjustment factor, dividend, split, security ID, CIK, exchange, or share-class column. The dataset-page statement that prices were adjusted for dividends/splits is an uploader assertion; it does not identify the algorithm, adjustment-as-of date, or distinguish raw from adjusted values in the files. Set `price_basis=UNKNOWN`; **no total-return claim** follows from these fields alone. No delisting consideration or terminal-return ledger was found in the layout.

## Five pilot tickers and SEC overlap

| Five-row pilot ticker | File in archive? | Observed canonical file span | Relevant verified mapping interval through cutoff | Mapped SEC transaction rows in interval* | Disposition |
|---|---:|---|---|---:|---|
| `NFLX` | Yes | 2005-02-25–2017-11-10; 3,201 rows | 2006-01-05–2017-11-10; **2,984** bars | **6,063**, 2006-01-05–2017-11-08 | Candidate only; PENDING |
| `CRM` | Yes | 2005-02-25–2017-11-10; 3,201 rows | 2006-01-01–2017-11-10; **2,986** bars (first 2006-01-03) | **27,776**, 2006-01-03–2017-11-09 | Candidate only; PENDING. 2019 mapping endpoint is **not** a delisting date. |
| `GTM` | No | — | Starts 2025-05-14, after cutoff | Not applicable | Exclude. Earlier `ZI` is also absent here and is a distinct interval requiring separate identity work. |
| `BKNG` | No | — | Starts 2018-03-04, after cutoff | Not applicable | Exclude. `PCLN` file exists through 2017-11-10 (3,201 rows), but is a predecessor ticker, **not** an approved `BKNG` alias. |
| `TSLA` | Yes | 2010-06-28–2017-11-10; 1,858 rows | 2017-02-01–2017-11-10; **198** bars | **147**, 2017-02-01–2017-11-10 | Candidate only; PENDING |

*Read-only counts from `transaction_market_mapping` for those mapping IDs and transaction dates, not a matched-price or event-study cohort. Multiple transactions can share a date; these counts do not establish filing-time entry or complete post-event horizons. The 81 known Phase 5.1 QA issues remain a separate fail-closed quarantine.

## Drafts and next gate

- [Observed schema draft](../../configs/market_data/archive_2017/schema.json)
- [Rights/provenance draft](../../configs/market_data/archive_2017/rights.json)
- [Three-candidate manifest](../../configs/market_data/archive_2017/manifest.csv) — **all PENDING**; no approval was inferred from ZIP filenames.

The importer requires exact observed headers; an explicit Date/OHLCV column map; `price_basis` and methodology evidence if known; source URL, license label, unresolved provenance, and review date; and a manifest with mapping ID, CIK, ticker, explicit share class, dated interval, independent evidence, and **manual `APPROVED` status**. The production mapping rows have null `security_class`, so the draft explicitly says `COMMON` based on cited SEC evidence; the importer cannot itself authenticate that evidence. Its fail-closed approval gate prevents this draft from being imported unchanged.

**Next step:** owner reviews the three identities, class evidence, archive provenance risk, and use restrictions. Only if approved, create a separate approved copy of the manifest, run the import command in [the pilot guide](phase_5_2a_offline_archive_pilot.md) against this local ZIP and a **new** `data/processed/archive_pilot_2017.db`, then inspect findings/security-year/cohort coverage before designing any event returns. The price-basis `UNKNOWN` gate remains; approval of identities is not approval of total-return semantics.

`.gitignore` now covers `data/raw/*.zip`; `git check-ignore -v` confirms this archive is ignored and `git status` does not list it. A deliberate `git add -f` could still override Git ignore, so the file should never be force-added.
