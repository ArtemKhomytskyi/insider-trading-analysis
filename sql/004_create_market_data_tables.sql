-- Phase 5: historical security mapping and market-data acquisition tables.
-- Derived layer only; raw SEC, clean transactions, and Phase 4 entity tables remain unchanged.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS security_identifier_mapping (
    mapping_id TEXT PRIMARY KEY,
    issuer_cik TEXT NOT NULL,
    ticker_raw TEXT,
    ticker_clean TEXT,
    market_data_symbol TEXT,
    security_title TEXT,
    security_class TEXT NULL,
    valid_from TEXT,
    valid_to TEXT,
    exchange TEXT NULL,
    mapping_source TEXT,
    mapping_method TEXT,
    mapping_confidence TEXT,
    is_primary_mapping INTEGER,
    is_current INTEGER,
    is_delisted INTEGER,
    is_ticker_change INTEGER,
    is_reused_ticker INTEGER,
    is_ambiguous INTEGER,
    requires_review INTEGER,
    source_first_seen_date TEXT,
    source_last_seen_date TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS transaction_market_mapping (
    transaction_id TEXT PRIMARY KEY,
    issuer_cik TEXT,
    transaction_date TEXT,
    filing_date TEXT,
    ticker_at_transaction TEXT,
    market_data_symbol TEXT,
    mapping_id TEXT,
    mapping_confidence TEXT,
    mapping_method TEXT,
    mapping_status TEXT,
    candidate_count_raw INTEGER,
    candidate_count_logical INTEGER,
    resolution_method TEXT,
    ambiguity_reason TEXT,
    source_ticker_match INTEGER,
    is_mapped INTEGER,
    is_ambiguous INTEGER,
    requires_review INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS market_prices_daily (
    market_data_symbol TEXT,
    trading_date TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adjusted_open REAL NULL,
    adjusted_high REAL NULL,
    adjusted_low REAL NULL,
    adjusted_close REAL NOT NULL,
    volume REAL,
    provider TEXT,
    currency TEXT NULL,
    exchange TEXT NULL,
    is_adjusted INTEGER,
    downloaded_at TEXT,
    PRIMARY KEY (market_data_symbol, trading_date, provider)
);

CREATE TABLE IF NOT EXISTS market_data_download_status (
    market_data_symbol TEXT,
    provider TEXT,
    requested_start TEXT,
    requested_end TEXT,
    downloaded_start TEXT,
    downloaded_end TEXT,
    row_count INTEGER,
    status TEXT,
    attempt_count INTEGER,
    last_error TEXT,
    last_attempt_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (market_data_symbol, provider, requested_start, requested_end)
);

CREATE TABLE IF NOT EXISTS market_splits (
    market_data_symbol TEXT,
    event_date TEXT,
    event_type TEXT,
    value REAL,
    provider TEXT,
    downloaded_at TEXT,
    PRIMARY KEY (market_data_symbol, event_date, provider)
);

CREATE TABLE IF NOT EXISTS market_dividends (
    market_data_symbol TEXT,
    event_date TEXT,
    event_type TEXT,
    value REAL,
    provider TEXT,
    downloaded_at TEXT,
    PRIMARY KEY (market_data_symbol, event_date, provider)
);

CREATE INDEX IF NOT EXISTS idx_security_mapping_issuer_interval
    ON security_identifier_mapping (issuer_cik, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_security_mapping_symbol
    ON security_identifier_mapping (market_data_symbol);
CREATE INDEX IF NOT EXISTS idx_security_mapping_ticker
    ON security_identifier_mapping (ticker_clean);

CREATE INDEX IF NOT EXISTS idx_transaction_market_mapping_tid
    ON transaction_market_mapping (transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_market_mapping_issuer
    ON transaction_market_mapping (issuer_cik);
CREATE INDEX IF NOT EXISTS idx_transaction_market_mapping_symbol
    ON transaction_market_mapping (market_data_symbol);
CREATE INDEX IF NOT EXISTS idx_transaction_market_mapping_flags
    ON transaction_market_mapping (is_mapped, is_ambiguous, requires_review);

CREATE INDEX IF NOT EXISTS idx_market_prices_symbol_date
    ON market_prices_daily (market_data_symbol, trading_date);
CREATE INDEX IF NOT EXISTS idx_market_download_status_status
    ON market_data_download_status (status);

CREATE TABLE IF NOT EXISTS security_mapping_build_metadata (
    build_id TEXT PRIMARY KEY,
    started_at TEXT,
    completed_at TEXT,
    status TEXT,
    source_identifier_row_count INTEGER,
    mapping_row_count INTEGER,
    transaction_mapping_row_count INTEGER,
    builder_version TEXT,
    is_sample INTEGER,
    sample_limit INTEGER,
    error_message TEXT
);
