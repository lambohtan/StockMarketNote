const state = { csrf: "", bootstrap: null, config: null, pool: null, page: "overview" };
const pageMeta = {
  overview: ["SYSTEM OVERVIEW", "运行中心"], pool: ["CANDIDATE UNIVERSE", "候选股票池"],
  analysis: ["RESEARCH LIBRARY", "分析师文章"], reddit: ["SOCIAL EVIDENCE", "Reddit 信息"],
  settings: ["CONTROL PLANE", "调度与设置"]
};
const taskIcon = { pool_update: "◇", reddit_update: "◎", analysis_update: "◌", publish_report: "⇥" };
const taskHelp = {
  pool_update: "多来源热度 → 确定性排名 → SQLite 快照",
  reddit_update: "股票池 → 原始帖子/评论缓存，不调用 LLM",
  analysis_update: "逐票串行 Lean 研究，默认关闭以控制消耗",
  publish_report: "汇总已存文章到本地报告，不发手机"
};
const taskParamLabels = {
  top: "处理数量", min_price: "最低股价", min_market_cap: "最低市值",
  window_hours: "时间窗口（小时）", comments_per_post: "每帖评论上限",
  min_interval_seconds: "请求最小间隔（秒）", timeout: "超时（秒）", retention_days: "保留天数",
  subreddits: "Subreddits", model: "快模型", deep: "判官模型", evidence: "证据模式",
  tool_rounds: "工具轮次上限", analysts: "分析师", rounds: "多空轮数", judge_samples: "判官采样",
  lang: "输出语言", analysis_limit: "报告文章上限"
};
const weekdayNames = ["一", "二", "三", "四", "五", "六", "日"];

async function api(path, options = {}) {
  const init = { cache: "no-store", ...options, headers: { ...(options.headers || {}) } };
  if (options.body) init.headers["Content-Type"] = "application/json";
  if (options.method === "POST") init.headers["X-StockWatch-Token"] = state.csrf;
  const response = await fetch(path, init);
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function h(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtDate(value) { if (!value) return "—"; const d = new Date(value); return Number.isNaN(d.valueOf()) ? h(value) : d.toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}); }
function duration(seconds) { seconds = Number(seconds || 0); if (seconds < 60) return `${seconds}秒`; const h = Math.floor(seconds/3600), m = Math.floor((seconds%3600)/60); return h ? `${h}小时 ${m}分` : `${m}分钟`; }
function statusText(status) { return ({queued:"等待中",running:"运行中",stopping:"正在停止",success:"完成",degraded:"降级完成",failed:"失败",cancelled:"已停止",interrupted:"异常中断"})[status] || "尚未运行"; }
function toast(message) { document.getElementById("toast").textContent = message; document.body.classList.add("show-toast"); clearTimeout(toast.timer); toast.timer=setTimeout(()=>document.body.classList.remove("show-toast"),2600); }

async function refreshOverview() {
  const data = await api("/api/v1/bootstrap"); state.bootstrap = data; state.csrf = data.csrf_token;
  const service = data.service, active = data.tasks.filter(t => t.active).length;
  const pill = document.getElementById("service-pill"); pill.className="service-pill ok"; pill.querySelector("span").textContent="本地服务正常";
  document.getElementById("hero-active").textContent=active;
  document.getElementById("hero-enabled").textContent=data.tasks.filter(t=>t.enabled).length;
  document.getElementById("hero-uptime").textContent=duration(service.uptime_seconds);
  renderTasks(data.tasks); renderRuns(data.runs);
}

function renderTasks(tasks) {
  const root=document.getElementById("task-grid"); root.classList.remove("skeleton-grid");
  root.innerHTML=tasks.map(task=>{
    const status=task.active ? (task.active.stopping?"stopping":"running") : task.latest?.status;
    const next=task.next_run_at ? `下次 ${fmtDate(task.next_run_at)}` : (task.enabled?"暂无未来日程":"定时已关闭");
    return `<article class="task-card"><div class="task-top"><span class="task-icon">${taskIcon[task.name]}</span><span class="status-tag ${h(status)}">${h(statusText(status))}</span></div><h3>${h(task.label)}</h3><div class="next">${h(taskHelp[task.name])}<br>${next}</div><div class="task-actions"><button class="button primary run-task" data-task="${task.name}" ${task.active?"disabled":""}>立即运行</button>${task.active?`<button class="button danger stop-task" data-task="${task.name}">停止</button>`:""}</div></article>`;
  }).join("");
  root.querySelectorAll(".run-task").forEach(b=>b.onclick=()=>runTask(b.dataset.task));
  root.querySelectorAll(".stop-task").forEach(b=>b.onclick=()=>stopTask(b.dataset.task));
}

function renderRuns(runs) {
  const root=document.getElementById("runs-table");
  if(!runs.length){root.className="data-table empty-state";root.textContent="还没有运行记录";return;}
  root.className="data-table"; root.innerHTML=`<table><thead><tr><th>任务</th><th>触发</th><th>开始</th><th>状态</th><th>摘要</th><th></th></tr></thead><tbody>${runs.map(r=>`<tr><td>${h(r.task_name)}</td><td>${r.trigger==="schedule"?"定时":"手动"}</td><td>${fmtDate(r.started_at)}</td><td><span class="status-tag ${h(r.status)}">${h(statusText(r.status))}</span></td><td>${h(r.summary||"—")}</td><td><button class="button view-log" data-run="${h(r.run_id)}">日志</button></td></tr>`).join("")}</tbody></table>`;
  root.querySelectorAll(".view-log").forEach(b=>b.onclick=()=>showLog(b.dataset.run));
}

async function runTask(name){try{await api(`/api/v1/tasks/${name}/run`,{method:"POST",body:"{}"});toast("任务已启动");await refreshOverview();}catch(e){toast(e.message);}}
async function stopTask(name){try{await api(`/api/v1/tasks/${name}/stop`,{method:"POST",body:"{}"});toast("正在停止任务");await refreshOverview();}catch(e){toast(e.message);}}
async function showLog(run){try{const data=await api(`/api/v1/log?run_id=${encodeURIComponent(run)}`);document.getElementById("log-title").textContent=`${data.run.task_name} · ${statusText(data.run.status)}`;document.getElementById("log-body").textContent=data.content||"日志暂无内容";document.getElementById("log-modal").hidden=false;}catch(e){toast(e.message);}}

async function loadPool(){const data=await api("/api/v1/pool?limit=200");state.pool=data;const summary=document.getElementById("pool-summary"), root=document.getElementById("pool-table");if(!data.available){summary.innerHTML="";root.className="data-table empty-state";root.textContent=data.message;return;}summary.innerHTML=[['分析日',data.analysis_date],['快照状态',data.status],['候选总数',data.total_candidates],['距今',`${data.age_hours}小时`]].map(x=>`<div class="summary-item"><span>${x[0]}</span><strong>${h(x[1])}</strong></div>`).join("");renderPoolRows(data.pool);}
function renderPoolRows(rows){const root=document.getElementById("pool-table");if(!rows.length){root.className="data-table empty-state";root.textContent="当前快照没有候选";return;}root.className="data-table";root.innerHTML=`<table><thead><tr><th>#</th><th>代码</th><th>公司</th><th>交易所</th><th>热度分</th><th>来源</th><th>提及</th><th>价格</th></tr></thead><tbody>${rows.map(r=>`<tr><td class="rank">${r.rank}</td><td class="ticker">${h(r.ticker)}</td><td>${h(r.name||"—")}</td><td>${h(r.exchange)}</td><td class="score">${Number(r.score).toFixed(3)}</td><td>${h(r.sources.join(" · "))}</td><td>${r.mentions??"—"}</td><td>${r.price==null?"—":Number(r.price).toFixed(2)}</td></tr>`).join("")}</tbody></table>`;}

async function loadAnalyses(){const data=await api("/api/v1/analyses?limit=100"),root=document.getElementById("analysis-list");if(!data.items.length){root.className="analysis-list empty-state";root.textContent="还没有保存的 Lean 分析文章";return;}root.className="analysis-list";root.innerHTML=data.items.map(item=>`<div class="analysis-group"><h3>${h(item.ticker)}</h3><p>${h(item.created_at)}</p><span class="recommendation">${h(item.recommendation||"未产出评级")}</span><div class="article-buttons">${item.articles.map(a=>`<button class="article-button" data-id="${h(a.id)}">${h(a.title)}</button>`).join("")}</div></div>`).join("");root.querySelectorAll(".article-button").forEach(b=>b.onclick=()=>loadArticle(b));}
async function loadArticle(button){try{document.querySelectorAll(".article-button.active").forEach(b=>b.classList.remove("active"));button.classList.add("active");const data=await api(`/api/v1/analysis?id=${encodeURIComponent(button.dataset.id)}`);document.getElementById("article-placeholder").hidden=true;document.getElementById("article-content").hidden=false;document.getElementById("article-title").textContent=data.title;document.getElementById("article-body").textContent=data.content;}catch(e){toast(e.message);}}

async function loadReddit(){const data=await api("/api/v1/reddit?limit=100"),root=document.getElementById("reddit-list");if(!data.items.length){root.className="reddit-list empty-state";root.textContent=data.message||"还没有 Reddit 快照";return;}root.className="reddit-list";root.innerHTML=data.items.map(item=>`<section class="reddit-group"><div class="reddit-head"><h3>${h(item.ticker)}</h3><span class="status-tag ${h(item.status)}">${h(item.status)}</span></div><div class="reddit-meta">${h(item.analysis_date)} · 帖子 ${item.posts_stored} · 原始评论 ${item.comments_stored} · ${fmtDate(item.fetched_at)}</div><div class="reddit-posts">${item.posts.map(p=>p.permalink?`<a class="reddit-post" href="${h(p.permalink)}" target="_blank" rel="noreferrer"><strong>${h(p.title||"（无标题）")}</strong><small>r/${h(p.subreddit)} · ${fmtDate(p.published_at)}</small></a>`:`<div class="reddit-post"><strong>${h(p.title||"（无标题）")}</strong><small>r/${h(p.subreddit)}</small></div>`).join("")||'<div class="muted">本快照没有可展示帖子</div>'}</div></section>`).join("");}

async function loadSettings(){state.config=await api("/api/v1/config");document.getElementById("scheduler-enabled").checked=state.config.scheduler.enabled;const root=document.getElementById("settings-tasks");root.innerHTML=Object.entries(state.config.tasks).map(([name,task])=>`<article class="settings-card" data-task="${name}"><div class="settings-card-head"><h3>${h(state.bootstrap?.tasks.find(t=>t.name===name)?.label||name)}</h3><label class="setting-toggle"><input class="task-enabled" type="checkbox" ${task.enabled?"checked":""}><i></i></label></div><div class="field-grid"><div class="field"><label>每日时间</label><input class="schedule-time" type="time" value="${h(task.schedule.time)}"></div><div class="field wide"><label>运行日</label><div class="weekday-row">${weekdayNames.map((d,i)=>`<label class="weekday"><input type="checkbox" value="${i}" ${task.schedule.days.includes(i)?"checked":""}><span>${d}</span></label>`).join("")}</div></div>${Object.entries(task.parameters).map(([key,value])=>parameterField(key,value)).join("")}</div></article>`).join("");}
function parameterField(key,value){const wide=["subreddits","analysts","lang"].includes(key)?" wide":"";if(key==="evidence")return `<div class="field${wide}"><label>${taskParamLabels[key]}</label><select data-param="${key}">${["prefetch","hybrid","tools"].map(x=>`<option ${value===x?"selected":""}>${x}</option>`).join("")}</select></div>`;const type=typeof value==="number"?"number":"text", step=Number.isInteger(value)?"1":"any";return `<div class="field${wide}"><label>${h(taskParamLabels[key]||key)}</label><input data-param="${key}" type="${type}" step="${step}" value="${h(value)}"></div>`;}
function readSettings(){const payload=JSON.parse(JSON.stringify(state.config));payload.scheduler.enabled=document.getElementById("scheduler-enabled").checked;document.querySelectorAll(".settings-card").forEach(card=>{const task=payload.tasks[card.dataset.task];task.enabled=card.querySelector(".task-enabled").checked;task.schedule.time=card.querySelector(".schedule-time").value;task.schedule.days=[...card.querySelectorAll(".weekday input:checked")].map(x=>Number(x.value));card.querySelectorAll("[data-param]").forEach(input=>{const old=task.parameters[input.dataset.param];task.parameters[input.dataset.param]=typeof old==="number"?Number(input.value):input.value;});});return payload;}

async function switchPage(name){state.page=name;document.querySelectorAll(".nav-item").forEach(x=>x.classList.toggle("active",x.dataset.page===name));document.querySelectorAll(".page").forEach(x=>x.classList.toggle("active",x.id===`page-${name}`));document.getElementById("eyebrow").textContent=pageMeta[name][0];document.getElementById("page-title").textContent=pageMeta[name][1];document.querySelector(".sidebar").classList.remove("open");try{if(name==="pool")await loadPool();if(name==="analysis")await loadAnalyses();if(name==="reddit")await loadReddit();if(name==="settings")await loadSettings();}catch(e){toast(e.message);}}

document.addEventListener("DOMContentLoaded",async()=>{
  document.querySelectorAll(".nav-item").forEach(b=>b.onclick=()=>switchPage(b.dataset.page));
  document.getElementById("mobile-menu").onclick=()=>document.querySelector(".sidebar").classList.toggle("open");
  document.getElementById("refresh").onclick=async()=>{try{await refreshOverview();await switchPage(state.page);toast("已刷新");}catch(e){toast(e.message);}};
  document.getElementById("close-log").onclick=()=>document.getElementById("log-modal").hidden=true;
  document.getElementById("log-modal").onclick=e=>{if(e.target.id==="log-modal")e.currentTarget.hidden=true;};
  document.getElementById("pool-search").oninput=e=>{if(!state.pool?.pool)return;const q=e.target.value.trim().toLowerCase();renderPoolRows(state.pool.pool.filter(r=>`${r.ticker} ${r.name}`.toLowerCase().includes(q)));};
  document.getElementById("settings-form").onsubmit=async e=>{e.preventDefault();try{state.config=await api("/api/v1/config",{method:"POST",body:JSON.stringify(readSettings())});document.getElementById("save-status").textContent="设置已保存";toast("设置已安全保存");await refreshOverview();}catch(err){document.getElementById("save-status").textContent=err.message;toast(err.message);}};
  try{await refreshOverview();setInterval(()=>refreshOverview().catch(()=>{}),15000);}catch(e){const pill=document.getElementById("service-pill");pill.className="service-pill error";pill.querySelector("span").textContent="服务不可用";toast(e.message);}
});
