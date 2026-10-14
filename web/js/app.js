import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";

const sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
const BUCKET = "snapshots";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");
const LISTING_RE = /^https?:\/\/(www\.)?etsy\.com\/([a-z]{2}(-[a-z]{2})?\/)?listing\/\d+/i;
const LV = { low: "低", medium: "中", high: "高" };
const KIND = { identical: "同款", similar: "类似品", search_result: "关键词搜索页" };
const STATUS = { queued: "排队中", running: "处理中", succeeded: "完成", failed: "失败", canceled: "已取消" };
const STAGE = { fetch_etsy: "读取 Etsy", analyze: "分析产品", search_amazon: "搜索亚马逊", score: "评估", snapshots: "保存证据" };

// ---------- 登录 ----------
$("btnLogin").onclick = async () => {
  const { error } = await sb.auth.signInWithOtp({ email: $("email").value, options: { emailRedirectTo: location.href.split("#")[0] } });
  $("loginMsg").textContent = error ? error.message : "已发送登录链接，请查收邮件";
};
$("btnLogout").onclick = () => sb.auth.signOut();
sb.auth.onAuthStateChange((_e, session) => {
  $("login").classList.toggle("hidden", !!session);
  $("app").classList.toggle("hidden", !session);
  if (session) { $("who").textContent = session.user.email; loadAll(); subscribe(); }
});

// ---------- 提交 ----------
$("btnSubmit").onclick = async () => {
  const urls = [...new Set($("urls").value.split(/\s+/).map((s) => s.trim()).filter(Boolean))];
  const ok = urls.filter((u) => LISTING_RE.test(u)), bad = urls.length - ok.length;
  if (!ok.length) return ($("submitMsg").textContent = "没有有效的 Etsy listing 链接");
  const { data: batch, error } = await sb.from("batches").insert({ name: new Date().toISOString() }).select().single();
  if (error) return ($("submitMsg").textContent = error.message);
  const { error: e2 } = await sb.from("jobs").insert(ok.map((u) => ({ batch_id: batch.id, etsy_url: u })));
  $("submitMsg").textContent = e2 ? e2.message : `已提交 ${ok.length} 条${bad ? `，忽略无效 ${bad} 条` : ""}`;
  $("urls").value = ""; loadAll();
};

// ---------- 数据 ----------
const S = { jobs: new Map(), analyses: new Map(), etsy: new Map(), snaps: new Map(), signed: new Map() };
const group = (rows) => { const m = new Map(); rows.forEach((r) => { if (!m.has(r.job_id)) m.set(r.job_id, []); m.get(r.job_id).push(r); }); return m; };

async function loadAll() {
  const [j, a, e, s] = await Promise.all([
    sb.from("jobs").select("*").order("created_at", { ascending: false }).limit(200),
    sb.from("analyses").select("*"),
    sb.from("etsy_products").select("job_id,title,image_url,price,shop_name,listing_id"),
    sb.from("evidence_snapshots").select("*").order("id"),
  ]);
  S.jobs = new Map((j.data || []).map((r) => [r.id, r]));
  S.analyses = new Map((a.data || []).map((r) => [r.job_id, r]));
  S.etsy = new Map((e.data || []).map((r) => [r.job_id, r]));
  S.snaps = group(s.data || []);
  await signPaths((s.data || []).map((r) => r.image_path));
  render();
}

async function signPaths(paths) {
  const need = [...new Set(paths.filter((p) => p && !S.signed.has(p)))];
  for (let i = 0; i < need.length; i += 100) {
    const { data } = await sb.storage.from(BUCKET).createSignedUrls(need.slice(i, i + 100), 3600);
    (data || []).forEach((d) => d.signedUrl && S.signed.set(d.path, d.signedUrl));
  }
}

let channel, reloadTimer;
const reload = () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(loadAll, 400); };
function subscribe() {
  if (channel) return;
  channel = sb.channel("rt")
    .on("postgres_changes", { event: "*", schema: "public", table: "jobs" }, (p) => { S.jobs.set(p.new.id, p.new); render(); if (p.new.status === "succeeded") reload(); })
    .on("postgres_changes", { event: "*", schema: "public", table: "analyses" }, reload)
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "evidence_snapshots" }, reload)
    .subscribe();
}

// ---------- 表格 ----------
function audienceSummary(a) {
  const ev = a.audience_evidence || {};
  if (!a.target_audience) return "";
  return ev.source === "reviews"
    ? `分析了 ${ev.reviews_analyzed} 条评论 → 人群：${a.target_audience}`
    : `无评论（0 条），依据商品${(ev.listing_fields_used || []).map((f) => ({ title: "标题", tags: "标签", description: "描述" }[f] || f)).join("/")} → 人群：${a.target_audience}`;
}

function audienceDetail(a) {
  const ev = a.audience_evidence || {};
  const sig = (ev.signals || []).map((s) => `<span class="tag">${esc(s.signal)} ×${esc(s.count)}</span>`).join("");
  const quotes = (ev.evidence || []).map((q) => `<blockquote>${q.rating ? "⭐" + esc(q.rating) + " " : ""}“${esc(q.quote)}”<br><small>→ ${esc(q.signal)}</small></blockquote>`).join("");
  return `<details><summary>查看依据</summary>
    <div class="kv"><b>来源：</b>${ev.source === "reviews" ? `Etsy 买家评论（${esc(ev.reviews_analyzed)} 条）` : "商品文字（无评论）"}</div>
    <div class="kv"><b>方法：</b>${esc(ev.method)}</div>
    <div class="kv"><b>信号：</b>${sig || "无"}</div>${quotes}</details>`;
}

function render() {
  const jobs = [...S.jobs.values()].sort((x, y) => y.created_at.localeCompare(x.created_at));
  $("rows").innerHTML = jobs.map((j) => {
    const a = S.analyses.get(j.id) || {}, e = S.etsy.get(j.id) || {}, sn = S.snaps.get(j.id) || [];
    const cnt = (k) => sn.filter((s) => s.kind === k).length;
    const st = j.status === "running" ? `${STATUS.running}<br><small>${esc(STAGE[j.stage] || j.stage)}</small>` : esc(STATUS[j.status] || j.status);
    const img = e.image_url ? `<img class="thumb" src="${safeUrl(e.image_url)}" data-zoom="${safeUrl(e.image_url)}" data-cap="${esc(e.title)}" alt="">` : `<div class="thumb"></div>`;
    return `<tr>
      <td>${img}</td>
      <td class="${esc(j.status)}">${st}${j.error ? `<br><small>${esc(j.error)}</small>` : ""}</td>
      <td><a href="${safeUrl(j.etsy_url)}" target="_blank" rel="noopener">${esc(e.title || j.etsy_listing_id || "Etsy 链接")}</a><br><small>${esc(a.product_summary)}</small></td>
      <td>${esc(audienceSummary(a))}${a.audience_evidence ? audienceDetail(a) : ""}</td>
      <td>${(a.keywords || []).map((k) => `<span class="tag">${esc(k)}</span>`).join("")}</td>
      <td>${a.competition_level ? `<span class="tag lv-${esc(a.competition_level)}">${LV[a.competition_level]}</span>` : ""}</td>
      <td>${a.demand_level ? `<span class="tag lv-${a.demand_level === "high" ? "low" : a.demand_level === "low" ? "high" : "medium"}">${LV[a.demand_level]}</span>` : ""}</td>
      <td><b>${esc(a.opportunity_score)}</b></td>
      <td>${esc(a.verdict)}</td>
      <td>${sn.length ? `<span class="tag k-identical">同款 ${cnt("identical")}</span><span class="tag k-similar">类似 ${cnt("similar")}</span><span class="tag k-search_result">搜索页 ${cnt("search_result")}</span>` : ""}</td>
      <td>${a.decision_trace ? `<button data-detail="${esc(j.id)}">决策过程</button>` : ""}</td></tr>`;
  }).join("");
}

// ---------- 详情弹窗 ----------
const fmt = (v) => Array.isArray(v) ? v.map((x) => esc(typeof x === "object" ? JSON.stringify(x) : x)).join("；") : typeof v === "object" && v ? JSON.stringify(v) : esc(v);
const kv = (o) => Object.entries(o || {}).map(([k, v]) => `<b>${esc(k)}：</b>${fmt(v)}`).join("　");

function openDetail(id) {
  const j = S.jobs.get(id), a = S.analyses.get(id) || {}, e = S.etsy.get(id) || {}, sn = S.snaps.get(id) || [];
  const steps = (a.decision_trace || []).map((s, i) => `<li><h4>${i + 1}. ${esc(s.title)}</h4>
     <div class="kv">输入　${kv(s.input)}</div><div class="kv">输出　${kv(s.output)}</div>
     <div class="kv">理由　${Array.isArray(s.reasoning) ? "<ul>" + s.reasoning.map((r) => `<li>${esc(r)}</li>`).join("") + "</ul>" : esc(s.reasoning)}</div></li>`).join("");
  const last = (a.decision_trace || []).slice(-1)[0] || {};
  const cards = ["identical", "similar", "search_result"].map((k) => {
    const list = sn.filter((s) => s.kind === k);
    if (!list.length) return "";
    return `<h3><span class="tag k-${k}">${KIND[k]}</span> ${list.length} 个</h3><div class="grid">${list.map((s) => {
      const u = S.signed.get(s.image_path) || "";
      return `<div class="card">${u ? `<img src="${esc(u)}" data-zoom="${esc(u)}" data-cap="${esc(s.title)}" alt="">` : ""}
        <div><b>${esc(s.title)}</b></div>
        ${s.keyword ? `<div>搜索词：${esc(s.keyword)}</div>` : ""}
        ${s.asin ? `<div>ASIN ${esc(s.asin)} · $${esc(s.price)} · BSR ${esc(s.bsr ?? "-")} · 评论 ${esc(s.review_count ?? "-")}${s.rating ? " · ⭐" + esc(s.rating) : ""}</div>` : ""}
        ${s.similarity != null ? `<div>相似度 ${esc(s.similarity)}</div>` : ""}
        <div><small>${esc(s.similarity_reason)}</small></div>
        <div><small>图片：${esc({ mock_placeholder: "mock 占位图", product_image: "商品主图", screenshot: "网页截图", data_card: "数据摘要卡" }[s.image_source] || s.image_source)}</small></div>
        <a href="${safeUrl(s.source_url)}" target="_blank" rel="noopener">打开来源</a></div>`;
    }).join("")}</div>`;
  }).join("");
  $("detailBody").innerHTML = `<h2>${esc(e.title || j.etsy_url)}</h2>
    <div class="conclusion"><b>结论：</b>${esc(a.verdict)}（机会分 ${esc(a.opportunity_score)}，竞争${LV[a.competition_level] || "-"}，需求${LV[a.demand_level] || "-"}）<br><small>为什么：${esc(last.reasoning)}</small></div>
    <h3>决策过程</h3><ol class="timeline">${steps}</ol>
    <h3>亚马逊证据快照</h3>${cards || "<p>无</p>"}
    <p><small>说明：亚马逊不公开销量，月销量为 BSR 推算的估计值。</small></p>`;
  $("detail").classList.remove("hidden");
}

document.addEventListener("click", (ev) => {
  const t = ev.target;
  if (t.dataset?.zoom) { $("lbImg").src = t.dataset.zoom; $("lbCap").textContent = t.dataset.cap || ""; $("lightbox").classList.remove("hidden"); return; }
  if (t.dataset?.detail) return openDetail(t.dataset.detail);
  if (t.id === "lightbox" || t.id === "lbImg") $("lightbox").classList.add("hidden");
  if (t.id === "detail" || t.id === "detailClose") $("detail").classList.add("hidden");
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { $("lightbox").classList.add("hidden"); $("detail").classList.add("hidden"); } });
