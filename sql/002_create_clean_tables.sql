-- Phase 3: clean enriched insider transaction layer.
-- Raw tables remain immutable; this is a derived research table.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS clean_insider_transactions (
    transaction_id                  TEXT PRIMARY KEY,
    accession_number                TEXT,
    nonderiv_trans_sk               TEXT,

    document_type                   TEXT,
    is_form_3                       INTEGER,
    is_form_4                       INTEGER,
    is_form_5                       INTEGER,
    is_amendment                    INTEGER,

    issuer_cik                      TEXT,
    issuer_name                     TEXT,
    ticker                          TEXT,
    ticker_raw                      TEXT,
    ticker_clean                    TEXT,
    is_valid_ticker                 INTEGER,
    is_invalid_ticker_placeholder   INTEGER,
    is_multisymbol_ticker           INTEGER,

    insider_cik                     TEXT,
    insider_name                    TEXT,
    insider_name_clean              TEXT,

    raw_relationship                TEXT,
    raw_role_title                  TEXT,
    normalized_role                 TEXT,
    role_group                      TEXT,
    role_weight                     REAL,
    is_executive                    INTEGER,
    is_board_member                 INTEGER,
    is_large_owner                  INTEGER,
    role_requires_review            INTEGER,
    role_confidence                 TEXT,

    transaction_date                TEXT,
    filing_date                     TEXT,
    period_of_report                TEXT,
    filing_lag_days                 INTEGER,
    is_stale_filing                 INTEGER,
    filing_lag_bucket               TEXT,
    transaction_year                INTEGER,
    filing_year                     INTEGER,
    is_pre_source_transaction_date  INTEGER,
    transaction_year_source_gap     INTEGER,

    transaction_code                TEXT,
    transaction_meaning             TEXT,
    transaction_category            TEXT,
    signal_direction                TEXT,
    signal_quality                  TEXT,
    transaction_include_clean_mvp   INTEGER,

    security_title_raw              TEXT,
    security_title_normalized       TEXT,
    normalized_security_type        TEXT,
    security_include_clean_mvp      INTEGER,
    security_requires_review        INTEGER,
    security_confidence             TEXT,

    acquired_disposed_code          TEXT,
    direct_indirect_ownership       TEXT,
    nature_of_ownership             TEXT,

    shares                          REAL,
    price_per_share                 REAL,
    shares_owned_following          REAL,
    transaction_value               REAL,

    is_purchase                     INTEGER,
    is_sale                         INTEGER,
    is_open_market                  INTEGER,

    is_missing_price                INTEGER,
    is_zero_price                   INTEGER,
    is_negative_or_zero_shares      INTEGER,
    is_missing_transaction_date     INTEGER,
    is_missing_filing_date          INTEGER,
    is_filing_before_transaction    INTEGER,
    is_extreme_transaction_value    INTEGER,
    is_tiny_transaction             INTEGER,
    is_valid_clean_event            INTEGER,

    include_mvp_alpha               INTEGER,

    source_year                     INTEGER,
    source_quarter                  INTEGER,
    source_file                     TEXT,
    source_folder                   TEXT,
    created_at                      TEXT
);

CREATE INDEX IF NOT EXISTS idx_clean_ticker
    ON clean_insider_transactions (ticker);
CREATE INDEX IF NOT EXISTS idx_clean_ticker_clean
    ON clean_insider_transactions (ticker_clean);
CREATE INDEX IF NOT EXISTS idx_clean_is_valid_ticker
    ON clean_insider_transactions (is_valid_ticker);
CREATE INDEX IF NOT EXISTS idx_clean_insider_cik
    ON clean_insider_transactions (insider_cik);
CREATE INDEX IF NOT EXISTS idx_clean_issuer_cik
    ON clean_insider_transactions (issuer_cik);
CREATE INDEX IF NOT EXISTS idx_clean_transaction_date
    ON clean_insider_transactions (transaction_date);
CREATE INDEX IF NOT EXISTS idx_clean_transaction_year
    ON clean_insider_transactions (transaction_year);
CREATE INDEX IF NOT EXISTS idx_clean_filing_date
    ON clean_insider_transactions (filing_date);
CREATE INDEX IF NOT EXISTS idx_clean_transaction_code
    ON clean_insider_transactions (transaction_code);
CREATE INDEX IF NOT EXISTS idx_clean_normalized_role
    ON clean_insider_transactions (normalized_role);
CREATE INDEX IF NOT EXISTS idx_clean_normalized_security_type
    ON clean_insider_transactions (normalized_security_type);
CREATE INDEX IF NOT EXISTS idx_clean_include_mvp_alpha
    ON clean_insider_transactions (include_mvp_alpha);
CREATE INDEX IF NOT EXISTS idx_clean_is_stale_filing
    ON clean_insider_transactions (is_stale_filing);
CREATE INDEX IF NOT EXISTS idx_clean_accession_number
    ON clean_insider_transactions (accession_number);
