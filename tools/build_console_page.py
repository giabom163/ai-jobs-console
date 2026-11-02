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
  var cache = new Map();
  async function data(){
    return PayloadLoader.once(cache,'index',async function(){
      try { return await PayloadLoader.json(orig,'./console-data.json',{timeout:15000}); }
      catch(error) {
        if (/^HTTP (?!5[0-9][0-9])/.test(error.message)) throw error;
        return PayloadLoader.json(orig,'./console-data.json',{timeout:15000});
      }
    });
  }
  var orig = window.fetch ? window.fetch.bind(window) : null;
  function resp(obj){ return new Response(JSON.stringify(obj), {status:200, headers:{'Content-Type':'application/json'}}); }
  window.fetch = async function(input, init){
    var u = (typeof input === 'string') ? input : ((input && input.url) || '');
    var url = new URL(u, location.href);
    if (url.origin !== location.origin || url.pathname.indexOf('/api/') !== 0) return orig ? orig(input, init) : Promise.reject(new Error('no fetch'));
    var d = await data();
    if(url.pathname === '/api/employment-events') {
      try {var ledger=d.employment_events||{events:[],coverage_note:'尚无事件数据'};var events=EventCatalog.select(ledger.events,url.searchParams);return resp({items:events,summary:EventCatalog.summarize(events),coverage_note:ledger.coverage_note});}
      catch(error){return new Response(JSON.stringify({error:error.message}),{status:400});}
    }
    var since = url.searchParams.get('since');
    var facetFilters;
    try { facetFilters = JobCatalog.filters(url.pathname === '/api/signals' ? new URLSearchParams() : url.searchParams); }
    catch(error){return new Response(JSON.stringify({error:error.message}),{status:400});}
    var jobSince = facetFilters.since;
    var windowJobs = JobCatalog.select(d.jobs,facetFilters);
    var reviewJobs = JobCatalog.select(d.jobs,Object.assign({},facetFilters,{status:'review'}));
    var stats = JobCatalog.summarize(windowJobs,d.stats,facetFilters,reviewJobs);
    var localDates = d.meta && d.meta.display_timezone === 'Asia/Shanghai';
    var timeWarning = localDates ? '' : '旧数据尚未按北京时间重新导出；当日入库与趋势暂不展示。';
    if (url.pathname === '/api/stats') return resp(Object.assign({}, stats, {generated_at:d.generated_at, time_warning:timeWarning, today_new:localDates?stats.today_new:null, trend:localDates?stats.trend:[]}));
    if (url.pathname === '/api/sources') return resp({health:d.source_health||{items:[],note:'旧版本尚无来源运行数据，请重新构建。'},items: d.sources.map(function(s){
      var jobs = windowJobs.filter(function(j){return j.company_id === s.company_id;});
      var pending = reviewJobs.filter(function(j){return j.company_id === s.company_id;});
      return Object.assign({}, s, {jobs:jobs.length,pending_review:pending.length,last_seen:jobs.map(function(j){return j.last_seen;}).filter(Boolean).sort().pop()||null,verified:jobs.filter(function(j){return Number.isInteger(j.http_status)&&j.http_status>=200&&j.http_status<300;}).length});
    })});
    if (url.pathname === '/api/signals') {
      var p = url.searchParams, review = p.get('review') || 'approved';
      if (review !== 'approved' && review !== 'pending') return new Response(JSON.stringify({error:'unsupported review status'}), {status:400});
      // Older datasets have no review metadata: never promote their RSS entries to reviewed events.
      var items = review === 'pending' ? [] : (d.signals || []).filter(function(x){return x.review_status === 'approved';});
      var category = p.get('category'), until = p.get('until');
      if(category && !['hiring','layoff','shrink','freeze'].includes(category)) return new Response(JSON.stringify({error:'unsupported event category'}), {status:400});
      if(category) items = items.filter(function(x){return x.category === category;});
      function timestamp(v){return Date.parse(/^\\d{4}-\\d{2}-\\d{2}$/.test(v) ? v+'T00:00:00+08:00' : v);}
      if((since && !Number.isFinite(timestamp(since))) || (until && !Number.isFinite(timestamp(until))) || (since && until && timestamp(since)>=timestamp(until))) return new Response(JSON.stringify({error:'invalid date bounds'}), {status:400});
      if(since) items = items.filter(function(x){return x.published_at && timestamp(x.published_at) >= timestamp(since);});
      if(until) items = items.filter(function(x){return x.published_at && timestamp(x.published_at) < timestamp(until);});
      var cap = Math.min(200,Math.max(1,parseInt(p.get('limit') || '50',10)||50));
      return resp({items:items.slice(0,cap),storage:d.signal_storage||{storage_status:'unknown',note:'旧版本未提供存储状态。'},note:review==='pending'?'待核验原文仅供本地复核，不随公开站导出。':null});
    }
    if (url.pathname === '/api/report') return resp(Object.assign({}, d.report, {total:stats.ai_total,intern:stats.intern,regular:stats.regular,unknown:stats.unknown,other_employment:stats.other_employment,build:stats.build,adjacent:stats.adjacent,verified:stats.verified,pending:stats.ai_total-stats.verified,companies:stats.companies,top_companies:stats.top_companies.slice(0,5),window_since:jobSince,date_basis:'first_seen',today_new:localDates?stats.today_new:null, signals:(d.signals||[]).filter(function(x){return x.review_status === 'approved';}).length, signal_candidates:(d.signal_candidates||[]).length, note:timeWarning+' 按首次发现时间筛选；发布时间未知保持空值。链接可读不代表当前可投递。'}));
    if (url.pathname === '/api/jobs') {
      var p = url.searchParams;
      var status = p.get('status') || 'listed';
      if(!['listed','open','review'].includes(status)) return new Response(JSON.stringify({error:'Unsupported record status'}), {status:400});
      var items = windowJobs.slice();
      var total = items.length;
      var limit = Math.min(100, Math.max(1, parseInt(p.get('limit') || '30', 10) || 30));
      var offset = Math.max(0, parseInt(p.get('offset') || '0', 10) || 0);
      return resp({total: total, limit: limit, offset: offset, since: jobSince, date_basis:'first_seen',
                   meta:d.meta, items: items.slice(offset, offset + limit)});
    }
    var m = url.pathname.match(/^\\/api\\/jobs\\/(.+)$/);
    if (m) {
      var id = decodeURIComponent(m[1]);
      var job = null;
      for (var i = 0; i < d.jobs.length; i++) if (d.jobs[i].id === id) { job = d.jobs[i]; break; }
      if (!job) return new Response(JSON.stringify({error:'not found'}), {status:404});
      if(job.detail_file){
        if(!/^details-[0-9a-f]{2}\\.[0-9a-f]{12}\\.json$/.test(job.detail_file)) return new Response(JSON.stringify({error:'invalid detail file'}),{status:500});
        var shard=await PayloadLoader.once(cache,job.detail_file,function(){return PayloadLoader.json(orig,'./'+job.detail_file,{timeout:15000});});
        job=shard.jobs && shard.jobs[id];
        if(!job||job.id!==id) return new Response(JSON.stringify({error:'detail unavailable'}),{status:404});
      }
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
    parser.add_argument('--out', type=Path, default=ROOT / 'site' / 'index.html')
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
