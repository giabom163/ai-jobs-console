"""Persist actual source observations; never promote business state from readability."""
import hashlib
import json
from datetime import datetime
from .job_fields import save_source_fields
from .job_states import REVIEWABLE_STATUSES


def record_observation(con, job, observation):
    if not observation.get('attempted') or not observation.get('checked_at'):
        return False
    checked = observation['checked_at']
    actual = datetime.fromisoformat(checked.replace('Z', '+00:00'))
    if actual.tzinfo is None:
        raise ValueError('source observation requires timezone')
    current = con.execute('SELECT last_verified_at,status FROM jobs WHERE id=?', (job['id'],)).fetchone()
    if current[0] and actual < datetime.fromisoformat(current[0].replace('Z', '+00:00')):
        return False
    con.execute('UPDATE jobs SET liveness=?,liveness_code=?,liveness_reason=?,liveness_checked_at=?,last_checked_at=?,last_verified_at=?,http_status=?,final_url=? WHERE id=?',
                (observation['verdict'], observation['code'], observation['reason'], checked, checked, checked,
                 observation.get('http_status'), observation.get('final_url'), job['id']))
    evidence = observation.get('snapshot') or dict(kind='source_verification', code=observation['code'],
                verdict=observation['verdict'], requested_job_id=job['external_id'], url=job['url'])
    if observation.get('presence') == 'confirmed':
        evidence.update(presence='confirmed', observed_at=checked,
                        source_status_raw=observation.get('source_status_raw'),
                        url=job['url'])
        raw_response=evidence.get('response_text')
        response_hash=observation.get('response_sha256')
        if not isinstance(raw_response,str) or hashlib.sha256(raw_response.encode('utf8')).hexdigest()!=response_hash:
            raise ValueError('raw response missing or hash differs')
        payload_bytes=json.dumps(evidence.get('payload'),ensure_ascii=False,sort_keys=True,separators=(',', ':')).encode('utf8')
        payload_hash=hashlib.sha256(payload_bytes).hexdigest()
        evidence['payload_sha256']=payload_hash
        con.execute('UPDATE jobs SET presence_status=?,presence_checked_at=?,source_status_raw=?,source_response_sha256=? WHERE id=?',
                    ('confirmed',checked,json.dumps(observation.get('source_status_raw'),ensure_ascii=False),response_hash,job['id']))
        if observation.get('date_evidence'):
            save_source_fields(con, job['id'], published=observation.get('published_at'),
                               updated=observation.get('source_updated_at'),
                               source_status=observation.get('source_status_mapped'), checked_at=checked)
            previous = con.execute('SELECT date_evidence_json FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
            try:
                date_evidence = json.loads(previous) if previous else {}
            except (TypeError, ValueError):
                date_evidence = {}
            date_evidence['latest_detail_observation'] = dict(observation['date_evidence'], observed_at=checked)
            con.execute('UPDATE jobs SET date_evidence_json=? WHERE id=?',
                        (json.dumps(date_evidence,ensure_ascii=False,sort_keys=True),job['id']))
    body = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    con.execute('INSERT INTO job_snapshots(job_id,seen_at,status,content_hash,location,raw_json) VALUES(?,?,?,?,?,?)',
                (job['id'], checked, current[1], job['content_hash'], job['location'], body))
    # The evidence hash describes the response; the snapshot content_hash remains
    # the source job's content hash for compatibility with historical snapshots.
    if observation['verdict'] == 'expired' and current[1] in REVIEWABLE_STATUSES:
        con.execute("UPDATE jobs SET status='closed' WHERE id=?", (job['id'],))
        con.execute('INSERT INTO job_changes(job_id,change_type,changed_at,note) VALUES(?,?,?,?)',
                    (job['id'], 'closed', checked, json.dumps(dict(code=observation['code'], evidence_sha256=hashlib.sha256(body.encode()).hexdigest()))))
    return True


def assert_target_unchanged(con, job):
    keys=('source','external_id','content_hash','status','location','first_seen','last_seen','last_verified_at','last_checked_at')
    current=con.execute('SELECT '+','.join(keys)+' FROM jobs WHERE id=?',(job['id'],)).fetchone()
    if current is None or any(current[i] != job.get(key) for i,key in enumerate(keys)):
        raise ValueError('job changed while source observation was being collected')
