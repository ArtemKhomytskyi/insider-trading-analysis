-- Phase 4: normalized entity dimensions derived from clean_insider_transactions.
-- Raw SEC tables and clean_insider_transactions remain untouched.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS entity_build_metadata (
    build_id TEXT PRIMARY KEY,
    started_at TEXT,
    completed_at TEXT,
    status TEXT,
    source_clean_row_count INTEGER,
    source_max_rowid INTEGER,
    builder_version TEXT,
    is_sample INTEGER,
    sample_limit INTEGER,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS clean_insiders (
    insider_cik TEXT PRIMARY KEY,
    canonical_name TEXT,
    insider_name_clean TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    first_filing_date TEXT,
    last_filing_date TEXT,
    transaction_count INTEGER,
    filing_count INTEGER,
    company_count INTEGER,
    purchase_count INTEGER,
    sale_count INTEGER,
    mvp_transaction_count INTEGER,
    is_ever_director INTEGER,
    is_ever_officer INTEGER,
    is_ever_ten_percent_owner INTEGER,
    most_recent_role TEXT,
    most_common_role TEXT,
    most_common_role_group TEXT,
    distinct_raw_name_count INTEGER,
    distinct_role_count INTEGER,
    name_requires_review INTEGER,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS insider_name_aliases (
    insider_cik TEXT,
    raw_name TEXT,
    normalized_name TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    occurrence_count INTEGER,
    is_canonical INTEGER,
    PRIMARY KEY (insider_cik, normalized_name, raw_name)
);

CREATE TABLE IF NOT EXISTS company_dimension (
    issuer_cik TEXT PRIMARY KEY,
    canonical_company_name TEXT,
    current_or_latest_ticker TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    first_filing_date TEXT,
    last_filing_date TEXT,
    transaction_count INTEGER,
    mvp_transaction_count INTEGER,
    unique_insider_count INTEGER,
    distinct_name_count INTEGER,
    distinct_ticker_count INTEGER,
    sector TEXT NULL,
    industry TEXT NULL,
    exchange TEXT NULL,
    market_cap REAL NULL,
    country TEXT NULL,
    currency TEXT NULL,
    metadata_source TEXT NULL,
    metadata_updated_at TEXT NULL,
    company_requires_review INTEGER,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS company_identifier_history (
    issuer_cik TEXT,
    ticker TEXT,
    company_name TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    first_source_year INTEGER,
    last_source_year INTEGER,
    occurrence_count INTEGER,
    is_latest_ticker INTEGER,
    is_latest_name INTEGER,
    ticker_is_valid INTEGER,
    ticker_is_multisymbol INTEGER,
    PRIMARY KEY (issuer_cik, ticker, company_name)
);

CREATE TABLE IF NOT EXISTS insider_company_roles (
    insider_cik TEXT,
    issuer_cik TEXT,
    normalized_role TEXT,
    role_group TEXT,
    role_weight REAL,
    is_executive INTEGER,
    is_board_member INTEGER,
    is_large_owner INTEGER,
    raw_role_title TEXT,
    raw_relationship TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    filing_count INTEGER,
    transaction_count INTEGER,
    role_requires_review INTEGER,
    role_confidence TEXT,
    PRIMARY KEY (
        insider_cik, issuer_cik, normalized_role, first_seen_date,
        raw_role_title, raw_relationship
    )
);

CREATE INDEX IF NOT EXISTS idx_clean_insiders_canonical_name
    ON clean_insiders (canonical_name);
CREATE INDEX IF NOT EXISTS idx_clean_insiders_name_clean
    ON clean_insiders (insider_name_clean);
CREATE INDEX IF NOT EXISTS idx_clean_insiders_most_common_role
    ON clean_insiders (most_common_role);
CREATE INDEX IF NOT EXISTS idx_clean_insiders_last_seen_date
    ON clean_insiders (last_seen_date);

CREATE INDEX IF NOT EXISTS idx_company_dimension_ticker
    ON company_dimension (current_or_latest_ticker);
CREATE INDEX IF NOT EXISTS idx_company_dimension_name
    ON company_dimension (canonical_company_name);
CREATE INDEX IF NOT EXISTS idx_company_dimension_last_seen_date
    ON company_dimension (last_seen_date);

CREATE INDEX IF NOT EXISTS idx_insider_company_roles_insider
    ON insider_company_roles (insider_cik);
CREATE INDEX IF NOT EXISTS idx_insider_company_roles_issuer
    ON insider_company_roles (issuer_cik);
CREATE INDEX IF NOT EXISTS idx_insider_company_roles_role
    ON insider_company_roles (normalized_role);
CREATE INDEX IF NOT EXISTS idx_insider_company_roles_group
    ON insider_company_roles (role_group);

CREATE INDEX IF NOT EXISTS idx_company_identifier_history_issuer
    ON company_identifier_history (issuer_cik);
CREATE INDEX IF NOT EXISTS idx_company_identifier_history_ticker
    ON company_identifier_history (ticker);
CREATE INDEX IF NOT EXISTS idx_company_identifier_history_first_seen
    ON company_identifier_history (first_seen_date);
CREATE INDEX IF NOT EXISTS idx_company_identifier_history_last_seen
    ON company_identifier_history (last_seen_date);
