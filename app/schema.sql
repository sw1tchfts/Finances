-- Finances schema.
-- Everything is additive. Corrections are made by writing NEW rows and marking
-- the superseded row, never by deleting or mutating in place. This gives a
-- complete audit trail suitable for evidentiary review.

PRAGMA foreign_keys = ON;

-- One row per CSV file ingested. file_hash is sha256 of the raw bytes so we
-- can prove what the source was when the analysis was performed.
CREATE TABLE IF NOT EXISTS imports (
    id            INTEGER PRIMARY KEY,
    filename      TEXT NOT NULL,
    file_hash     TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    imported_at   TEXT NOT NULL DEFAULT (datetime('now')),
    notes         TEXT
);

-- Canonical transactions loaded from Rocket Money CSV. Immutable once inserted.
-- fingerprint is sha256(date|account_number|amount|name|description) so that
-- re-importing an overlapping export is idempotent.
CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY,
    fingerprint       TEXT NOT NULL UNIQUE,
    import_id         INTEGER NOT NULL REFERENCES imports(id),
    date              TEXT NOT NULL,
    original_date     TEXT,
    account_type      TEXT,
    account_name      TEXT,
    account_number    TEXT,
    institution_name  TEXT,
    name              TEXT,
    custom_name       TEXT,
    amount            REAL NOT NULL,
    description       TEXT,
    category          TEXT,
    note              TEXT,
    ignored_from      TEXT,
    tax_deductible    TEXT,
    transaction_tags  TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tx_date     ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_account  ON transactions(account_number);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);

-- Rules drive automatic classification. Priority is ascending: lower number
-- wins when multiple rules match. A rule with enabled=0 is skipped but kept
-- for history.
CREATE TABLE IF NOT EXISTS rules (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT NOT NULL,
    priority               INTEGER NOT NULL DEFAULT 100,
    enabled                INTEGER NOT NULL DEFAULT 1,
    match_account_numbers  TEXT,    -- JSON array of account numbers; null = any
    match_name_regex       TEXT,    -- matched against name || description
    match_category_regex   TEXT,
    match_amount_min       REAL,
    match_amount_max       REAL,
    match_date_from        TEXT,
    match_date_to          TEXT,
    match_sign             TEXT,    -- 'positive' (outflow), 'negative' (inflow), or null
    classification         TEXT NOT NULL,    -- see classifications.classification
    split_user_pct         REAL,
    split_partner_pct      REAL,
    note                   TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT
);

-- Current classification of each transaction. Multiple historical rows per
-- transaction; only one has superseded_at IS NULL (the "current" classification).
--
-- classification values:
--   business_income   - counts toward business revenue
--   business_expense  - legitimate business expense, offsets revenue
--   shared_expense    - household expense the partners agreed to share
--   personal          - one partner's private expense
--   transfer          - credit card payment, internal transfer, loan payment
--   excluded          - intentionally ignored (e.g. mis-categorized dupe)
--   unclassified      - default; hasn't been touched
CREATE TABLE IF NOT EXISTS classifications (
    id                 INTEGER PRIMARY KEY,
    transaction_id     INTEGER NOT NULL REFERENCES transactions(id),
    classification     TEXT NOT NULL,
    split_user_pct     REAL,
    split_partner_pct  REAL,
    source             TEXT NOT NULL,        -- 'rule:<id>' | 'manual' | 'default'
    rule_id            INTEGER REFERENCES rules(id),
    note               TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at      TEXT,
    superseded_by      INTEGER REFERENCES classifications(id)
);

CREATE INDEX IF NOT EXISTS idx_cls_tx      ON classifications(transaction_id);
CREATE INDEX IF NOT EXISTS idx_cls_current ON classifications(transaction_id) WHERE superseded_at IS NULL;

-- Uploaded receipts. File bytes live on disk under data/receipts/, content
-- hash stored for tamper-detection.
CREATE TABLE IF NOT EXISTS receipts (
    id               INTEGER PRIMARY KEY,
    transaction_id   INTEGER REFERENCES transactions(id),
    filename         TEXT NOT NULL,
    stored_path      TEXT NOT NULL,
    file_hash        TEXT NOT NULL,
    mime_type        TEXT,
    size_bytes       INTEGER,
    note             TEXT,
    uploaded_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_receipt_tx ON receipts(transaction_id);

-- Partnership window, partner names, default split, etc.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Everything mutating goes here. Read this table to reconstruct the history
-- of every decision in the system.
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY,
    event_type   TEXT NOT NULL,   -- 'import'|'rule_create'|'rule_update'|'classify'|'receipt_upload'|'setting_update'
    entity_type  TEXT NOT NULL,
    entity_id    INTEGER,
    details      TEXT,            -- JSON
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
