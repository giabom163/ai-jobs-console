"""Provider-neutral, read-only validation for human-reviewed source artifacts."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

# Sources are approved for public-page evidence only. Social posts remain claims
# by their authors; Mainland location and the post's publication date still need
# explicit excerpts before a record can be approved for public display.
APPROVED_EVENT_SOURCE_STRATEGIES = {
    'cn-official-employment-notices-v1': {
        'hosts': ('www.gov.cn', 'www.miit.gov.cn'),
        'region': 'CN-mainland',
        'mode': 'manual-discovery-primary-evidence',
        'approved_at': '2026-11-05',
    },
    'social-self-report-v1': {
        'hosts': ('www.v2ex.com', 'www.bilibili.com', 'www.douyin.com', 'www.youtube.com'),
        'region': 'CN-mainland-claim-evidence-required',
        'mode': 'public-author-self-report',
        'approved_at': '2026-11-06',
    },
}

# On-demand search is discovery only. Returned pages remain untrusted candidates.
APPROVED_EVENT_DISCOVERY_PROVIDERS = {
    'deepseek-direct-search-v1': {
        'provider': 'DeepSeek Search',
        'region_scope': 'CN-mainland-query-only',
        'trigger': 'operator_click',
        'max_results': 3,
        'automatic_source_fetch': False,
        'automatic_publication': False,
    },
}


def _valid_web_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {'http', 'https'} and bool(parsed.hostname) and not parsed.username and not parsed.password


def normalize_observation(evidence, document_url, content, evidence_root):
    """Return normalized observation metadata after checking the saved artifact.

    Legacy Fetch fields are adapted only when their original receipt contains the
    required evidence. Missing timestamps or redirect destinations are not invented.
    """
    observation = evidence.get('source_observation')
    legacy = False
    if observation is None:
        receipt = evidence.get('fetch_receipt') or {}
        legacy = True
        observation = {
            'requested_url': (receipt.get('input') or {}).get('url'),
            'final_url': receipt.get('final_url'),
            'observed_at': receipt.get('observed_at') or receipt.get('completed_at'),
            'http_status': receipt.get('http_status'),
            'raw_file_path': receipt.get('response_file'),
            'raw_file_sha256': receipt.get('response_sha256'),
            'extracted_text_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
            'extractor_version': receipt.get('extractor_version') or 'legacy-unrecorded',
            'published_at': receipt.get('published_at'),
            'date_span': receipt.get('date_span'),
            'source_strategy_id': receipt.get('source_strategy_id'),
            'legacy_adapter': 'fetch-receipt-v1',
        }
        if receipt.get('action') != 'fetch' or receipt.get('dispatched') is not True:
            raise ValueError('source observation must be dispatched evidence')
    if not isinstance(observation, dict):
        raise ValueError('source_observation must be an object')
    required = ('requested_url', 'observed_at', 'http_status', 'raw_file_path',
                'raw_file_sha256', 'extracted_text_sha256', 'extractor_version')
    if any(observation.get(key) in (None, '') for key in required):
        raise ValueError('source observation is missing required provenance fields')
    if observation['requested_url'] != document_url or not _valid_web_url(document_url):
        raise ValueError('requested_url must match the stored document URL')
    strategy_id = observation.get('source_strategy_id')
    strategy = APPROVED_EVENT_SOURCE_STRATEGIES.get(strategy_id)
    if strategy is None:
        raise ValueError('event source strategy is not approved; existing job-source permission does not apply')
    requested_host = urlsplit(observation['requested_url']).hostname
    if requested_host not in strategy.get('hosts', ()):
        raise ValueError('requested_url host is outside the approved event source strategy')
    if not legacy and (not _valid_web_url(observation.get('final_url'))):
        raise ValueError('normalized evidence requires a valid final_url')
    try:
        observed = datetime.fromisoformat(str(observation['observed_at']).replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('observed_at must be an ISO timestamp') from error
    if observed.tzinfo is None:
        raise ValueError('observed_at must include a timezone')
    if type(observation['http_status']) is not int or observation['http_status'] != 200:
        raise ValueError('source observation must record HTTP 200')
    if not isinstance(observation['extractor_version'], str) or not observation['extractor_version'].strip():
        raise ValueError('extractor_version is required')
    artifact = Path(observation['raw_file_path']).resolve()
    root = Path(evidence_root).resolve()
    if not artifact.is_relative_to(root) or not artifact.is_file():
        raise ValueError('raw evidence artifact must exist under the evidence root')
    raw_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if raw_hash != observation['raw_file_sha256']:
        raise ValueError('raw evidence artifact hash differs')
    text_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
    if text_hash != observation['extracted_text_sha256']:
        raise ValueError('extracted text hash differs from the reviewed document')
    normalized = dict(observation)
    normalized['legacy_adapter'] = 'fetch-receipt-v1' if legacy else normalized.get('legacy_adapter')
    return normalized


def is_reviewed_observation(evidence, document_url, published_at, content_sha256):
    observation = evidence.get('source_observation')
    if not isinstance(observation, dict):
        return False
    strategy = APPROVED_EVENT_SOURCE_STRATEGIES.get(observation.get('source_strategy_id'))
    if strategy is None or urlsplit(str(observation.get('requested_url') or '')).hostname not in strategy.get('hosts', ()):
        return False
    artifact = Path(str(observation.get('raw_file_path') or '')).resolve()
    evidence_root = Path.home()/'.codex'/'documents'
    if not artifact.is_relative_to(evidence_root.resolve()) or not artifact.is_file():
        return False
    try:
        artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    except OSError:
        return False
    return bool(
        evidence.get('source_verified') is True and isinstance(observation, dict) and
        observation.get('requested_url') == document_url and
        observation.get('final_url') and observation.get('observed_at') and
        observation.get('http_status') == 200 and
        observation.get('published_at') == published_at and
        observation.get('extracted_text_sha256') == content_sha256 and
        observation.get('raw_file_sha256') == artifact_hash and
        observation.get('extractor_version')
    )
