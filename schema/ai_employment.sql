-- AI 就业岗位库（SQLite）
-- 与原 sentiment.db 分离；不存候选人个人信息。

CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  ats_type TEXT,              -- greenhouse / lever / ashby / workable / custom
  board_token TEXT,           -- ATS 的 board / org token
  watchlist INTEGER NOT NULL DEFAULT 1,
  domain TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,       -- ats:greenhouse / ats:lever / boss / official
  external_id TEXT NOT NULL,
  company_id TEXT REFERENCES companies(id),
  title TEXT NOT NULL,
  normalized_title TEXT,
  location TEXT,
  remote INTEGER,
  salary_min REAL,
  salary_max REAL,
  currency TEXT,
  seniority TEXT,
  description TEXT,           -- 只存摘要/清洗后文本，原始放 snapshot
  skills_json TEXT,
  ai_relevance TEXT,          -- build / adjacent / exposed / none
  ai_confidence REAL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',  -- open / closed
  url TEXT,
  content_hash TEXT NOT NULL,
  last_verified_at TEXT,
  http_status INTEGER,
  final_url TEXT,
  authenticity_score REAL NOT NULL DEFAULT 0,
  authenticity_level TEXT NOT NULL DEFAULT '待核验',
  domain_match INTEGER NOT NULL DEFAULT 0,
  cross_source INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS job_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  seen_at TEXT NOT NULL,
  status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  salary_min REAL,
  salary_max REAL,
  location TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS job_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  change_type TEXT NOT NULL,  -- new / changed / closed
  changed_at TEXT NOT NULL,
  old_hash TEXT,
  new_hash TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS job_daily (
  day TEXT NOT NULL,
  company_id TEXT REFERENCES companies(id),
  new_jobs INTEGER NOT NULL DEFAULT 0,
  closed_jobs INTEGER NOT NULL DEFAULT 0,
  changed_jobs INTEGER NOT NULL DEFAULT 0,
  ai_jobs INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, company_id)
);

CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS job_changes_day ON job_changes(changed_at);

CREATE TABLE IF NOT EXISTS job_evidence (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  source TEXT NOT NULL,
  url TEXT,
  seen_at TEXT NOT NULL,
  PRIMARY KEY(job_id,source)
);

CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT,
  source TEXT,
  published_at TEXT,
  summary TEXT,
  tag TEXT
);
