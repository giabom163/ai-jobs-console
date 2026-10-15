const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeLink = (value) => /^https?:\/\//i.test(value) ? esc(value) : "#";
let manualLimit = 3;
const dateLabel = (value) => { if (!value) return "时间未知"; const date = new Date(value); return Number.isNaN(+date) ? "时间未知" : date.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}); };
async function api(path, body) { const response = await fetch(path, {method: body ? "POST" : "GET", headers: body ? {"Content-Type":"application/json"} : {}, body: body ? JSON.stringify(body) : undefined}); const data = await response.json(); if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`); return data; }
function show(message, error=false) { const el=$("#notice"); el.textContent=message; el.style.color=error?"#ad4641":"#9a5a27"; }
async function load() {
  const data=await api("/api/data"), filter=$("#sentiment-filter").value;
  manualLimit = data.manual_limit;
  $("#refresh").textContent=`刷新来源（最多 ${manualLimit} 个）`;
  $("#feed-count").textContent=data.feeds.length; $("#item-count").textContent=data.items.length; $("#negative-count").textContent=data.items.filter(x=>x.sentiment==="偏负面").length;
  $("#feeds").innerHTML=data.feeds.length?data.feeds.map(f=>`<div class="feed"><b>${esc(f.name)}</b><small>${esc(f.item_count)} 条 · ${esc(f.url)}</small></div>`).join(""):`<div class="empty">还没有来源</div>`;
  $("#keyword-form [name=keywords]").value=data.keywords.join("，");
  const terms=data.keywords.map(x=>x.toLocaleLowerCase("zh-CN"));
  const rows=data.items.filter(x=>(filter==="all"||x.sentiment===filter)&&(!terms.length||terms.some(term=>(x.title+" "+x.summary).toLocaleLowerCase("zh-CN").includes(term))));
  $("#items").innerHTML=rows.length?rows.map(x=>`<article class="item"><div class="meta"><span>${esc(x.feed_name)}</span><span>·</span><time>${esc(dateLabel(x.published))}</time><span class="badge ${x.sentiment.replace("/待判","")}">${esc(x.sentiment)}</span></div><h3><a href="${safeLink(x.link)}" target="_blank" rel="noopener noreferrer">${esc(x.title)}</a></h3><p>${esc(x.summary.slice(0,420))}</p></article>`).join(""):`<div class="empty">还没有匹配条目。添加来源后，点击“刷新所有来源”。</div>`;
}
$("#feed-form").addEventListener("submit",async(e)=>{e.preventDefault();const form=new FormData(e.currentTarget);try{await api("/api/feeds",{name:form.get("name"),url:form.get("url")});e.currentTarget.reset();show("来源已添加");await load()}catch(err){show(err.message,true)}});
$("#keyword-form").addEventListener("submit",async(e)=>{e.preventDefault();const values=e.currentTarget.elements.keywords.value.split(/[，,\n\r]+/).map(x=>x.trim()).filter(Boolean);try{await api("/api/keywords",{keywords:values});show("关键词已保存");await load()}catch(err){show(err.message,true)}});
$("#refresh").addEventListener("click",async()=>{const button=$("#refresh");button.disabled=true;button.textContent="正在刷新…";show(`按来源编号处理前 ${manualLimit} 个来源，每源最多 ${manualLimit} 条`);try{const result=await api("/api/refresh",{});const summary=result.results.map(x=>x.error?`${x.name}：${x.error}`:`${x.name}：新增 ${x.added}`).join("；")||"没有配置来源";show(summary);$("#last-refresh").textContent=new Date().toLocaleTimeString("zh-CN",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit"});await load()}catch(err){show(err.message,true)}finally{button.disabled=false;button.textContent=`刷新来源（最多 ${manualLimit} 个）`}});
$("#sentiment-filter").addEventListener("change",()=>load().catch(err=>show(err.message,true)));
load().catch(err=>show(err.message,true));
