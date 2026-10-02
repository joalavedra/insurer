PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS quotes (
    quote_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    premium_cents INTEGER NOT NULL,
    tax_cents INTEGER NOT NULL,
    earned_cents INTEGER NOT NULL DEFAULT 0,
    earned_tax_cents INTEGER NOT NULL DEFAULT 0,
    earned_through TEXT NOT NULL,
    aggregate_paid_cents INTEGER NOT NULL DEFAULT 0,
    case_reserve_cents INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (quote_id) REFERENCES quotes(quote_id)
);

CREATE TABLE IF NOT EXISTS policy_versions (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    profile_json TEXT NOT NULL,
    premium_cents INTEGER NOT NULL,
    tax_cents INTEGER NOT NULL,
    earned_before_cents INTEGER NOT NULL,
    earned_tax_before_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (policy_id, version),
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    cause TEXT NOT NULL,
    loss_date TEXT NOT NULL,
    notified_date TEXT NOT NULL,
    purchase_id TEXT NOT NULL,
    claimed_cents INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    paid_cents INTEGER NOT NULL DEFAULT 0,
    reserve_cents INTEGER NOT NULL DEFAULT 0,
    fraud_truth INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_postings (
    posting_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL,
    account TEXT NOT NULL,
    debit_cents INTEGER NOT NULL DEFAULT 0,
    credit_cents INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (entry_id) REFERENCES ledger_entries(entry_id)
);

CREATE TABLE IF NOT EXISTS bordereau_rows (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    month TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
