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
  ai_reason TEXT,             -- 判定依据（词典命中/职能排除），便于复核
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  posted_at TEXT,             -- 岗位发布时间（来源给出的发布时间/首次刷新）
  updated_at TEXT,            -- 来源最近一次刷新时间
  published_at TEXT,          -- 明确首次发布时间；不从刷新/获取时间回填
  source_updated_at TEXT,     -- 来源明确更新时间，与本地处理时间分开
  application_start TEXT,
  application_deadline TEXT,
  source_status TEXT,         -- 来源声明；unknown/listed/open/closed，非真实性结论
  source_status_raw TEXT,
  department TEXT,
  requisition_id TEXT,
  last_checked_at TEXT,       -- 具体岗位来源最后检查时间；不等于仍在招
  date_evidence_json TEXT,
  status TEXT NOT NULL DEFAULT 'review',  -- open/review/verification_pending/closed/excluded/merged/stale (see tools/job_states.py)
  url TEXT,
  content_hash TEXT NOT NULL,
  last_verified_at TEXT,
  http_status INTEGER,
  final_url TEXT,
  presence_status TEXT,       -- source ID/content presence, not a hiring-state claim
  presence_checked_at TEXT,
  source_response_sha256 TEXT,
  scored_at TEXT,             -- local scoring time; never a source observation
  authenticity_score REAL NOT NULL DEFAULT 0,
  authenticity_level TEXT NOT NULL DEFAULT '待核验',
  domain_match INTEGER NOT NULL DEFAULT 0,
  cross_source INTEGER NOT NULL DEFAULT 0,
  dedup_key TEXT,
  duplicate_of TEXT,
  merged_at TEXT,
  liveness TEXT,              -- active / expired / uncertain（页面存活判定）
  liveness_code TEXT,
  liveness_reason TEXT,
  liveness_checked_at TEXT,
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

CREATE TABLE IF NOT EXISTS source_registry (
  source_id TEXT PRIMARY KEY,
  source_key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  source_type TEXT NOT NULL,
  enabled INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawler_runs (
  run_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES source_registry(source_id),
  run_kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','partial','failed','preview','interrupted')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count >= 0),
  records_seen INTEGER NOT NULL DEFAULT 0 CHECK(records_seen >= 0),
  records_added INTEGER NOT NULL DEFAULT 0 CHECK(records_added >= 0),
  records_updated INTEGER NOT NULL DEFAULT 0 CHECK(records_updated >= 0),
  error_summary TEXT,
  details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS crawler_runs_source_started
  ON crawler_runs(source_id, started_at DESC);
