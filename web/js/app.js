import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";

// ?demo=1：纯前端示例数据，不登录、不连 Supabase，仅用于预览界面
const DEMO = new URLSearchParams(location.search).has("demo");
const BUCKET = "snapshots";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");
const imgUrl = (u) => (/^(https?:\/\/|data:image\/)/i.test(u || "") ? esc(u) : "");
const LISTING_RE = /^https?:\/\/(www\.)?etsy\.com\/([a-z]{2}(-[a-z]{2})?\/)?listing\/\d+/i;
const LV = { low: "低", medium: "中", high: "高" };
const HIDDEN_STEPS = new Set(["assess", "score", "conclusion"]);
const monthly = (s) => s.monthly_sold ?? s.est_monthly_sales ?? (s.raw || {}).monthly_sold ?? null;
const KIND = { identical: "同款", similar: "类似品", search_result: "搜索页" };
const STATUS = { queued: ["处理中 · 排队中", "pill-info"], running: ["处理中 · 进行中", "pill-info pill-running"], succeeded: ["已完成", "pill-ok"], failed: ["异常", "pill-bad"], paused: ["已暂停", "pill-warn"], canceled: ["已取消", ""] };
// 任务分类：待我处理 = 已完成但未查看；处理中 = queued+running
const CATS = [["all", "全部"], ["todo", "待我处理"], ["active", "处理中"], ["done", "已完成"], ["error", "异常"], ["paused", "已暂停"]];
const catOf = (j) => j.status === "failed" ? "error" : j.status === "paused" ? "paused" : j.status === "queued" || j.status === "running" ? "active"
  : j.status === "succeeded" ? (j.reviewed_at ? "done" : "todo") : "other";
const inCat = (j, c) => c === "all" || catOf(j) === c || (c === "done" && j.status === "succeeded");
const PRIO = { todo: 0, error: 1, active: 2, paused: 3, done: 4, other: 5 };
const F = { cat: "all", q: "" };
const ts = (v) => (v ? new Date(v).getTime() : null);
function dur(ms) {
  if (ms == null || ms < 0) return "—";
  const s = Math.round(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}小时${m}分` : m ? `${m}分${String(r).padStart(2, "0")}秒` : `${r}秒`;
}
// 排队等待 = 领取时刻(或现在) - 入队时刻；分析耗时 = 完成时刻(或现在) - 领取时刻
const waitMs = (j) => { const q = ts(j.queued_at || j.created_at); if (!q) return null;
  const end = j.status === "queued" ? Date.now() : ts(j.started_at); return end ? end - q : null; };
const runMs = (j) => { const st = ts(j.started_at); if (!st) return null;
  const end = j.status === "running" ? Date.now() : ts(j.finished_at); return end ? end - st : null; };
const STAGE = { fetch_etsy: "读取 Etsy", analyze: "分析产品", search_amazon: "搜索亚马逊", score: "评估", snapshots: "保存证据" };
const IMG_SRC = { mock_placeholder: "mock 占位图", product_image: "商品主图", screenshot: "网页截图", data_card: "数据摘要卡" };

let sb = null;
const S = { jobs: new Map(), analyses: new Map(), etsy: new Map(), snaps: new Map(), asins: new Map(), signed: new Map(), loaded: false };
const group = (rows) => { const m = new Map(); rows.forEach((r) => { if (!m.has(r.job_id)) m.set(r.job_id, []); m.get(r.job_id).push(r); }); return m; };

// ---------- 启动 ----------
async function boot() {
  render();
  if (DEMO) {
    const d = await import("./demo.js");
    const q0 = new URLSearchParams(location.search);
    showApp("demo@example.com");
    $("demoBadge").classList.remove("hidden");
    setTimeout(() => {
      const fx = d.fixtures(), shift = Date.now() - new Date(fx.now || Date.now()).getTime();
      fx.jobs.forEach((j) => ["created_at", "queued_at", "started_at", "finished_at", "reviewed_at"].forEach((k) => { if (j[k]) j[k] = new Date(new Date(j[k]).getTime() + shift).toISOString(); }));
      ingest(fx);
      const q = new URLSearchParams(location.search);
      if (q.has("open")) { const first = [...S.analyses.keys()][Number(q.get("open")) || 0]; first && openDetail(first); }
    }, q0.has("skeleton") ? 1e9 : 500); // 模拟加载，可看到骨架屏
    return;
  }
  const { createClient } = await import("https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm");
  sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
  sb.auth.onAuthStateChange((_e, session) => {
    if (session) { showApp(session.user.email); loadAll(); subscribe(); }
    else { $("login").classList.remove("hidden"); $("app").classList.add("hidden"); $("userBox").classList.add("hidden"); }
  });
}
function showApp(email) {
  $("login").classList.add("hidden"); $("app").classList.remove("hidden");
  $("userBox").classList.remove("hidden"); $("who").textContent = email;
}

// ---------- 登录 ----------
$("btnLogin").onclick = async () => {
  const email = $("email").value.trim();
  if (!email) return ($("loginMsg").textContent = "请输入邮箱");
  $("btnLogin").disabled = true;
  const { error } = await sb.auth.signInWithOtp({ email, options: { emailRedirectTo: location.href.split("#")[0] } });
  $("btnLogin").disabled = false;
  $("loginMsg").textContent = error ? error.message : "✓ 已发送登录链接，请查收邮件";
};
$("btnLogout").onclick = () => (DEMO ? (location.search = "") : sb.auth.signOut());

// ---------- 提交 ----------
const parseUrls = () => [...new Set($("urls").value.split(/\s+/).map((s) => s.trim()).filter(Boolean))];
$("urls").addEventListener("input", () => {
  const all = parseUrls(), ok = all.filter((u) => LISTING_RE.test(u)).length;
  $("urlCount").textContent = `${ok} 条有效链接${all.length - ok ? ` · ${all.length - ok} 条无效` : ""}`;
});
$("btnSubmit").onclick = async () => {
  const urls = parseUrls(), ok = urls.filter((u) => LISTING_RE.test(u)), bad = urls.length - ok.length;
  if (!ok.length) return ($("submitMsg").textContent = "没有有效的 Etsy listing 链接");
  if (DEMO) return ($("submitMsg").textContent = "演示模式不会提交");
  $("btnSubmit").disabled = true;
  const { data: batch, error } = await sb.from("batches").insert({ name: new Date().toISOString() }).select().single();
  let msg = error?.message;
  if (!error) {
    const { error: e2 } = await sb.from("jobs").insert(ok.map((u) => ({ batch_id: batch.id, etsy_url: u })));
    msg = e2 ? e2.message : `已提交 ${ok.length} 条${bad ? `，忽略无效 ${bad} 条` : ""}`;
    if (!e2) { $("urls").value = ""; $("urlCount").textContent = "0 条有效链接"; }
  }
  $("btnSubmit").disabled = false; $("submitMsg").textContent = msg; loadAll();
};

// ---------- 数据 ----------
function ingest({ jobs, analyses, etsy, snaps, signed, matches }) {
  S.jobs = new Map(jobs.map((r) => [r.id, r]));
  S.analyses = new Map(analyses.map((r) => [r.job_id, r]));
  S.etsy = new Map(etsy.map((r) => [r.job_id, r]));
  S.snaps = group(snaps);
  S.asins = new Map();
  [...snaps, ...(matches || [])].forEach((r) => { if (r.asin) { if (!S.asins.has(r.job_id)) S.asins.set(r.job_id, new Set()); S.asins.get(r.job_id).add(r.asin.toUpperCase()); } });
  if (signed) Object.entries(signed).forEach(([k, v]) => S.signed.set(k, v));
  S.loaded = true; render();
}
async function loadAll() {
  const [j, a, e, s, m] = await Promise.all([
    sb.from("jobs").select("*").order("created_at", { ascending: false }).limit(200),
    sb.from("analyses").select("*"),
    sb.from("etsy_products").select("job_id,title,image_url,price,shop_name,listing_id"),
    sb.from("evidence_snapshots").select("*").order("id"),
    sb.from("amazon_matches").select("job_id,asin"),
  ]);
  await signPaths((s.data || []).map((r) => r.image_path));
  ingest({ jobs: j.data || [], analyses: a.data || [], etsy: e.data || [], snaps: s.data || [], matches: m.data || [] });
}
async function signPaths(paths) {
  const need = [...new Set(paths.filter((p) => p && !S.signed.has(p)))];
  for (let i = 0; i < need.length; i += 100) {
    const { data } = await sb.storage.from(BUCKET).createSignedUrls(need.slice(i, i + 100), 3600);
    (data || []).forEach((d) => d.signedUrl && S.signed.set(d.path, d.signedUrl));
  }
}
let channel, timer;
const reload = () => { clearTimeout(timer); timer = setTimeout(loadAll, 400); };
function subscribe() {
  if (channel) return;
  channel = sb.channel("rt")
    .on("postgres_changes", { event: "*", schema: "public", table: "jobs" }, (p) => { S.jobs.set(p.new.id, p.new); render(); if (p.new.status === "succeeded") reload(); })
    .on("postgres_changes", { event: "*", schema: "public", table: "analyses" }, reload)
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "evidence_snapshots" }, reload)
    .subscribe();
  // 兜底：Realtime 掉线时，有处理中的任务就每 15 秒刷新一次状态
  setInterval(() => { if ([...S.jobs.values()].some((j) => j.status === "queued" || j.status === "running")) loadAll(); }, 15000);
}

// ---------- 任务操作 ----------
const RPC = { pause: ["pause_job", "paused"], resume: ["resume_job", "queued"], retry: ["retry_job", "queued"], reviewed: ["mark_job_reviewed", null] };
async function jobAction(id, act) {
  const j = S.jobs.get(id); if (!j) return;
  const [fn, st] = RPC[act], now = new Date().toISOString();
  const local = () => { if (st) j.status = st;
    if (act === "resume" || act === "retry") Object.assign(j, { queued_at: now, started_at: null, finished_at: null, error: null, stage: null });
    if (act === "retry") j.reviewed_at = null;
    if (act === "reviewed") j.reviewed_at = now; render(); };
  if (DEMO) return local();
  const { data, error } = await sb.rpc(fn, { p_job: id });
  if (error || data === false) { alert(error?.message || "操作未生效：任务状态已变化，已刷新"); return loadAll(); }
  local();
}
// 只更新计时文字，不整页重绘
setInterval(() => document.querySelectorAll("[data-tick]").forEach((el) => { const j = S.jobs.get(el.dataset.tick); if (j) el.textContent = timeText(j); }), 1000);
function timeText(j) {
  const w = waitMs(j), r = runMs(j), parts = [];
  if (w != null) parts.push(`排队等待 ${dur(w)}`);
  if (j.status === "running") parts.push(`已运行 ${dur(r)}`);
  else if (r != null && (j.status === "succeeded" || j.status === "failed")) parts.push(`耗时 ${dur(r)}`);
  return parts.join(" · ");
}

// ---------- 渲染：列表 ----------
const pill = (text, cls = "", dot = true) => `<span class="pill ${cls}">${dot ? '<span class="dot"></span>' : ""}${esc(text)}</span>`;
// const lvPill = (lv, invert) => { if (!lv) return ""; const good = invert ? lv === "high" : lv === "low"; const bad = invert ? lv === "low" : lv === "high"; return pill(LV[lv], good ? "pill-ok" : bad ? "pill-bad" : "pill-warn", false); };
// const scoreCls = (n) => (n >= 60 ? "s-hi" : n >= 35 ? "s-mid" : "s-lo");

function audienceSummary(a) {
  const ev = a.audience_evidence || {};
  if (!a.target_audience) return "";
  const total = ev.listing_reviews_total ?? ev.reviews_analyzed;
  return ev.source === "reviews"
    ? `<span class="muted">分析了 <b>${esc(ev.reviews_analyzed)}</b> 条评论（商品共 ${esc(total)} 条）${ev.shop_reviews_used ? `，含同店补充 ${esc(ev.shop_reviews_used)} 条` : ""} →</span> ${esc(a.target_audience)}`
    : `<span class="muted">无评论，依据商品${esc((ev.listing_fields_used || []).map((f) => ({ title: "标题", tags: "标签", description: "描述" }[f] || f)).join("/"))} →</span> ${esc(a.target_audience)}`;
}
function audienceDetail(a) {
  const ev = a.audience_evidence;
  if (!ev) return "";
  const sig = (ev.signals || []).map((s) => `<span class="chip">${esc(s.signal)} ×${esc(s.count)}</span>`).join("");
  const quotes = (ev.evidence || []).slice(0, 4).map((q) => `<div class="quote">${q.source === "shop" ? '<span class="chip">同店补充</span>' : ""}${q.rating ? "★" + esc(q.rating) + " · " : ""}“${esc(q.quote)}”<br><small>→ ${esc(q.signal)}</small></div>`).join("");
  return `<details class="evi"><summary>查看依据</summary><div class="evi-body">
    <div class="kv"><b>来源</b>　${ev.source === "reviews"
      ? `本商品评论 ${esc(ev.listing_reviews_used ?? ev.reviews_analyzed)} 条（共 ${esc(ev.listing_reviews_total ?? "?")} 条）${ev.shop_reviews_used ? ` + 同店其它商品评论 ${esc(ev.shop_reviews_used)} 条（补充，仅作参考）` : ""}`
      : "商品文字（0 条评论）"}</div>
    <div class="kv"><b>规则</b>　至少 ${esc(ev.min_reviews ?? 50)} 条，不足则全部${ev.shop_supplement_enabled ? "；允许同店补充" : "；未使用同店补充"}</div>
    <div class="kv"><b>方法</b>　${esc(ev.method)}</div>
    <div class="kv">${sig || "无明显信号"}</div>${quotes}</div></details>`;
}
function skeleton() {
  return Array.from({ length: 3 }, () => `<div class="card row"><div class="sk" style="width:76px;height:76px;border-radius:10px"></div>
    <div><div class="sk sk-line" style="width:80%"></div><div class="sk sk-line" style="width:50%"></div></div>
    <div><div class="sk sk-line" style="width:90%"></div><div class="sk sk-line" style="width:60%"></div></div>
    <div></div></div>`).join("");
}
function render() {
  const all = [...S.jobs.values()];
  const q = F.q.trim().toLowerCase();
  const match = (j) => { if (!q) return true; const e = S.etsy.get(j.id) || {};
    return String(j.etsy_listing_id || e.listing_id || "").includes(q) || (e.title || "").toLowerCase().includes(q)
      || j.etsy_url.toLowerCase().includes(q) || [...(S.asins.get(j.id) || [])].some((a) => a.toLowerCase().includes(q)); };
  const base = all.filter(match);
  $("chips").innerHTML = S.loaded ? CATS.map(([k, t]) => `<button class="fchip ${F.cat === k ? "active" : ""} fc-${k}" data-cat="${k}">${t} <span class="n">${base.filter((j) => inCat(j, k)).length}</span></button>`).join("") : "";
  const doneD = all.map(runMs).filter((v, i) => v != null && all[i].status === "succeeded");
  $("avgDur").textContent = doneD.length ? `平均分析耗时 ${dur(doneD.reduce((a, b) => a + b, 0) / doneD.length)}（${doneD.length} 个）` : "";
  if (!S.loaded) return ($("list").innerHTML = skeleton());
  if (!all.length) return ($("list").innerHTML = `<div class="card empty"><div class="ico">🔍</div><h3>还没有分析记录</h3><p class="muted">在上方粘贴 Etsy 商品链接，点击“开始分析”</p></div>`);
  const jobs = base.filter((j) => inCat(j, F.cat)).sort((x, y) => (PRIO[catOf(x)] - PRIO[catOf(y)]) || y.created_at.localeCompare(x.created_at));
  if (!jobs.length) return ($("list").innerHTML = `<div class="card empty"><div class="ico">🗂️</div><h3>没有符合条件的任务</h3><p class="muted">换个分类或<a href="#" data-clear="1">清除筛选</a></p></div>`);
  $("list").innerHTML = jobs.map((j) => {
    const a = S.analyses.get(j.id) || {}, e = S.etsy.get(j.id) || {}, sn = S.snaps.get(j.id) || [];
    const cnt = (k) => sn.filter((s) => s.kind === k).length;
    const [stText, stCls] = STATUS[j.status] || [j.status, ""];
    const img = imgUrl(e.image_url);
    return `<article class="card row">
      ${img ? `<img class="thumb" src="${img}" data-zoom="${img}" data-cap="${esc(e.title)}" alt="" loading="lazy">` : `<div class="thumb ${j.status === "running" || j.status === "queued" ? "sk" : ""}"></div>`}
      <div class="c-main">
        <a class="title" href="${safeUrl(j.etsy_url)}" target="_blank" rel="noopener">${esc(e.title || `Etsy #${j.etsy_listing_id || ""}`)}</a>
        <div class="sub">${esc(a.product_summary || (j.status === "succeeded" || j.status === "failed" ? "" : j.status === "paused" ? "已暂停，点击继续后重新排队" : "等待分析…"))}</div>
        <div class="meta">${pill(stText + (j.status === "running" && STAGE[j.stage] ? " · " + STAGE[j.stage] : ""), stCls)}
          ${e.price ? `<span class="chip">$${esc(e.price)}</span>` : ""}${ehuntChips(e)}${(a.keywords || []).map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div>
        ${j.error ? `<div class="err">${j.status === "queued" ? "上次尝试失败，将自动重试：" : ""}${esc(j.error)}</div>` : ""}
        <div class="timing"><span data-tick="${esc(j.id)}">${esc(timeText(j))}</span>${j.status === "succeeded" && !j.reviewed_at ? ' <span class="pill pill-warn">待我处理</span>' : ""}</div>
      </div>
      <div class="c-aud"><div class="label">目标人群</div><div class="aud">${audienceSummary(a) || '<span class="hint">—</span>'}</div>${audienceDetail(a)}</div>
      <div class="c-act actions">${sn.some((s) => s.kind !== "search_result") ? `<div class="meta" style="margin:0;justify-content:flex-end">
          <span class="pill kpill k-identical">同款 ${cnt("identical")}</span><span class="pill kpill k-similar">类似 ${cnt("similar")}</span></div>` : ""}
        ${a.decision_trace ? `<button class="btn btn-ghost btn-sm" data-detail="${esc(j.id)}">决策过程 →</button>` : ""}
        ${actBtns(j)}</div>
    </article>`;
  }).join("");
}

const actBtns = (j) => { const b = (act, t, cls = "btn-ghost") => `<button class="btn ${cls} btn-sm" data-act="${act}" data-id="${esc(j.id)}">${t}</button>`;
  return j.status === "queued" ? b("pause", "⏸ 暂停") : j.status === "paused" ? b("resume", "▶ 继续", "btn-primary") : j.status === "failed" || j.status === "canceled" ? b("retry", "↻ 重试", "btn-primary")
    : j.status === "succeeded" && !j.reviewed_at ? b("reviewed", "✓ 标记已查看") : ""; };
const ehuntChips = (e) => { const m = (e.raw || {}).ehunt; if (!m) return "";
  return [["周销量", m.sales_weekly], ["总销量", m.sales_total], ["收藏", m.favorites], ["周收藏", m.favorites_weekly], ["评论", m.reviews]]
    .filter(([, v]) => v != null).map(([k, v]) => `<span class="chip">${k} ${esc(v)}</span>`).join(""); };

// ---------- 渲染：抽屉 ----------
const fmt = (v) => Array.isArray(v) ? v.map((x) => esc(typeof x === "object" ? JSON.stringify(x) : x)).join("、") : v && typeof v === "object" ? esc(JSON.stringify(v)) : esc(v);
const kv = (o) => Object.entries(o || {}).map(([k, v]) => `<span><b>${esc(k)}</b> ${fmt(v)}</span>`).join("　");
let drawerState = null;

function openDetail(id) {
  const j = S.jobs.get(id), a = S.analyses.get(id) || {}, e = S.etsy.get(id) || {}, sn = S.snaps.get(id) || [];
  const trace = a.decision_trace || [];
  // 竞争/需求评估与由其得出的结论步骤不再展示（由用户根据匹配原始数据自行判断）
  const shown = trace.filter((s) => !HIDDEN_STEPS.has(s.step) && !/竞争|需求|机会分/.test(s.title || ""));
  const steps = shown.map((s, i) => `<li class="step"><span class="num">${i + 1}</span><div class="step-card"><h4>${esc(s.title)}</h4>
    <dl class="io"><dt>输出</dt><dd>${kv(s.output)}</dd></dl>
    <details class="step-more"><summary>查看输入与理由</summary><dl class="io"><dt>输入</dt><dd>${kv(s.input)}</dd>
    <dt>理由</dt><dd>${Array.isArray(s.reasoning) ? "<ul>" + s.reasoning.map((r) => `<li>${esc(r)}</li>`).join("") + "</ul>" : esc(s.reasoning)}</dd></dl></details></div></li>`).join("");
  const nI = sn.filter((s) => s.kind === "identical").length, nS = sn.filter((s) => s.kind === "similar").length;
  drawerState = { id };
  $("drawerTitle").textContent = e.title || j.etsy_url;
  $("drawerBody").innerHTML = `
    <div class="summary">
      ${a.product_summary ? `<div class="kv"><b>产品</b>　${esc(a.product_summary)}</div>` : ""}
      ${a.target_audience ? `<div class="kv"><b>人群</b>　${esc(a.target_audience)}</div>` : ""}
      ${(a.keywords || []).length ? `<div class="kv"><b>搜索词</b>　${a.keywords.map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div>` : ""}
      <div class="kv"><b>匹配</b>　<span class="pill kpill k-identical">同款 ${nI}</span> <span class="pill kpill k-similar">类似品 ${nS}</span></div></div>
    <h3>亚马逊同款 <span class="hint">${nI} 个</span></h3><div id="grid-identical" class="grid"></div>
    <h3>亚马逊类似品 <span class="hint">${nS} 个</span></h3><div id="grid-similar" class="grid"></div>
    <h3 id="traceHead">决策过程</h3><ol class="timeline">${steps}</ol>
    <p class="note">亚马逊不公开精确销量：有“过去一月购买”(monthlySold) 的商品用该值，其余为 BSR 推算的估计值；同款/类似品判定方法见“匹配同款/类似”步骤。</p>`;
  renderSnaps();
  if (j.status === "succeeded" && !j.reviewed_at) jobAction(id, "reviewed"); // 打开决策过程即视为已查看
  $("drawerMask").classList.remove("hidden"); $("drawer").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false");
  $("drawerBody").scrollTop = 0;
}
function renderSnaps() {
  if (!drawerState) return;
  ["identical", "similar"].forEach((k) => { const g = $("grid-" + k); if (!g) return;
  const list = (S.snaps.get(drawerState.id) || []).filter((s) => s.kind === k);
  g.innerHTML = list.length ? list.map((s) => {
    const u = imgUrl(S.signed.get(s.image_path));
    return `<div class="snap k-${esc(s.kind)}">${u ? `<img src="${u}" data-zoom="${u}" data-cap="${esc(s.title)}" alt="" loading="lazy">` : '<div class="sk" style="aspect-ratio:16/10"></div>'}
      <div class="snap-b"><div class="snap-t">${esc(s.title)}</div>
        ${s.keyword ? `<div>搜索词 <span class="chip">${esc(s.keyword)}</span></div>` : ""}
        ${s.asin ? `<div class="facts"><span>ASIN <b>${esc(s.asin)}</b></span><span><b>$${esc(s.price)}</b></span>${monthly(s) != null ? `<span>月销 <b>${esc(monthly(s))}</b></span>` : ""}<span>BSR <b>${esc(s.bsr ?? "-")}</b></span><span>评论 <b>${esc(s.review_count ?? "-")}</b></span>${s.rating ? `<span>★ <b>${esc(s.rating)}</b></span>` : ""}</div>` : ""}
        ${s.similarity != null ? `<div>相似度 <b>${esc(s.similarity)}</b></div>` : ""}
        <div class="reason">${esc(s.similarity_reason)}</div>
        <div class="meta" style="margin-top:4px"><span class="pill kpill k-${esc(s.kind)}">${KIND[s.kind]}</span><span class="hint">${esc(IMG_SRC[s.image_source] || s.image_source)}</span><span class="spacer"></span><a href="${safeUrl(s.source_url)}" target="_blank" rel="noopener">来源 ↗</a></div>
      </div></div>`;
  }).join("") : '<p class="hint">无</p>'; });
}
function closeDrawer() { $("drawer").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); $("drawerMask").classList.add("hidden"); drawerState = null; }

document.addEventListener("click", (ev) => {
  const t = ev.target.closest("[data-zoom],[data-detail],[data-tab],[data-cat],[data-act],[data-clear]") || ev.target;
  if (t.dataset?.cat) { F.cat = t.dataset.cat; return render(); }
  if (t.dataset?.act) return jobAction(t.dataset.id, t.dataset.act);
  if (t.dataset?.clear) { ev.preventDefault(); F.cat = "all"; F.q = ""; $("q").value = ""; return render(); }
  if (t.dataset?.zoom) { $("lbImg").src = t.dataset.zoom; $("lbCap").textContent = t.dataset.cap || ""; $("lightbox").classList.remove("hidden"); return; }
  if (t.dataset?.detail) return openDetail(t.dataset.detail);
  if (t.closest?.("#lightbox")) $("lightbox").classList.add("hidden");
  if (t.id === "drawerMask" || t.closest?.("#drawerClose")) closeDrawer();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (!$("lightbox").classList.contains("hidden")) $("lightbox").classList.add("hidden"); else closeDrawer();
});

$("q").addEventListener("input", () => { F.q = $("q").value; render(); });
$("btnClear").onclick = () => { F.cat = "all"; F.q = ""; $("q").value = ""; render(); };
boot();
