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
const KIND = { identical: "同款", similar: "类似品", search_result: "搜索页" };
const STATUS = { queued: ["排队中", ""], running: ["处理中", "pill-info pill-running"], succeeded: ["已完成", "pill-ok"], failed: ["失败", "pill-bad"], canceled: ["已取消", ""] };
const STAGE = { fetch_etsy: "读取 Etsy", analyze: "分析产品", search_amazon: "搜索亚马逊", score: "评估", snapshots: "保存证据" };
const IMG_SRC = { mock_placeholder: "mock 占位图", product_image: "商品主图", screenshot: "网页截图", data_card: "数据摘要卡" };

let sb = null;
const S = { jobs: new Map(), analyses: new Map(), etsy: new Map(), snaps: new Map(), signed: new Map(), loaded: false };
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
      ingest(d.fixtures());
      const q = new URLSearchParams(location.search);
      if (q.has("open")) { const first = [...S.analyses.keys()][Number(q.get("open")) || 0]; first && openDetail(first); if (q.get("tab")) { drawerState.kind = q.get("tab"); renderSnaps(); } }
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
function ingest({ jobs, analyses, etsy, snaps, signed }) {
  S.jobs = new Map(jobs.map((r) => [r.id, r]));
  S.analyses = new Map(analyses.map((r) => [r.job_id, r]));
  S.etsy = new Map(etsy.map((r) => [r.job_id, r]));
  S.snaps = group(snaps);
  if (signed) Object.entries(signed).forEach(([k, v]) => S.signed.set(k, v));
  S.loaded = true; render();
}
async function loadAll() {
  const [j, a, e, s] = await Promise.all([
    sb.from("jobs").select("*").order("created_at", { ascending: false }).limit(200),
    sb.from("analyses").select("*"),
    sb.from("etsy_products").select("job_id,title,image_url,price,shop_name,listing_id"),
    sb.from("evidence_snapshots").select("*").order("id"),
  ]);
  await signPaths((s.data || []).map((r) => r.image_path));
  ingest({ jobs: j.data || [], analyses: a.data || [], etsy: e.data || [], snaps: s.data || [] });
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
}

// ---------- 渲染：列表 ----------
const pill = (text, cls = "", dot = true) => `<span class="pill ${cls}">${dot ? '<span class="dot"></span>' : ""}${esc(text)}</span>`;
const lvPill = (lv, invert) => { if (!lv) return ""; const good = invert ? lv === "high" : lv === "low"; const bad = invert ? lv === "low" : lv === "high"; return pill(LV[lv], good ? "pill-ok" : bad ? "pill-bad" : "pill-warn", false); };
const scoreCls = (n) => (n >= 60 ? "s-hi" : n >= 35 ? "s-mid" : "s-lo");

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
    <div><div class="sk" style="width:46px;height:46px;border-radius:12px"></div></div><div></div></div>`).join("");
}
function render() {
  const jobs = [...S.jobs.values()].sort((x, y) => y.created_at.localeCompare(x.created_at));
  const c = (st) => jobs.filter((j) => j.status === st).length;
  $("stats").innerHTML = S.loaded ? [pill(`全部 ${jobs.length}`, "", false), c("running") + c("queued") ? pill(`进行中 ${c("running") + c("queued")}`, "pill-info") : "", pill(`已完成 ${c("succeeded")}`, "pill-ok"), c("failed") ? pill(`失败 ${c("failed")}`, "pill-bad") : ""].join("") : "";
  if (!S.loaded) return ($("list").innerHTML = skeleton());
  if (!jobs.length) return ($("list").innerHTML = `<div class="card empty"><div class="ico">🔍</div><h3>还没有分析记录</h3><p class="muted">在上方粘贴 Etsy 商品链接，点击“开始分析”</p></div>`);
  $("list").innerHTML = jobs.map((j) => {
    const a = S.analyses.get(j.id) || {}, e = S.etsy.get(j.id) || {}, sn = S.snaps.get(j.id) || [];
    const cnt = (k) => sn.filter((s) => s.kind === k).length;
    const [stText, stCls] = STATUS[j.status] || [j.status, ""];
    const img = imgUrl(e.image_url);
    const n = a.opportunity_score;
    return `<article class="card row">
      ${img ? `<img class="thumb" src="${img}" data-zoom="${img}" data-cap="${esc(e.title)}" alt="" loading="lazy">` : `<div class="thumb ${j.status === "running" || j.status === "queued" ? "sk" : ""}"></div>`}
      <div class="c-main">
        <a class="title" href="${safeUrl(j.etsy_url)}" target="_blank" rel="noopener">${esc(e.title || `Etsy #${j.etsy_listing_id || ""}`)}</a>
        <div class="sub">${esc(a.product_summary || (j.status === "succeeded" ? "" : "等待分析…"))}</div>
        <div class="meta">${pill(stText + (j.status === "running" && STAGE[j.stage] ? " · " + STAGE[j.stage] : ""), stCls)}
          ${e.price ? `<span class="chip">$${esc(e.price)}</span>` : ""}${(a.keywords || []).map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div>
        ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
      </div>
      <div class="c-aud"><div class="label">目标人群</div><div class="aud">${audienceSummary(a) || '<span class="hint">—</span>'}</div>${audienceDetail(a)}</div>
      <div class="c-score">${n != null ? `<div class="score"><div class="score-num ${scoreCls(n)}">${esc(n)}</div>
        <div class="score-lv">竞争 ${lvPill(a.competition_level)}<br>需求 ${lvPill(a.demand_level, true)}</div></div>
        <div class="verdict">${esc(a.verdict)}</div>` : ""}</div>
      <div class="c-act actions">${sn.length ? `<div class="meta" style="margin:0;justify-content:flex-end">
          <span class="pill kpill k-identical">同款 ${cnt("identical")}</span><span class="pill kpill k-similar">类似 ${cnt("similar")}</span><span class="pill kpill k-search_result">搜索 ${cnt("search_result")}</span></div>` : ""}
        ${a.decision_trace ? `<button class="btn btn-ghost btn-sm" data-detail="${esc(j.id)}">决策过程 →</button>` : ""}</div>
    </article>`;
  }).join("");
}

// ---------- 渲染：抽屉 ----------
const fmt = (v) => Array.isArray(v) ? v.map((x) => esc(typeof x === "object" ? JSON.stringify(x) : x)).join("、") : v && typeof v === "object" ? esc(JSON.stringify(v)) : esc(v);
const kv = (o) => Object.entries(o || {}).map(([k, v]) => `<span><b>${esc(k)}</b> ${fmt(v)}</span>`).join("　");
let drawerState = null;

function openDetail(id) {
  const j = S.jobs.get(id), a = S.analyses.get(id) || {}, e = S.etsy.get(id) || {}, sn = S.snaps.get(id) || [];
  const trace = a.decision_trace || [], last = trace[trace.length - 1] || {};
  const steps = trace.map((s, i) => `<li class="step"><span class="num">${i + 1}</span><div class="step-card"><h4>${esc(s.title)}</h4>
    <dl class="io"><dt>输入</dt><dd>${kv(s.input)}</dd><dt>输出</dt><dd>${kv(s.output)}</dd>
    <dt>理由</dt><dd>${Array.isArray(s.reasoning) ? "<ul>" + s.reasoning.map((r) => `<li>${esc(r)}</li>`).join("") + "</ul>" : esc(s.reasoning)}</dd></dl></div></li>`).join("");
  const kinds = ["identical", "similar", "search_result"].filter((k) => sn.some((s) => s.kind === k));
  drawerState = { id, kind: kinds[0] };
  $("drawerTitle").textContent = e.title || j.etsy_url;
  $("drawerBody").innerHTML = `
    <div class="conclusion">${a.opportunity_score != null ? `<div class="score-num ${scoreCls(a.opportunity_score)}">${esc(a.opportunity_score)}</div>` : ""}
      <div><div><b>${esc(a.verdict)}</b>　竞争 ${lvPill(a.competition_level)} 需求 ${lvPill(a.demand_level, true)}</div><div class="why">为什么：${esc(last.reasoning)}</div></div></div>
    <h3>决策过程</h3><ol class="timeline">${steps}</ol>
    <h3>亚马逊证据快照 <span class="hint">共 ${sn.length} 个</span></h3>
    ${kinds.length ? `<div class="tabs" id="tabs">${kinds.map((k) => `<button class="tab" data-tab="${k}">${KIND[k]}<span class="n">${sn.filter((s) => s.kind === k).length}</span></button>`).join("")}</div><div id="snapGrid" class="grid"></div>` : '<p class="hint">无</p>'}
    <p class="note">亚马逊不公开销量，月销量为 BSR 推算的估计值；同款/类似品按标题相似度阈值划分。</p>`;
  renderSnaps();
  $("drawerMask").classList.remove("hidden"); $("drawer").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false");
  $("drawerBody").scrollTop = 0;
}
function renderSnaps() {
  if (!drawerState || !$("snapGrid")) return;
  document.querySelectorAll("#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === drawerState.kind));
  const list = (S.snaps.get(drawerState.id) || []).filter((s) => s.kind === drawerState.kind);
  $("snapGrid").innerHTML = list.map((s) => {
    const u = imgUrl(S.signed.get(s.image_path));
    return `<div class="snap k-${esc(s.kind)}">${u ? `<img src="${u}" data-zoom="${u}" data-cap="${esc(s.title)}" alt="" loading="lazy">` : '<div class="sk" style="aspect-ratio:16/10"></div>'}
      <div class="snap-b"><div class="snap-t">${esc(s.title)}</div>
        ${s.keyword ? `<div>搜索词 <span class="chip">${esc(s.keyword)}</span></div>` : ""}
        ${s.asin ? `<div class="facts"><span>ASIN <b>${esc(s.asin)}</b></span><span><b>$${esc(s.price)}</b></span><span>BSR <b>${esc(s.bsr ?? "-")}</b></span><span>评论 <b>${esc(s.review_count ?? "-")}</b></span>${s.rating ? `<span>★ <b>${esc(s.rating)}</b></span>` : ""}</div>` : ""}
        ${s.similarity != null ? `<div>相似度 <b>${esc(s.similarity)}</b></div>` : ""}
        <div class="reason">${esc(s.similarity_reason)}</div>
        <div class="meta" style="margin-top:4px"><span class="pill kpill k-${esc(s.kind)}">${KIND[s.kind]}</span><span class="hint">${esc(IMG_SRC[s.image_source] || s.image_source)}</span><span class="spacer"></span><a href="${safeUrl(s.source_url)}" target="_blank" rel="noopener">来源 ↗</a></div>
      </div></div>`;
  }).join("");
}
function closeDrawer() { $("drawer").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); $("drawerMask").classList.add("hidden"); drawerState = null; }

document.addEventListener("click", (ev) => {
  const t = ev.target.closest("[data-zoom],[data-detail],[data-tab]") || ev.target;
  if (t.dataset?.zoom) { $("lbImg").src = t.dataset.zoom; $("lbCap").textContent = t.dataset.cap || ""; $("lightbox").classList.remove("hidden"); return; }
  if (t.dataset?.detail) return openDetail(t.dataset.detail);
  if (t.dataset?.tab) { drawerState.kind = t.dataset.tab; return renderSnaps(); }
  if (t.closest?.("#lightbox")) $("lightbox").classList.add("hidden");
  if (t.id === "drawerMask" || t.closest?.("#drawerClose")) closeDrawer();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (!$("lightbox").classList.contains("hidden")) $("lightbox").classList.add("hidden"); else closeDrawer();
});

boot();
