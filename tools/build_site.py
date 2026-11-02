"""Build the local static console only. No collection, DB writes, tests or deployment."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

if __package__:
    from .build_console_page import render
    from .export_console import build, connect
else:
    from build_console_page import render
    from export_console import build, connect

ROOT = Path(__file__).resolve().parent.parent


def split_details(payload):
    """Keep list facets small; immutable shards retain each full job and evidence."""
    groups = {}
    for job in payload['jobs']:
        bucket = f"{hashlib.sha256(job['id'].encode()).digest()[0] % 32:02x}"
        groups.setdefault(bucket, {})[job['id']] = job
    artifacts, locations = {}, {}
    for bucket, jobs in sorted(groups.items()):
        content = json.dumps({'jobs': jobs}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        name = 'details-' + bucket + '.' + hashlib.sha256(content).hexdigest()[:12] + '.json'
        artifacts[name] = content
        locations.update({jid: name for jid in jobs})
    index = dict(payload, meta=dict(payload['meta'], detail_loading='sharded-v1', detail_shards=len(artifacts)))
    index['jobs'] = []
    for job in payload['jobs']:
        slim = {k:v for k,v in job.items() if k not in {'full_description','job_details','requirements_summary','duplicate_records'}}
        slim['detail_file'] = locations[job['id']]
        slim['requirements_count'] = len(job.get('requirements_summary', {}).get('items', []))
        details = job.get('job_details', {})
        list_details = {key: {'value': details[key].get('value')}
                        for key in ('experience', 'education')
                        if isinstance(details.get(key), dict) and details[key].get('value')}
        if list_details:
            slim['job_details'] = list_details
        index['jobs'].append(slim)
    return index, artifacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=3, help='Maximum build groups: page, script, data bundles; start with 3. No collection.')
    parser.add_argument("--prune-obsolete", action="store_true", help="Remove only obsolete generated bundles from site after enumerating them")
    args = parser.parse_args()
    if args.limit < 3:
        parser.error('three build groups are required; start with --limit 3')
    site = ROOT / 'site'
    site.mkdir(exist_ok=True)
    # Read all inputs before writing an artifact. The database stays read-only.
    html = render((ROOT / 'web' / 'index.html').read_text(encoding='utf-8'))
    js = (ROOT / 'web' / 'catalog.js').read_bytes()
    asset_name = 'catalog.' + hashlib.sha256(js).hexdigest()[:12] + '.js'
    # Keep the critical startup code in the document so a stalled script request
    # cannot prevent the loading and retry UI from starting.
    marker = '<script src="./catalog.js"></script>'
    if html.count(marker) != 1 or '</script' in js.decode('utf-8').lower():
        raise ValueError('catalog script cannot be safely embedded')
    html = html.replace(marker, '<script>\n' + js.decode('utf-8') + '\n</script>', 1)
    con = connect()
    try:
        payload = build(con)
    finally:
        con.close()
    index, artifacts = split_details(payload)
    index_content = json.dumps(index, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    index_name = 'jobs-index.' + hashlib.sha256(index_content).hexdigest()[:12] + '.json'
    html = html.replace("'./console-data.json'", "'./" + index_name + "'")
    artifacts.update({'index.html': html, asset_name: js,
                      index_name: index_content,
                      # Keep the old public data URL compatible with already-open pages.
                      'console-data.json': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))})
    for name, value in artifacts.items():
        if isinstance(value, bytes):
            (site / name).write_bytes(value)
        else:
            (site / name).write_text(value, encoding='utf-8')
    # Publish only this build's reachable files, never previous job datasets.
    expected = set(artifacts) | {'_redirects'}
    obsolete = [p for p in site.iterdir() if p.is_file() and p.name not in expected
                and (re.fullmatch(r'(?:details-[0-9a-f]{2}|jobs-index|catalog)\.[0-9a-f]{12}\.(?:json|js)', p.name)
                     or p.name == 'catalog.js')]
    print(json.dumps({'obsolete_generated_files': [p.name for p in obsolete]}))
    if args.prune_obsolete:
        for path in obsolete:
            if path.resolve().parent != site.resolve():
                raise ValueError('refusing path outside site')
            path.unlink()
    files = {name: hashlib.sha256((site / name).read_bytes()).hexdigest()
             for name in sorted(expected) if (site / name).is_file()}
    manifest = {'generated_at': payload['generated_at'], 'jobs': len(payload['jobs']),
                'schema_version': payload['meta']['schema_version'], 'files': files,
                'detail_shards': len({j['detail_file'] for j in index['jobs']}),
                'index_file': index_name, 'index_bytes': len(index_content),
                'database_mode': 'read_only', 'tests_run': False, 'deployed': False}
    (site / 'build-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    main()
