# Phase 5.2B controlled sequential EDGAR recovery

The sequential runner completed Batches 003–005, each with 100/100
`VERIFIED_RAW` responses and 100 recorded HTTP attempts. Together with the
pilot and Batches 001–002, the offline starting state for this expanded run
is 519/6,067 verified; 5,547 remain in the ordinary queue.
The exhausted pilot accession `0001305763-06-000002` remains in its separate
unresolved, owner-controlled retry queue.

`src.market_data.edgar_sequential preflight` is offline. It validates all
existing evidence and pinned manifest hashes, checks that Batch 002 is fully
verified, and writes `data/exports/market_data/edgar_sequential_progress.json`.
The checkpoint records the plan hash, current and next batch, verified count,
completed batch numbers, cumulative HTTP attempts, normal queue count, and
controlled retry count. The offline coverage reports
remain available through `src.market_data.edgar_remaining status`.

The `run` command is live and requires separate authorization. It holds an OS
lock so two sequential runners cannot operate concurrently. It requires a
three-digit start and end batch, a maximum of one to ten incomplete batches
per invocation, and a contact-bearing SEC User-Agent. Completed batches in the
requested range are skipped and do not consume the maximum. Earlier batches
must be complete before the requested start. Every chosen manifest is
SHA-256-checked before use. Recovery uses the existing isolated per-batch
SQLite databases, exact accession identity checks, append-only raw evidence,
durable pre-request attempt reservations, at most three attempts per filing,
300 lifetime attempts per batch, conservative pacing, bounded retries, and
`Retry-After` handling. The sequential runner stops after the first HTTP 429
instead of retrying into an access restriction; its recorded attempt and
response status remain available for manual review. Production SQLite is
never opened for writes.

Do not launch the legacy single-batch recovery command in parallel with this
runner; the runner lock coordinates runner processes, not unrelated manual
processes.

After each batch, the runner revalidates SQLite integrity, raw-response hashes,
source URLs, accession, issuer and owner CIKs, form type, filing date, raw
acceptance value, and the batch's expected 100/100 (or final 47/47) coverage.
It also reconciles global 6,067-accession and 33,986-transaction coverage.
Identity conflicts, evidence corruption, terminal HTTP/access-policy states,
exhausted attempts, or repeated transport failures stop the sequence before
another batch. An interrupted request retains its attempt reservation; the
runner writes `INTERRUPTED` or `STOPPED` to the global checkpoint and can
resume without repeating verified filings. Batch summaries print to the
console after every validated checkpoint.

The command below is for a separately authorized limited run of Batches
006–015, at most 1,000 planned URLs and 3,000 cumulative attempts across those
ten batch databases. It has **not** been executed:

```powershell
Set-Location 'D:\projects\INSIDER ALPHA LAB\insider-research'
.\.venv\Scripts\python.exe -m src.market_data.edgar_sequential run `
  --start-batch 006 `
  --end-batch 015 `
  --max-batches 10 `
  --user-agent 'InsiderAlphaLab Research artkhomytskyi@gmail.com'
```

Batch 002 took about four minutes from its first to last recorded attempt.
Ten similar successful batches would take roughly 40 minutes of request
execution, plus evidence validation and local I/O. The pacing floor is about
21 minutes for 1,000 filings. A longer run increases the exposure to SEC
throttling, transient transport failure, interruption, and local disk issues;
retry or 20-second timeouts can extend runtime considerably. The runner stops
rather than bypassing a policy or evidence failure. This command does not
authorize Batches 016–061, the controlled retry accession, or return/alpha
calculations.
