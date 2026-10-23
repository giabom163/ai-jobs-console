#!/usr/bin/env python
"""Generate the static console page from the maintained web/index.html.

No collection, database writes, build, or deployment. Generates one page.
Usage: python tools/build_console_page.py --out <site>/public/index.html --limit 3
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SHIM = """
<script>
(function(){
  var cache = {};
  async function data(){
    if(!cache.d){ var r = await orig('./console-data.json', {cache:'no-cache'}); if(!r.ok) throw new Error('Dataset HTTP '+r.status); cache.d = await r.json(); }
    return cache.d;
  }
  var orig = window.fetch ? window.fetch.bind(window) : null;
  function resp(obj){ return new Response(JSON.stringify(obj), {status:200, headers:{'Content-Type':'application/json'}}); }
  window.fetch = async function(input, init){
    var u = (typeof input === 'string') ? input : ((input && input.url) || '');
    var url = new URL(u, location.href);
    if (url.origin !== location.origin || url.pathname.indexOf('/api/') !== 0) return orig ? orig(input, init) : Promise.reject(new Error('no fetch'));
    var d = await data();
    var since = url.searchParams.get('since');
    if (url.pathname !== '/api/signals' && since && since !== d.stats.window_since) return new Response(JSON.stringify({error:'Unsupported static date window'}), {status:400});
    var localDates = d.meta && d.meta.display_timezone === 'Asia/Shanghai';
    var timeWarning = localDates ? '' : '旧数据尚未按北京时间重新导出；当日入库与趋势暂不展示。';
    if (url.pathname === '/api/stats') return resp(Object.assign({}, d.stats, {generated_at:d.generated_at, time_warning:timeWarning, today_new:localDates?d.stats.today_new:null, trend:localDates?d.stats.trend:[]}));
    if (url.pathname === '/api/sources') return resp({items: d.sources.map(function(s){
      var jobs = d.jobs.filter(function(j){return j.company === s.name && j.status === 'open';});
      return Object.assign({}, s, {jobs:jobs.length, verified:jobs.filter(function(j){return j.authenticity_level === '已验证';}).length});
    })});
    if (url.pathname === '/api/signals') {
      var p = url.searchParams, review = p.get('review') || 'approved';
      if (review !== 'approved' && review !== 'pending') return new Response(JSON.stringify({error:'unsupported review status'}), {status:400});
      // Older datasets have no review metadata: never promote their RSS entries to reviewed events.
      var items = review === 'pending' ? (d.signal_candidates || []) : (d.signals || []).filter(function(x){return x.review_status === 'approved';});
      var category = p.get('category'), until = p.get('until');
      if(category && !['hiring','layoff','shrink','freeze'].includes(category)) return new Response(JSON.stringify({error:'unsupported event category'}), {status:400});
      if(category) items = items.filter(function(x){return x.category === category;});
      function timestamp(v){return Date.parse(/^\\d{4}-\\d{2}-\\d{2}$/.test(v) ? v+'T00:00:00+08:00' : v);}
      if((since && !Number.isFinite(timestamp(since))) || (until && !Number.isFinite(timestamp(until))) || (since && until && timestamp(since)>=timestamp(until))) return new Response(JSON.stringify({error:'invalid date bounds'}), {status:400});
      if(since) items = items.filter(function(x){return x.published_at && timestamp(x.published_at) >= timestamp(since);});
      if(until) items = items.filter(function(x){return x.published_at && timestamp(x.published_at) < timestamp(until);});
      var cap = Math.min(200,Math.max(1,parseInt(p.get('limit') || '50',10)||50));
      return resp({items:items.slice(0,cap)});
    }
    if (url.pathname === '/api/report') return resp(Object.assign({}, d.report, {today_new:localDates?d.report.today_new:null, signals:(d.signals||[]).filter(function(x){return x.review_status === 'approved';}).length, signal_candidates:(d.signal_candidates||[]).length, note:timeWarning+' 仅统计收录窗口内在招 AI 岗位；链接核验通过代表最近一次 URL 核验通过，不保证当前仍可投递。剔除与合并数为库内累计。'}));
    if (url.pathname === '/api/jobs') {
      var p = url.searchParams;
      var status = p.get('status') || 'open';
      if(status !== 'open') return new Response(JSON.stringify({error:'Static dataset only includes open jobs'}), {status:400});
      var items = d.jobs.filter(function(j){return j.status === status;});
      var q = (p.get('q') || '').trim().toLowerCase();
      if (q) items = items.filter(function(j){ return ((j.title||'') + ' ' + (j.company||'')).toLowerCase().indexOf(q) >= 0; });
      var company = p.get('company');
      if (company) items = items.filter(function(j){ return j.company === company; });
      var ai = p.get('ai');
      if (ai === 'ai') items = items.filter(function(j){ return j.ai_relevance === 'build' || j.ai_relevance === 'adjacent'; });
      else if (ai) items = items.filter(function(j){ return j.ai_relevance === ai; });
      var level = p.get('level');
      if (level) items = items.filter(function(j){ return j.authenticity_level === level; });
      var total = items.length;
      var limit = Math.min(100, Math.max(1, parseInt(p.get('limit') || '30', 10) || 30));
      var offset = Math.max(0, parseInt(p.get('offset') || '0', 10) || 0);
      return resp({total: total, limit: limit, offset: offset, since: d.stats.window_since,
                   items: items.slice(offset, offset + limit)});
    }
    var m = url.pathname.match(/^\\/api\\/jobs\\/(.+)$/);
    if (m) {
      var id = decodeURIComponent(m[1]);
      var job = null;
      for (var i = 0; i < d.jobs.length; i++) if (d.jobs[i].id === id) { job = d.jobs[i]; break; }
      if (!job) return new Response(JSON.stringify({error:'not found'}), {status:404});
      return resp({job: job, snapshots: [], changes: [], history_available: false});
    }
    return new Response(JSON.stringify({error:'not found'}), {status:404});
  };
})();
</script>
"""


def render(html: str) -> str:
    marker = '<script>'
    section = html.find('</section>')
    idx = html.find(marker, section) if section >= 0 else -1
    if idx < 0:
        raise ValueError('app script not found')
    return html[:idx] + SHIM + '\n' + html[idx:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'web' / 'index.html')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=1, help='Maximum output pages; this command produces one')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')
    html = render(args.source.read_text(encoding='utf-8'))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding='utf-8')
    print(f'wrote {args.out} ({len(html)} characters)')


if __name__ == '__main__':
    main()
