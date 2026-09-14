-- Raw SEC Form 3/4/5 tables (immutable ingestion layer).
-- All business columns stored as TEXT to avoid transformation at load time.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS ingestion_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file     TEXT NOT NULL,
    target_table    TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    source_year     INTEGER,
    source_quarter  INTEGER,
    ingested_at     TEXT NOT NULL,
    UNIQUE (source_file, target_table)
);

CREATE TABLE IF NOT EXISTS raw_submission (
    ACCESSION_NUMBER          TEXT,
    FILING_DATE               TEXT,
    PERIOD_OF_REPORT          TEXT,
    DATE_OF_ORIG_SUB          TEXT,
    NO_SECURITIES_OWNED       TEXT,
    NOT_SUBJECT_SEC16         TEXT,
    FORM3_HOLDINGS_REPORTED   TEXT,
    FORM4_TRANS_REPORTED      TEXT,
    DOCUMENT_TYPE             TEXT,
    ISSUERCIK                 TEXT,
    ISSUERNAME                TEXT,
    ISSUERTRADINGSYMBOL       TEXT,
    REMARKS                   TEXT,
    AFF10B5ONE                TEXT,
    source_file               TEXT NOT NULL,
    source_year               INTEGER,
    source_quarter            INTEGER,
    source_folder             TEXT NOT NULL,
    ingested_at               TEXT NOT NULL,
    row_hash                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_reporting_owner (
    ACCESSION_NUMBER          TEXT,
    RPTOWNERCIK               TEXT,
    RPTOWNERNAME              TEXT,
    RPTOWNER_RELATIONSHIP     TEXT,
    RPTOWNER_TITLE            TEXT,
    RPTOWNER_TXT              TEXT,
    RPTOWNER_STREET1          TEXT,
    RPTOWNER_STREET2          TEXT,
    RPTOWNER_CITY             TEXT,
    RPTOWNER_STATE            TEXT,
    RPTOWNER_ZIPCODE          TEXT,
    RPTOWNER_STATE_DESC       TEXT,
    FILE_NUMBER               TEXT,
    source_file               TEXT NOT NULL,
    source_year               INTEGER,
    source_quarter            INTEGER,
    source_folder             TEXT NOT NULL,
    ingested_at               TEXT NOT NULL,
    row_hash                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_nonderiv_trans (
    ACCESSION_NUMBER              TEXT,
    NONDERIV_TRANS_SK             TEXT,
    SECURITY_TITLE                TEXT,
    SECURITY_TITLE_FN             TEXT,
    TRANS_DATE                    TEXT,
    TRANS_DATE_FN                 TEXT,
    DEEMED_EXECUTION_DATE         TEXT,
    DEEMED_EXECUTION_DATE_FN      TEXT,
    TRANS_FORM_TYPE               TEXT,
    TRANS_CODE                    TEXT,
    EQUITY_SWAP_INVOLVED          TEXT,
    EQUITY_SWAP_TRANS_CD_FN       TEXT,
    TRANS_TIMELINESS              TEXT,
    TRANS_TIMELINESS_FN           TEXT,
    TRANS_SHARES                  TEXT,
    TRANS_SHARES_FN               TEXT,
    TRANS_PRICEPERSHARE           TEXT,
    TRANS_PRICEPERSHARE_FN        TEXT,
    TRANS_ACQUIRED_DISP_CD        TEXT,
    TRANS_ACQUIRED_DISP_CD_FN     TEXT,
    SHRS_OWND_FOLWNG_TRANS        TEXT,
    SHRS_OWND_FOLWNG_TRANS_FN     TEXT,
    VALU_OWND_FOLWNG_TRANS        TEXT,
    VALU_OWND_FOLWNG_TRANS_FN     TEXT,
    DIRECT_INDIRECT_OWNERSHIP     TEXT,
    DIRECT_INDIRECT_OWNERSHIP_FN  TEXT,
    NATURE_OF_OWNERSHIP           TEXT,
    NATURE_OF_OWNERSHIP_FN        TEXT,
    source_file                   TEXT NOT NULL,
    source_year                   INTEGER,
    source_quarter                INTEGER,
    source_folder                 TEXT NOT NULL,
    ingested_at                   TEXT NOT NULL,
    row_hash                      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_nonderiv_holding (
    ACCESSION_NUMBER              TEXT,
    NONDERIV_HOLDING_SK           TEXT,
    SECURITY_TITLE                TEXT,
    SECURITY_TITLE_FN             TEXT,
    TRANS_FORM_TYPE               TEXT,
    TRANS_FORM_TYPE_FN            TEXT,
    SHRS_OWND_FOLWNG_TRANS        TEXT,
    SHRS_OWND_FOLWNG_TRANS_FN     TEXT,
    VALU_OWND_FOLWNG_TRANS        TEXT,
    VALU_OWND_FOLWNG_TRANS_FN     TEXT,
    DIRECT_INDIRECT_OWNERSHIP     TEXT,
    DIRECT_INDIRECT_OWNERSHIP_FN  TEXT,
    NATURE_OF_OWNERSHIP           TEXT,
    NATURE_OF_OWNERSHIP_FN        TEXT,
    source_file                   TEXT NOT NULL,
    source_year                   INTEGER,
    source_quarter                INTEGER,
    source_folder                 TEXT NOT NULL,
    ingested_at                   TEXT NOT NULL,
    row_hash                      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_deriv_trans (
    ACCESSION_NUMBER              TEXT,
    DERIV_TRANS_SK                TEXT,
    SECURITY_TITLE                TEXT,
    SECURITY_TITLE_FN             TEXT,
    CONV_EXERCISE_PRICE           TEXT,
    CONV_EXERCISE_PRICE_FN        TEXT,
    TRANS_DATE                    TEXT,
    TRANS_DATE_FN                 TEXT,
    DEEMED_EXECUTION_DATE         TEXT,
    DEEMED_EXECUTION_DATE_FN      TEXT,
    TRANS_FORM_TYPE               TEXT,
    TRANS_CODE                    TEXT,
    EQUITY_SWAP_INVOLVED          TEXT,
    EQUITY_SWAP_TRANS_CD_FN       TEXT,
    TRANS_TIMELINESS              TEXT,
    TRANS_TIMELINESS_FN           TEXT,
    TRANS_SHARES                  TEXT,
    TRANS_SHARES_FN               TEXT,
    TRANS_TOTAL_VALUE             TEXT,
    TRANS_TOTAL_VALUE_FN          TEXT,
    TRANS_PRICEPERSHARE           TEXT,
    TRANS_PRICEPERSHARE_FN        TEXT,
    TRANS_ACQUIRED_DISP_CD        TEXT,
    TRANS_ACQUIRED_DISP_CD_FN     TEXT,
    EXCERCISE_DATE                TEXT,
    EXCERCISE_DATE_FN             TEXT,
    EXPIRATION_DATE               TEXT,
    EXPIRATION_DATE_FN            TEXT,
    UNDLYNG_SEC_TITLE             TEXT,
    UNDLYNG_SEC_TITLE_FN          TEXT,
    UNDLYNG_SEC_SHARES            TEXT,
    UNDLYNG_SEC_SHARES_FN         TEXT,
    UNDLYNG_SEC_VALUE             TEXT,
    UNDLYNG_SEC_VALUE_FN          TEXT,
    SHRS_OWND_FOLWNG_TRANS        TEXT,
    SHRS_OWND_FOLWNG_TRANS_FN     TEXT,
    VALU_OWND_FOLWNG_TRANS        TEXT,
    VALU_OWND_FOLWNG_TRANS_FN     TEXT,
    DIRECT_INDIRECT_OWNERSHIP     TEXT,
    DIRECT_INDIRECT_OWNERSHIP_FN  TEXT,
    NATURE_OF_OWNERSHIP           TEXT,
    NATURE_OF_OWNERSHIP_FN        TEXT,
    source_file                   TEXT NOT NULL,
    source_year                   INTEGER,
    source_quarter                INTEGER,
    source_folder                 TEXT NOT NULL,
    ingested_at                   TEXT NOT NULL,
    row_hash                      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_deriv_holding (
    ACCESSION_NUMBER              TEXT,
    DERIV_HOLDING_SK              TEXT,
    SECURITY_TITLE                TEXT,
    SECURITY_TITLE_FN             TEXT,
    CONV_EXERCISE_PRICE           TEXT,
    CONV_EXERCISE_PRICE_FN        TEXT,
    TRANS_FORM_TYPE               TEXT,
    TRANS_FORM_TYPE_FN            TEXT,
    EXERCISE_DATE                 TEXT,
    EXERCISE_DATE_FN              TEXT,
    EXPIRATION_DATE               TEXT,
    EXPIRATION_DATE_FN            TEXT,
    UNDLYNG_SEC_TITLE             TEXT,
    UNDLYNG_SEC_TITLE_FN          TEXT,
    UNDLYNG_SEC_SHARES            TEXT,
    UNDLYNG_SEC_SHARES_FN         TEXT,
    UNDLYNG_SEC_VALUE             TEXT,
    UNDLYNG_SEC_VALUE_FN          TEXT,
    SHRS_OWND_FOLWNG_TRANS        TEXT,
    SHRS_OWND_FOLWNG_TRANS_FN     TEXT,
    VALU_OWND_FOLWNG_TRANS        TEXT,
    VALU_OWND_FOLWNG_TRANS_FN     TEXT,
    DIRECT_INDIRECT_OWNERSHIP     TEXT,
    DIRECT_INDIRECT_OWNERSHIP_FN  TEXT,
    NATURE_OF_OWNERSHIP           TEXT,
    NATURE_OF_OWNERSHIP_FN        TEXT,
    source_file                   TEXT NOT NULL,
    source_year                   INTEGER,
    source_quarter                INTEGER,
    source_folder                 TEXT NOT NULL,
    ingested_at                   TEXT NOT NULL,
    row_hash                      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_footnotes (
    ACCESSION_NUMBER          TEXT,
    FOOTNOTE_ID               TEXT,
    FOOTNOTE_TXT              TEXT,
    source_file               TEXT NOT NULL,
    source_year               INTEGER,
    source_quarter            INTEGER,
    source_folder             TEXT NOT NULL,
    ingested_at               TEXT NOT NULL,
    row_hash                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_owner_signature (
    ACCESSION_NUMBER          TEXT,
    OWNERSIGNATURENAME        TEXT,
    OWNERSIGNATUREDATE        TEXT,
    source_file               TEXT NOT NULL,
    source_year               INTEGER,
    source_quarter            INTEGER,
    source_folder             TEXT NOT NULL,
    ingested_at               TEXT NOT NULL,
    row_hash                  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_submission_row_hash
    ON raw_submission (row_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_reporting_owner_row_hash
    ON raw_reporting_owner (row_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_nonderiv_trans_row_hash
    ON raw_nonderiv_trans (row_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_nonderiv_holding_row_hash
    ON raw_nonderiv_holding (row_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_deriv_trans_row_hash
    ON raw_deriv_trans (row_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_deriv_holding_row_hash
    ON raw_deriv_holding (row_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_footnotes_row_hash
    ON raw_footnotes (row_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_owner_signature_row_hash
    ON raw_owner_signature (row_hash);

CREATE INDEX IF NOT EXISTS idx_raw_submission_accession
    ON raw_submission (ACCESSION_NUMBER);
CREATE INDEX IF NOT EXISTS idx_raw_submission_filing_date
    ON raw_submission (FILING_DATE);
CREATE INDEX IF NOT EXISTS idx_raw_submission_source_folder
    ON raw_submission (source_folder);
CREATE INDEX IF NOT EXISTS idx_raw_nonderiv_trans_accession
    ON raw_nonderiv_trans (ACCESSION_NUMBER);
CREATE INDEX IF NOT EXISTS idx_raw_nonderiv_trans_code
    ON raw_nonderiv_trans (TRANS_CODE);
CREATE INDEX IF NOT EXISTS idx_ingestion_log_source_file
    ON ingestion_log (source_file);
