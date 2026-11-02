"""Separate source facts from observations, with read compatibility for old DBs."""
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
SOURCE_COLUMNS = (
    'published_at', 'source_updated_at', 'application_start', 'application_deadline',
    'source_status', 'source_status_raw', 'department', 'requisition_id',
    'last_checked_at', 'date_evidence_json',
)


def ensure_columns(con):
    have = {row[1] for row in con.execute('PRAGMA table_info(jobs)')}
    for name in SOURCE_COLUMNS:
        if name not in have:
            con.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')


def register_read_view(con):
    """Never reinterpret legacy posted_at/updated_at as verified source dates."""
    have = {row[1] for row in con.execute('PRAGMA main.table_info(jobs)')}
    missing = ','.join(f'NULL AS {name}' for name in SOURCE_COLUMNS if name not in have)
    con.execute('CREATE TEMP VIEW job_records AS SELECT jobs.*' +
                (',' + missing if missing else '') + ' FROM main.jobs')


def recent_since():
    return max('2026-06-01', (datetime.now(TZ) - timedelta(days=30)).date().isoformat())


def save_source_fields(con, jid, *, published=None, updated=None, department=None,
                       source_status=None, source_status_raw=None, checked_at=None):
    if not checked_at:
        raise ValueError('source facts require the actual observation timestamp')
    observation=datetime.fromisoformat(checked_at.replace('Z','+00:00'))
    if observation.tzinfo is None:
        raise ValueError('source observation timestamp requires a timezone')
    for name,value in [('published',published),('updated',updated)]:
        try:
            if value: datetime.fromisoformat(str(value).replace('Z','+00:00'))
        except (ValueError,TypeError):
            if name=='published': published=None
            else: updated=None
    # First publication is immutable after being evidenced. Corrections need a separate audit.
    # Neither an older observation nor an older source update may overwrite a newer fact.
    con.execute('UPDATE jobs SET published_at=COALESCE(published_at,?),'
                'source_updated_at=CASE WHEN source_updated_at IS NULL OR julianday(?)>=julianday(source_updated_at) THEN COALESCE(?,source_updated_at) ELSE source_updated_at END,department=COALESCE(?,department),'
                'source_status=COALESCE(?,source_status),source_status_raw=CASE WHEN ? IS NOT NULL THEN ? ELSE source_status_raw END,'
                'last_checked_at=? WHERE id=? AND (last_checked_at IS NULL OR julianday(?)>=julianday(last_checked_at))',
                (published, updated, updated, department, source_status, source_status, source_status_raw, checked_at, jid, checked_at))
