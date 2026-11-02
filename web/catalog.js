/* Static dataset facets. Field semantics mirror tools/job_catalog.py. */
(function(global){
  'use strict';
  const floor='2026-06-01', roles=['research','application','platform','product','data','operations','solutions','unknown'];
  const employment=['intern','regular','contract','part_time','unknown'], cities=['beijing','shanghai','hangzhou','shenzhen'];
  const localDay=value=>new Date(Date.parse(value)+8*3600000).toISOString().slice(0,10);
  const subMillisecond=value=>{const m=String(value).match(/\.(\d+)/);return Number((m?m[1]:'').padEnd(6,'0').slice(3,6));};
  function filters(params){
    const f={};['q','company','ai','level','status','role','employment','cities','city_text','education_text','experience_text','since','until'].forEach(k=>f[k]=(params.get(k)||'').trim());
    f.status=f.status||'listed';
    [['role',roles],['employment',employment],['ai',['ai','build','adjacent']],['status',['listed','open','review']]].forEach(([k,allowed])=>{if(f[k]&&!allowed.includes(f[k]))throw Error('unsupported '+k);});
    f.cities=[...new Set(f.cities.split(',').filter(Boolean))];
    if(f.cities.some(c=>!cities.includes(c)))throw Error('unsupported city');
    f.since=f.since==='all'?floor:(f.since||new Date(Date.now()+8*3600000-30*86400000).toISOString().slice(0,10));
    ['since','until'].forEach(k=>{if(f[k]&&(!/^\d{4}-\d{2}-\d{2}$/.test(f[k])||!Number.isFinite(Date.parse(f[k]+'T00:00:00Z'))||new Date(f[k]+'T00:00:00Z').toISOString().slice(0,10)!==f[k]))throw Error('invalid date');});
    if(f.since<floor)f.since=floor;
    if(f.until&&f.until<f.since)throw Error('invalid date bounds');
    return f;
  }
  function select(jobs,f){
    return jobs.filter(j=>{
      if(!['open','review'].includes(j.status)||!j.first_seen)return false;
      if(f.status==='listed'&&j.status!=='open')return false;
      if(f.status!=='listed'&&j.status!==f.status)return false;
      const day=localDay(j.first_seen);
      if(day<f.since||(f.until&&day>f.until))return false;
      if(f.q&&!(j.search_text||((j.title||'')+'\n'+(j.company||''))).toLowerCase().includes(f.q.toLowerCase()))return false;
      if([['company','company'],['level','authenticity_level'],['role','role_family'],['employment','employment_type']].some(([key,field])=>f[key]&&(j[field]||(field==='employment_type'||field==='role_family'?'unknown':''))!==f[key]))return false;
      if(['build','adjacent'].includes(f.ai)&&j.ai_relevance!==f.ai)return false;
      if(f.cities.length&&!f.cities.some(c=>(j.city_codes||[]).includes(c)))return false;
      if(f.city_text&&!(j.location||'').toLowerCase().includes(f.city_text.toLowerCase()))return false;
      const details=j.job_details||{};
      if(f.education_text&&!String(details.education?.value||'').toLowerCase().includes(f.education_text.toLowerCase()))return false;
      if(f.experience_text&&!String(details.experience?.value||'').toLowerCase().includes(f.experience_text.toLowerCase()))return false;
      return true;
    }).sort((a,b)=>Date.parse(b.first_seen)-Date.parse(a.first_seen)||subMillisecond(b.first_seen)-subMillisecond(a.first_seen)||(a.id<b.id?1:a.id>b.id?-1:0));
  }
  function groups(jobs,key){const counts={};jobs.forEach(j=>{const k=j[key]||'未知';counts[k]=(counts[k]||0)+1;});return Object.entries(counts).map(([label,value])=>({label,value})).sort((a,b)=>b.value-a.value||a.label.localeCompare(b.label));}
  function summarize(jobs,base,f,reviewJobs){
    const count=k=>jobs.filter(j=>(j.employment_type||'unknown')===k).length;
    const today=localDay(new Date().toISOString()),week=localDay(new Date(Date.now()-6*86400000).toISOString()),start=localDay(new Date(Date.now()-13*86400000).toISOString()),days={};
    jobs.forEach(j=>{const day=localDay(j.first_seen);if(day>=start)days[day]=(days[day]||0)+1;});
    return Object.assign({},base,{window_since:f.since,date_basis:'first_seen',ai_total:jobs.length,total:jobs.length,open:jobs.filter(j=>j.status==='open').length,review_count:reviewJobs?reviewJobs.length:jobs.filter(j=>j.status==='review').length,
      build:jobs.filter(j=>j.ai_relevance==='build').length,adjacent:jobs.filter(j=>j.ai_relevance==='adjacent').length,
      intern:count('intern'),regular:count('regular'),unknown:count('unknown'),other_employment:count('contract')+count('part_time'),
      verified:jobs.filter(j=>Number.isInteger(j.http_status)&&j.http_status>=200&&j.http_status<300).length,liveness_checked:jobs.filter(j=>j.liveness).length,companies:new Set(jobs.map(j=>j.company_id)).size,
      today_new:jobs.filter(j=>localDay(j.first_seen)===today).length,week_new:jobs.filter(j=>localDay(j.first_seen)>=week).length,
      levels:groups(jobs,'authenticity_level'),roles:groups(jobs,'role_label'),top_companies:groups(jobs,'company').slice(0,8),
      trend:Object.keys(days).sort().map(day=>({day,new_jobs:days[day],closed_jobs:0}))});
  }
  global.JobCatalog={filters,select,summarize};
})(window);

(function(global){
  function json(fetcher,url,options={}){
    const controller=new AbortController();let timer;
    const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(Error('加载超时，请重试'));},options.timeout||15000);});
    const request=Promise.resolve().then(()=>fetcher(url,{cache:'no-store',signal:controller.signal})).then(r=>{if(!r.ok)throw Error('HTTP '+r.status);return r.json();});
    return Promise.race([request,timeout]).finally(()=>clearTimeout(timer));
  }
  function once(store,key,loader){
    if(!store.has(key)){const promise=Promise.resolve().then(loader).catch(error=>{store.delete(key);throw error;});store.set(key,promise);}
    return store.get(key);
  }
  global.PayloadLoader={json,once};
})(window);

(function(global){
  const types=['layoff','shrink','freeze','reorg'],statuses=['announced','implemented','reported','response_reported','pending','denied'];
  function select(events,p){
    const f={};['event_type','region','event_status','q','since','until'].forEach(k=>f[k]=(p.get(k)||'').trim());
    [['event_type',types],['event_status',statuses],['region',['domestic']]].forEach(([k,a])=>{if(f[k]&&!a.includes(f[k]))throw Error('unsupported '+k);});
    ['since','until'].forEach(k=>{if(f[k]&&(!/^\d{4}-\d{2}-\d{2}$/.test(f[k])||!Number.isFinite(Date.parse(f[k]))||new Date(f[k]).toISOString().slice(0,10)!==f[k]))throw Error('invalid event date');});
    if(f.since&&f.until&&f.since>f.until)throw Error('invalid event date range');
    return events.filter(e=>(!f.event_type||e.event_type===f.event_type)&&(!f.region||e.region===f.region)&&(!f.event_status||e.status===f.event_status)&&(!f.since||e.event_date>=f.since)&&(!f.until||e.event_date<=f.until)&&(!f.q||(e.company+' '+e.title+' '+(e.department||'')).toLowerCase().includes(f.q.toLowerCase()))).sort((a,b)=>b.event_date.localeCompare(a.event_date)||b.id.localeCompare(a.id));
  }
  function summarize(events){return {total:events.length,announced:events.filter(e=>e.event_type==='layoff'&&e.status==='announced').length,implemented:events.filter(e=>e.event_type==='layoff'&&e.status==='implemented').length,responses:events.filter(e=>['response_reported','denied'].includes(e.status)).length,pending:events.filter(e=>['reported','pending'].includes(e.status)).length,headcount_total:null,note:'按独立事件计数；不同地区、岗位/员工口径和重叠轮次不合并成人数总计。'};}
  global.EventCatalog={select,summarize};
})(window);
