-- Additive discovery storage. Existing jobs/signals are not rewritten.
CREATE TABLE IF NOT EXISTS discovery_documents (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    date_basis TEXT NOT NULL DEFAULT 'unknown',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    query TEXT NOT NULL,
    category TEXT NOT NULL,
    ai_scope TEXT NOT NULL DEFAULT 'pending',
    claim_status TEXT NOT NULL DEFAULT 'unverified',
    evidence_json TEXT NOT NULL,
    company_id TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(review_status IN ('pending','approved','excluded')),
    liveness TEXT NOT NULL DEFAULT 'unknown',
    last_verified_at TEXT
);
CREATE INDEX IF NOT EXISTS discovery_review_date
    ON discovery_documents(review_status, published_at);
