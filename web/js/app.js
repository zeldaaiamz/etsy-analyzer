import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";

// ?demo=1：纯前端示例数据，不登录、不连 Supabase，仅用于预览界面（&offline=1 / &quota=1 预览横幅）
const QS = new URLSearchParams(location.search);
const DEMO = QS.has("demo");
const BUCKET = "snapshots";
const MAX_SUBMIT = 500, EHUNT_LIMIT = 200, PAGE = 30, SIGN_TTL = 3600, OFFLINE_MS = 2 * 60 * 1000;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");
const imgUrl = (u) => (/^(https?:\/\/|data:image\/)/i.test(u || "") ? esc(u) : "");
const LV = { low: "低", medium: "中", high: "高" };
const HIDDEN_STEPS = new Set(["assess", "score", "conclusion"]);
const monthly = (s) => s?.monthly_sold ?? s?.est_monthly_sales ?? (s?.raw || {}).monthly_sold ?? (s?.raw || {}).monthlySold ?? null;
const KIND = { identical: "同款", similar: "类似品", search_result: "搜索页" };
const STATUS = { queued: ["处理中 · 排队中", "pill-info"], running: ["处理中 · 进行中", "pill-info pill-running"], succeeded: ["已完成", "pill-ok"], failed: ["异常", "pill-bad"], paused: ["已暂停", "pill-warn"], canceled: ["已取消", ""] };
// 任务分类：待我处理 = 已完成但未查看；处理中 = queued+running
const CATS = [["all", "全部"], ["todo", "待我处理"], ["active", "处理中"], ["done", "已完成"], ["error", "异常"], ["paused", "已暂停"]];
const catOf = (j) => j.status === "succeeded" && S.keep?.has(j.id) ? "todo" : j.status === "failed" ? "error" : j.status === "paused" ? "paused" : j.status === "queued" || j.status === "running" ? "active"
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
const waitMs = (j) => { const q = ts(j.queued_at || j.created_at); if (!q) return null;
  const end = j.status === "queued" ? Date.now() : ts(j.started_at); return end ? end - q : null; };
const runMs = (j) => { const st = ts(j.started_at); if (!st) return null;
  const end = j.status === "running" ? Date.now() : ts(j.finished_at); return end ? end - st : null; };
const STAGE = { fetch_etsy: "读取 Etsy", analyze: "分析产品", search_amazon: "搜索亚马逊", match: "匹配同款", score: "评估", snapshots: "保存证据" };
const IMG_SRC = { mock_placeholder: "mock 占位图", product_image: "商品主图", screenshot: "网页截图", data_card: "数据摘要卡" };

let sb = null;
const S = { jobs: new Map(), analyses: new Map(), etsy: new Map(), snaps: new Map(), asins: new Map(), matches: new Map(),
  signed: new Map(), lib: new Map(), sel: new Set(), loaded: false, statusLoaded: false, limit: PAGE, hasMore: false, flash: new Set(), keep: new Set(), libPending: new Set(), status: null, hb: [], usage: [] };
const group = (rows) => { const m = new Map(); rows.forEach((r) => { if (!m.has(r.job_id)) m.set(r.job_id, []); m.get(r.job_id).push(r); }); return m; };

const lidOf = (j) => j && Number(j.etsy_listing_id || S.etsy.get(j.id)?.listing_id || 0) || null;
const inLib = (j) => { const l = lidOf(j); return !!l && S.lib.has(l); };

// ---------- 小工具：toast / 复制 / CSV ----------
let toastT;
let toastActs = null;
function toast(msg, kind = "", acts = null, ms = 2600) {
  const t = $("toast"); toastActs = acts;
  t.innerHTML = `<span class="toast-m">${esc(msg)}</span>${(acts || []).map((a, i) => a.href ? `<a class="toast-a" href="${esc(a.href)}" data-tact="${i}">${esc(a.label)}</a>` : `<button class="toast-a" data-tact="${i}">${esc(a.label)}</button>`).join("")}`;
  t.className = `toast show ${kind}${acts ? " has-act" : ""}`;
  clearTimeout(toastT); toastT = setTimeout(() => { t.className = "toast"; toastActs = null; }, ms);
}
$("toast").addEventListener("click", (ev) => { const b = ev.target.closest("[data-tact]"); if (!b || !toastActs) return;
  const a = toastActs[Number(b.dataset.tact)]; clearTimeout(toastT); $("toast").className = "toast"; toastActs = null; a?.run?.(); });
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch { const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); }
  toast(`已复制：${text.length > 60 ? text.slice(0, 60) + "…" : text}`, "ok");
}
let modalRes = null;
let modalValidate = null;
function confirmDialog({ title, body, ok = "确定", danger = false, validate = null }) {
  modalValidate = validate; $("modalTitle").textContent = title; $("modalBody").innerHTML = body;
  $("modalOk").textContent = ok; $("modalOk").className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
  $("modal").classList.remove("hidden"); $("modalOk").focus();
  return new Promise((res) => { modalRes = res; });
}
const closeModal = (v) => { modalValidate = null; $("modal").classList.add("hidden"); const r = modalRes; modalRes = null; r && r(v); };
$("modalOk").onclick = () => { const v = modalValidate ? modalValidate() : true; if (v) closeModal(v); }; $("modalCancel").onclick = () => closeModal(false);
$("modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") closeModal(false); });
const csvCell = (v) => { let s = v == null ? "" : Array.isArray(v) ? v.join(" | ") : String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // 防 CSV 公式注入
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
function downloadCsv(name, header, rows) {
  const body = [header, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["\ufeff" + body], { type: "text/csv;charset=utf-8" }));
  a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast(`已导出 ${rows.length} 行`, "ok");
}

// ---------- 启动 / 登录状态 ----------
let currentUser = null;
async function boot() {
  render();
  if (DEMO) {
    const d = await import("./demo.js");
    showApp("demo@example.com");
    $("demoBadge").classList.remove("hidden");
    setTimeout(async () => {
      const fx = d.fixtures(), shift = Date.now() - new Date(fx.now || Date.now()).getTime();
      fx.jobs.forEach((j) => ["created_at", "queued_at", "started_at", "finished_at", "reviewed_at"].forEach((k) => { if (j[k]) j[k] = new Date(new Date(j[k]).getTime() + shift).toISOString(); }));
      if (QS.has("flash")) S.flash = new Set(fx.jobs.filter((j) => j.status === "queued" || j.status === "running").map((j) => j.id));
      S.status = QS.has("quota") ? { ehunt_paused_until: nextUtcMidnight(), ehunt_remaining: 0, ehunt_used_today: 200, ehunt_daily_limit: 200, ehunt_quota_at: new Date().toISOString(), reason: "EHunt 今日额度已用完，额度刷新后自动继续" }
        : { ehunt_remaining: Number(QS.get("rem") ?? 193), ehunt_used_today: 200 - Number(QS.get("rem") ?? 193), ehunt_daily_limit: 200, ehunt_quota_at: new Date().toISOString() };
      if (QS.has("quota")) { const base = fx.jobs.find((j) => j.status === "queued") || fx.jobs[0];
        for (let i = 0; i < (Number(QS.get("waiting")) || 5); i++) fx.jobs.unshift({ ...base, id: `demo-wait-${i}`, status: "queued", stage: null, started_at: null, finished_at: null, etsy_listing_id: 1800000000 + i,
          etsy_url: `https://www.etsy.com/listing/${1800000000 + i}/w`, error: i === 0 ? "EHunt 今日额度已用完，额度刷新后自动继续" : null, created_at: new Date(Date.now() - i * 60e3).toISOString(), queued_at: new Date(Date.now() - i * 60e3).toISOString() }); }
      S.hb = [{ worker_id: "demo", last_seen: new Date(Date.now() - (QS.has("offline") ? 9 * 60e3 : 5e3)).toISOString() }];
      S.statusLoaded = true;
      S.usage = [{ provider: "ehunt", calls: 7, units: 7, cache_hits: 3 }, { provider: "keepamore", calls: 36, units: 270, cache_hits: 12 }, { provider: "deepseek", calls: 14, units: 41210, cache_hits: 0 }];
      const KW = [["monster boo basket tag", "halloween basket tag", "personalized boo basket"], ["personalized teacher tote bag", "teacher appreciation gift"], ["minimalist line art print", "abstract face wall art", "boho bedroom decor"]];
      fx.analyses.forEach((a, i) => { if (KW[i]) a.keywords = KW[i]; });
      fx.snaps.forEach((s, i) => { if (s.asin) { const ms = i % 3 !== 2; s.raw = { ...(s.raw || {}), sales_source: ms ? "monthlySold" : "bsr_estimate" }; s.monthly_sold = ms ? [300, 1000, 50, 200][i % 4] : null; s.est_monthly_sales = ms ? s.monthly_sold : 120 + (s.bsr % 400); } });
      if (QS.has("q")) { F.q = QS.get("q"); $("q").value = F.q; }
      const { libraryFixtures } = await import("./demo-library.js");
      S.lib = new Map(libraryFixtures(fx).map((r) => [Number(r.etsy_listing_id), r]));
      if (QS.has("failed")) fx.jobs.push({ ...fx.jobs.find((j) => j.status === "failed"), id: "demo-failed-2", etsy_url: "https://www.etsy.com/listing/1700000009/y", etsy_listing_id: 1700000009, error: "亚马逊搜索超时（Keepamore 504）" });
      ingest(fx);
      if (QS.has("sel")) { [...S.jobs.values()].filter((j) => j.status !== "running").slice(0, Number(QS.get("sel")) || 3).forEach((j) => S.sel.add(j.id)); render(); }
      if (QS.has("libstage")) { L.stage = QS.get("libstage"); renderLibrary(); }
      if (QS.has("libsel")) { L.sel = new Set([...S.lib.values()].filter((r) => (r.stage || "candidate") === L.stage).slice(0, Number(QS.get("libsel")) || 2).map((r) => r.id)); renderLibrary(); }
      if (QS.has("libreason")) { moveStage([[...S.lib.values()].find((r) => (r.stage || "candidate") === "candidate")?.id], "rejected");
        setTimeout(() => { ["ip_risk", "other"].forEach((v) => { const c = document.querySelector(`input[name="rj"][value="${v}"]`); c.checked = true; c.dispatchEvent(new Event("change", { bubbles: true })); }); $("modalOk").click(); }, 80); }
      if (QS.has("confirm")) bulk(QS.get("confirm") || "delete");
      if (QS.has("submitted")) showSubmitResult({ ok: true, text: "已提交 3 条，已开始排队分析（新任务已高亮）" });
      if (QS.has("reviewed")) { const id = [...S.analyses.keys()][1]; openDetail(id); setTimeout(closeDrawer, 50); }
      if (QS.has("libmove")) setTimeout(() => moveStage([[...S.lib.values()].find((r) => stageOf(r) === "candidate")?.id], "active"), 50);
      if (QS.has("rjempty")) { moveStage([[...S.lib.values()].find((r) => stageOf(r) === "candidate")?.id], "rejected"); setTimeout(() => $("modalOk").click(), 80); }
      if (QS.has("delall")) { const ids = fx.jobs.filter((j) => j.status === "succeeded").map((j) => j.id); deleteJobs(ids); }
      if (QS.has("open")) { const first = [...S.analyses.keys()][Number(QS.get("open")) || 0]; first && openDetail(first); }
      if (QS.has("paste")) { const pv = QS.get("paste"); $("urls").value = /^\d+$/.test(pv) ? Array.from({ length: Number(pv) }, (_, i) => `https://www.etsy.com/listing/${1900000000 + i}/item-${i}`).join("\n") : pv.split("|").join("\n"); onUrlsInput(); }
      if (QS.has("submit")) $("btnSubmit").click();
      if (QS.has("addlib")) document.querySelector('[data-act="lib"]')?.click();
    }, QS.has("skeleton") ? 1e9 : 300);
    return;
  }
  // supabase-js 固定版本 + SRI，由 index.html 的 <script> 加载（全局 supabase）
  sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
  sb.auth.onAuthStateChange((event, session) => {
    const uid = session?.user?.id || null;
    if (uid && uid === currentUser) return; // TOKEN_REFRESHED 等：同一用户不重建
    teardown();
    currentUser = uid;
    if (uid) { showApp(session.user.email); setTimeout(() => { loadAll(); loadStatus(); subscribe(); }, 0); }
    else { S.jobs = new Map(); S.loaded = false; S.signed.clear(); closeDrawer();
      $("login").classList.remove("hidden"); $("app").classList.add("hidden"); $("libraryView").classList.add("hidden"); $("nav").classList.add("hidden"); $("userBox").classList.add("hidden"); }
  });
}
function showApp(email) {
  $("login").classList.add("hidden"); $("nav").classList.remove("hidden"); route();
  $("userBox").classList.remove("hidden"); $("who").textContent = email;
}
const nextUtcMidnight = () => { const d = new Date(); d.setUTCHours(24, 0, 0, 0); return d.toISOString(); };

$("btnLogin").onclick = async () => {
  const email = $("email").value.trim();
  if (!email) return ($("loginMsg").textContent = "请输入邮箱");
  $("btnLogin").disabled = true;
  const { error } = await sb.auth.signInWithOtp({ email, options: { emailRedirectTo: location.href.split("#")[0] } });
  $("btnLogin").disabled = false;
  $("loginMsg").textContent = error ? error.message : "✓ 已发送登录链接，请查收邮件";
};
$("btnLogout").onclick = () => (DEMO ? (location.search = "") : sb.auth.signOut());

// ---------- 链接校验：逐行给出原因 ----------
function checkLine(raw) {
  let u = raw.trim();
  if (!/^https?:\/\//i.test(u)) { if (/^(www\.|m\.)?etsy\.(com|me)\//i.test(u)) u = "https://" + u; else return { raw, reason: "不是链接（应以 https:// 开头）" }; }
  let p; try { p = new URL(u); } catch { return { raw, reason: "链接格式错误" }; }
  const host = p.hostname.toLowerCase();
  if (host === "etsy.me" || host === "www.etsy.me") return /^\/[A-Za-z0-9]+\/?$/.test(p.pathname) ? { raw, url: `https://etsy.me${p.pathname.replace(/\/$/, "")}`, id: null, short: true } : { raw, reason: "etsy.me 短链不完整" };
  if (!/(^|\.)etsy\.com$/.test(host)) return { raw, reason: "不是 Etsy 链接" };
  const m = p.pathname.match(/^\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?listing\/(\d+)(\/[^/?#]*)?/i);
  if (!m) return { raw, reason: /^\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?shop\//i.test(p.pathname) || host.split(".").length > 2 && !/^(www|m)\./.test(host) ? "店铺链接，不是商品(listing)链接"
    : /\/search/.test(p.pathname) ? "搜索页链接，请打开具体商品后复制地址" : "不是商品(listing)链接（需包含 /listing/数字）" };
  // 统一为 www.etsy.com（m.etsy.com、店铺子域名都转成标准地址，保留 slug 便于识别）
  return { raw, url: `https://www.etsy.com/listing/${m[1]}${m[2] || ""}`, id: Number(m[1]) };
}
function parseInput() {
  const lines = $("urls").value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
  const seen = new Set(), ok = [], bad = []; let dup = 0;
  for (const l of lines) { const r = checkLine(l); if (r.reason) { bad.push(r); continue; }
    const k = r.id ? `id:${r.id}` : r.url; if (seen.has(k)) { dup++; continue; } seen.add(k); ok.push(r); }
  return { ok, bad, dup };
}
function onUrlsInput() {
  const { ok, bad, dup } = parseInput(), short = ok.filter((r) => r.short).length;
  const pc = $("pasteCount"); pc.textContent = `${ok.length} / ${MAX_SUBMIT}`; pc.classList.toggle("over", ok.length > MAX_SUBMIT);
  $("urlCount").innerHTML = `<b>${ok.length}</b> 条有效${short ? `（含 ${short} 条短链，将自动展开）` : ""}${bad.length ? ` · <span class="bad">${bad.length} 条无效</span>` : ""}${dup ? ` · 重复 ${dup} 条已合并` : ""}${ok.length > MAX_SUBMIT ? ` · <span class="bad">超过单次上限 ${MAX_SUBMIT} 条</span>` : ""}`;
  $("urlIssues").innerHTML = bad.length ? `<ul>${bad.slice(0, 8).map((b) => `<li><code>${esc(b.raw.length > 70 ? b.raw.slice(0, 70) + "…" : b.raw)}</code><span>${esc(b.reason)}</span></li>`).join("")}${bad.length > 8 ? `<li class="hint">…另有 ${bad.length - 8} 条</li>` : ""}</ul>` : "";
  $("dupeBox").innerHTML = ""; pending = null;
}
$("urls").addEventListener("input", onUrlsInput);

// ---------- 提交：上限 / 去重提示 / 强反馈 ----------
let pending = null;
function showSubmitResult({ ok, text }) {
  const el = $("submitMsg"); el.className = `submit-msg ${ok ? "ok" : "bad"}`; el.textContent = text;
}
$("btnSubmit").onclick = async () => {
  const { ok, bad } = parseInput();
  if (!ok.length) return showSubmitResult({ ok: false, text: bad.length ? "没有有效的 Etsy 商品链接，请看下方每行原因" : "请先粘贴 Etsy 商品链接" });
  if (ok.length > MAX_SUBMIT) return showSubmitResult({ ok: false, text: `单次最多 ${MAX_SUBMIT} 条，当前 ${ok.length} 条，请分批提交` });
  const ids = ok.map((r) => r.id).filter(Boolean);
  let dupes = [];
  if (DEMO) { // 演示：用示例任务模拟“已分析过”提示，不会提交
    const seen = new Set(); dupes = [...S.jobs.values()].filter((j) => ids.includes(Number(j.etsy_listing_id)) && j.status === "succeeded" && !seen.has(j.etsy_listing_id) && seen.add(j.etsy_listing_id));
    dupes = withLib(dupes, ids);
    if (dupes.length) { pending = { ok, bad, dupes }; return renderDupes(); }
    return showSubmitResult({ ok: true, text: `演示模式不会提交。${quotaPlan(ok.length)}` });
  }
  setBusy(true, "检查重复…");
  if (ids.length) {
    const { data } = await sb.from("jobs").select("id,etsy_listing_id,status,finished_at,created_at").in("etsy_listing_id", ids)
      .in("status", ["succeeded", "queued", "running", "paused"]).order("created_at", { ascending: false });
    const seen = new Set(); dupes = (data || []).filter((d) => !seen.has(d.etsy_listing_id) && seen.add(d.etsy_listing_id));
    const { data: lb } = await sb.from("product_library").select("*").in("etsy_listing_id", ids); // 产品库可能有任务已删除的条目
    (lb || []).forEach((r) => S.lib.set(Number(r.etsy_listing_id), r));
    dupes = withLib(dupes, ids);
  }
  setBusy(false);
  if (dupes.length) { pending = { ok, bad, dupes }; return renderDupes(); }
  submitJobs(ok, bad.length);
};
// 已在产品库的 listing 优先标注“已在产品库（阶段）”，即使对应任务已删除
function withLib(dupes, ids) {
  const out = dupes.map((d) => { const r = S.lib.get(Number(d.etsy_listing_id)); return r ? { ...d, lib: r } : d; }), have = new Set(out.map((d) => Number(d.etsy_listing_id)));
  ids.forEach((id) => { const r = S.lib.get(id); if (r && !have.has(id)) { have.add(id); out.push({ id: r.job_id, etsy_listing_id: id, lib: r }); } });
  return out.sort((a, b) => !!b.lib - !!a.lib);
}
function renderDupes() {
  const { ok, dupes } = pending, dupIds = new Set(dupes.map((d) => Number(d.etsy_listing_id))), rest = ok.filter((r) => !dupIds.has(r.id)).length;
  const nLib = dupes.filter((d) => d.lib).length, nJob = dupes.length - nLib;
  const head = [nLib ? `${nLib} 条已在产品库` : "", nJob ? `${nJob} 条已分析过或正在分析` : ""].filter(Boolean).join("，");
  $("dupeBox").innerHTML = `<div class="dupe"><b>${head}</b>（默认跳过；重复提交会再次消耗 EHunt/Keepamore 额度）
    <ul>${dupes.slice(0, 6).map((d) => d.lib
      ? `<li>#${esc(d.etsy_listing_id)}${d.lib.title ? ` · ${esc(d.lib.title.length > 40 ? d.lib.title.slice(0, 40) + "…" : d.lib.title)}` : ""} · <span class="pill pill-lib">已在产品库（${esc(STAGE_NAME[stageOf(d.lib)])}）</span>
        <a href="#/library" data-libgo="${esc(stageOf(d.lib))}">在产品库查看</a></li>`
      : `<li>#${esc(d.etsy_listing_id)} · ${esc((STATUS[d.status] || [d.status])[0])}${d.finished_at ? ` · ${esc(new Date(d.finished_at).toLocaleString())}` : ""}
      <a href="#" data-view="${esc(d.id)}" data-lid="${esc(d.etsy_listing_id)}">查看已有结果</a></li>`).join("")}${dupes.length > 6 ? `<li class="hint">…另有 ${dupes.length - 6} 条</li>` : ""}</ul>
    <div class="dupe-act">${rest ? `<button class="btn btn-primary btn-sm" data-dupe="skip">跳过重复，提交其余 ${rest} 条</button>` : `<button class="btn btn-primary btn-sm" data-dupe="cancel">全部跳过（无需提交）</button>`}
      <button class="btn btn-ghost btn-sm" data-dupe="force">全部提交（强制重跑 ${ok.length} 条）</button>
      ${rest ? '<button class="btn btn-ghost btn-sm" data-dupe="cancel">取消</button>' : ""}</div></div>`;
  $("dupeBox").querySelector('[data-dupe="skip"],[data-dupe="cancel"]')?.focus();
}
function setBusy(b, text = "提交中…") { const btn = $("btnSubmit"); btn.disabled = b; btn.innerHTML = b ? `<span class="spin"></span>${text}` : "开始分析 →"; }
async function submitJobs(list, badN = 0) {
  setBusy(true); $("dupeBox").innerHTML = ""; pending = null;
  const { data: batch, error } = await sb.from("batches").insert({ name: new Date().toISOString() }).select().single();
  let res = { ok: false, text: error?.message };
  if (!error) {
    const { data: rows, error: e2 } = await sb.from("jobs").insert(list.map((r) => ({ batch_id: batch.id, etsy_url: r.url, ...(r.id ? { etsy_listing_id: r.id } : {}) }))).select("id");
    if (e2) res = { ok: false, text: `提交失败：${e2.message}` };
    else {
      res = { ok: true, text: `✓ 已提交 ${list.length} 条，已开始排队分析${badN ? `；忽略无效 ${badN} 条` : ""}。${quotaPlan(list.length, list.length)}` };
      $("urls").value = ""; onUrlsInput();
      S.flash = new Set((rows || []).map((r) => r.id)); F.cat = "all"; F.q = ""; $("q").value = "";
      setTimeout(() => S.flash.clear(), 8000);
    }
  }
  setBusy(false); showSubmitResult(res);
  await loadAll();
  if (res.ok) document.querySelector(".row.flash")?.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------- 数据（分页：只取当前页任务的相关行） ----------
function ingest({ jobs, analyses, etsy, snaps, signed, matches }) {
  S.jobs = new Map(jobs.map((r) => [r.id, r]));
  S.analyses = new Map(analyses.map((r) => [r.job_id, r]));
  S.etsy = new Map(etsy.map((r) => [r.job_id, r]));
  S.snaps = group(snaps);
  S.asins = new Map();
  [...snaps, ...(matches || [])].forEach((r) => { if (r.asin) { if (!S.asins.has(r.job_id)) S.asins.set(r.job_id, new Set()); S.asins.get(r.job_id).add(r.asin.toUpperCase()); } });
  if (signed) Object.entries(signed).forEach(([k, v]) => S.signed.set(k, { url: v, exp: Infinity }));
  S.loaded = true; [...S.sel].forEach((id) => S.jobs.has(id) || S.sel.delete(id)); render(); renderBanners();
  if (location.hash.startsWith("#/library")) renderLibrary();
  if (drawerState) renderSnaps();
}
let loading = false;
async function loadAll() {
  if (!sb || !currentUser || loading) return; loading = true;
  try {
    // 任务列表不含已在产品库的 listing：优先用视图 task_jobs（012），未部署时退回 jobs + not.in（产品库先取）
    const lb = await sb.from("product_library").select("*").order("added_at", { ascending: false });
    if (!lb.error) S.lib = new Map((lb.data || []).map((r) => [Number(r.etsy_listing_id), r]));
    const libIds = [...S.lib.keys()];
    const orphan = [...S.lib.values()].filter((r) => !r.job_id).map((r) => Number(r.etsy_listing_id));
    if (orphan.length) { const { data: pj } = await sb.from("jobs").select("etsy_listing_id").in("etsy_listing_id", orphan).in("status", ["queued", "running"]);
      S.libPending = new Set((pj || []).map((x) => Number(x.etsy_listing_id))); } else S.libPending = new Set();
    let j = S.noView ? { error: true } : await sb.from("task_jobs").select("*").order("created_at", { ascending: false }).limit(S.limit + 1);
    if (j.error) { S.noView = true; let q = sb.from("jobs").select("*");
      if (libIds.length) q = q.or(`etsy_listing_id.is.null,etsy_listing_id.not.in.(${libIds.join(",")})`);
      j = await q.order("created_at", { ascending: false }).limit(S.limit + 1); }
    const jobs = j.data || []; S.hasMore = jobs.length > S.limit; jobs.length = Math.min(jobs.length, S.limit);
    const ids = jobs.map((x) => x.id), inIds = (q) => (ids.length ? q.in("job_id", ids) : q.limit(0));
    const [a, e, s, m] = await Promise.all([
      inIds(sb.from("analyses").select("*")),
      inIds(sb.from("etsy_products").select("job_id,title,image_url,price,shop_name,listing_id,raw")),
      inIds(sb.from("evidence_snapshots").select("*").order("id")),
      inIds(sb.from("amazon_matches").select("job_id,asin")),
    ]);
    ingest({ jobs, analyses: a.data || [], etsy: e.data || [], snaps: s.data || [], matches: m.data || [] });
  } finally { loading = false; }
}
// 签名 URL 记录过期时间：剩余 <5 分钟或加载失败时重签
async function signPaths(paths, force = false) {
  if (DEMO || !sb) return false;
  const now = Date.now();
  const need = [...new Set(paths.filter((p) => p && (force || !S.signed.has(p) || S.signed.get(p).exp - now < 5 * 60e3)))];
  for (let i = 0; i < need.length; i += 100) {
    const { data } = await sb.storage.from(BUCKET).createSignedUrls(need.slice(i, i + 100), SIGN_TTL);
    (data || []).forEach((d) => d.signedUrl && S.signed.set(d.path, { url: d.signedUrl, exp: now + SIGN_TTL * 1000 }));
  }
  return need.length > 0;
}
const signedUrl = (p) => S.signed.get(p)?.url;
const retried = new Map();
document.addEventListener("error", async (ev) => {
  const p = ev.target?.dataset?.path; if (!p || DEMO) return;
  if (Date.now() - (retried.get(p) || 0) < 60e3) return; // 同一图片 1 分钟内只重签一次
  retried.set(p, Date.now());
  if (await signPaths([p], true)) renderSnaps();
}, true);

async function loadStatus() {
  if (!sb || !currentUser) return;
  const day = new Date().toISOString().slice(0, 10);
  const [st, hb, us] = await Promise.all([
    sb.from("service_status").select("*").eq("id", 1).maybeSingle(),
    sb.from("worker_heartbeat").select("worker_id,last_seen,current_job"),
    sb.from("provider_usage_daily").select("*").eq("day", day),
  ]);
  S.status = st.data || null; S.hb = hb.data || []; S.usage = us.data || []; S.statusLoaded = !st.error && !hb.error;
  renderBanners();
}
// ---------- EHunt 每日额度 ----------
const bjTime = (t) => new Date(t).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false });
function quota() {
  const st = S.status || {}, limit = st.ehunt_daily_limit || EHUNT_LIMIT, now = Date.now(), pu = ts(st.ehunt_paused_until);
  const exhausted = !!(pu && pu > now);
  let used = st.ehunt_used_today ?? (st.ehunt_remaining != null ? limit - st.ehunt_remaining : null);
  // 记录早于今天 UTC 0 点且未熔断 → 视为已刷新
  const q = ts(st.ehunt_quota_at), mid = new Date(); mid.setUTCHours(0, 0, 0, 0);
  if (!exhausted && q && q < mid.getTime()) used = 0;
  if (exhausted) used = limit;
  const remaining = used == null ? null : Math.max(0, limit - used);
  return { limit, used, remaining, exhausted, resetAt: exhausted ? pu : (() => { const d = new Date(mid); d.setUTCDate(d.getUTCDate() + 1); return d.getTime(); })() };
}
// 提交 n 条时的排程说明（已排队的任务先占用今天额度；缓存命中不耗额度，所以是“约”）
function quotaPlan(n, justAdded = 0) {
  const { remaining, limit, resetAt } = quota(); if (remaining == null) return "";
  const ahead = Math.max(0, [...S.jobs.values()].filter((j) => j.status === "queued" || j.status === "running").length - justAdded);
  let today = Math.min(n, Math.max(0, remaining - ahead)), rest = n - today;
  if (!rest) return "";
  const tm = bjTime(resetAt);
  if (rest <= limit) return `共 ${n} 条：约 ${today} 条今天分析，${rest} 条将在明天额度刷新后（约北京时间 ${tm}）自动分析`;
  const more = rest - limit, days = Math.ceil(more / limit);
  return `共 ${n} 条：约 ${today} 条今天分析，${limit} 条明天额度刷新后（约北京时间 ${tm}）自动分析，${days === 1 ? `其余 ${more} 条后天自动分析` : `其余 ${more} 条在之后 ${days} 天陆续自动分析（每天约 ${limit} 条）`}`;
}
function renderQuota() {
  const el = $("quotaMeter"), q = quota();
  if (q.used == null) return el.classList.add("hidden");
  const pct = Math.min(100, Math.round(q.used / q.limit * 100));
  el.className = `qmeter${q.exhausted ? " qm-bad" : pct >= 80 ? " qm-warn" : ""}`;
  el.innerHTML = `<span class="qm-t">今日 EHunt 额度</span><span class="qm-bar"><i style="width:${pct}%"></i></span><span class="qm-n"><span class="qm-u">已用 </span><b>${q.used}</b> / ${q.limit}</span>`;
}
function renderBanners() {
  const out = [], now = Date.now();
  const last = Math.max(0, ...S.hb.map((h) => ts(h.last_seen) || 0));
  if (S.statusLoaded && (!last || now - last > OFFLINE_MS))
    out.push(`<div class="banner banner-bad"><b>分析服务离线</b>　${last ? `最后心跳 ${dur(now - last)} 前` : "暂无心跳"}；排队任务会保留，服务恢复后自动继续。</div>`);
  const pu = ts(S.status?.ehunt_paused_until);
  if (pu && pu > now) { const n = [...S.jobs.values()].filter((j) => j.status === "queued").length, lim = S.status.ehunt_daily_limit || EHUNT_LIMIT;
    out.push(`<div class="banner banner-warn"><b>今日额度已用完（${lim}/${lim}）</b>，剩余 ${n} 个任务将在额度刷新后（约 北京时间 ${esc(bjTime(pu))}）自动继续<span class="banner-sub">已缓存（24 小时内分析过）的商品不耗额度，会照常处理</span></div>`); }
  renderQuota();
  $("banners").innerHTML = out.join("");
  const u = Object.fromEntries((S.usage || []).map((r) => [r.provider, r]));
  const kfmt = (n) => n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
  const parts = [u.ehunt && `Etsy 数据 <b>${u.ehunt.calls}</b> 次${u.ehunt.cache_hits ? `（缓存 ${u.ehunt.cache_hits}）` : ""}`,
    u.keepamore && `亚马逊数据 <b>${u.keepamore.units}</b> 点`, u.deepseek && `AI <b>${kfmt(u.deepseek.units)}</b> 字`].filter(Boolean);
  const rem = S.status?.ehunt_remaining, el = $("usage");
  el.innerHTML = parts.length || rem != null ? `今日用量：${parts.join(" · ") || "0"}${rem != null ? ` · Etsy 额度剩余 <b>${rem}</b>` : ""}` : "";
  el.title = "Etsy 数据 = EHunt（每条新商品 1 次，缓存命中不计）\n亚马逊数据 = Keepamore（Keepa 数据点）\nAI = DeepSeek（tokens，约等于字数）\n按 UTC 日统计（北京时间 8:00 刷新）";
}

let channel = null, pollTimer = null, statusTimer = null, reloadT = null, resignTimer = null;
const reload = () => { clearTimeout(reloadT); reloadT = setTimeout(loadAll, 400); };
function teardown() {
  if (channel && sb) sb.removeChannel(channel);
  channel = null;
  [pollTimer, statusTimer, resignTimer].forEach((t) => t && clearInterval(t));
  pollTimer = statusTimer = resignTimer = null;
}
function subscribe() {
  if (channel) return;
  channel = sb.channel(`rt-${currentUser}`)
    .on("postgres_changes", { event: "*", schema: "public", table: "jobs" }, (p) => { if (!p.new?.id) return reload();
      if (inLib(p.new)) return; const isNew = !S.jobs.has(p.new.id); S.jobs.set(p.new.id, p.new); render(); if (isNew || p.new.status === "succeeded") reload(); })
    .on("postgres_changes", { event: "*", schema: "public", table: "analyses" }, reload)
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "evidence_snapshots" }, reload)
    .on("postgres_changes", { event: "*", schema: "public", table: "service_status" }, loadStatus)
    .on("postgres_changes", { event: "*", schema: "public", table: "product_library" }, reload)
    .subscribe();
  // 兜底：Realtime 掉线时，有处理中的任务就每 15 秒刷新；心跳/用量每 30 秒；签名 URL 每 5 分钟检查
  pollTimer = setInterval(() => { if ([...S.jobs.values()].some((j) => j.status === "queued" || j.status === "running")) loadAll(); }, 15000);
  statusTimer = setInterval(loadStatus, 30000);
  resignTimer = setInterval(async () => { if (drawerState && await signPaths((S.snaps.get(drawerState.id) || []).map((s) => s.image_path))) renderSnaps(); }, 5 * 60e3);
}

// ---------- 任务操作（全部走 RPC） ----------
const RPC = { pause: ["pause_job", "paused"], resume: ["resume_job", "queued"], retry: ["retry_job", "queued"], rerun: ["retry_job", "queued"],
  reviewed: ["mark_job_reviewed", null], cancel: ["request_cancel", null] };
async function jobAction(id, act) {
  const j = S.jobs.get(id); if (!j) return;
  if (act === "lib") return libToggle([id], !inLib(j));
  if (act === "del") return deleteJobs([id]);
  if (act === "rerun" && !confirm("重新分析会再次调用数据源（24 小时内的 Etsy/亚马逊数据走缓存，不重复扣费）。继续？")) return;
  if (act === "cancel" && j.status === "running" && !confirm("停止运行中的任务？会在当前步骤完成后停下，已发出的请求仍会计费。")) return;
  const [fn, st] = RPC[act], now = new Date().toISOString();
  const local = (res) => { if (st) j.status = st;
    if (act === "cancel") { if (res === "requested") j.cancel_requested = true; else j.status = "canceled"; }
    if (act === "resume" || act === "retry" || act === "rerun") Object.assign(j, { queued_at: now, started_at: null, finished_at: null, error: null, stage: null, cancel_requested: false });
    if (act === "retry" || act === "rerun") j.reviewed_at = null;
    if (act === "reviewed") j.reviewed_at = now; render(); };
  if (DEMO) return local(j.status === "running" ? "requested" : "canceled");
  const { data, error } = await sb.rpc(fn, { p_job: id });
  if (error || data === false || data === "noop") { toast(error?.message || "操作未生效：任务状态已变化，已刷新", "bad"); return loadAll(); }
  local(data);
  if (act === "cancel") toast(data === "requested" ? "已请求停止，将在当前步骤完成后停下" : "已取消", "ok");
  if (act === "rerun" || act === "retry") toast("已重新加入队列", "ok");
}

// ---------- 产品库 / 删除 / 批量 ----------
const nIds = (n) => `<b>${n}</b>`;
async function libToggle(ids, add) {
  const jobs = ids.map((id) => S.jobs.get(id)).filter((j) => j && (add ? j.status === "succeeded" && !inLib(j) : inLib(j)));
  if (!jobs.length) return toast(add ? "没有可加入的已完成任务" : "没有已入库的任务", "bad");
  if (DEMO) {
    jobs.forEach((j) => { const l = lidOf(j), e = S.etsy.get(j.id) || {}, a = S.analyses.get(j.id) || {};
      if (add) S.lib.set(l, { id: `demo-${l}`, job_id: j.id, etsy_listing_id: l, etsy_url: j.etsy_url, title: e.title, image_url: e.image_url, price: e.price,
        product_summary: a.product_summary, target_audience: a.target_audience, keywords: a.keywords, stage: "candidate", reject_reasons: [], added_at: new Date().toISOString() });
      else S.lib.delete(l); });
  } else {
    const { data, error } = add ? await sb.rpc("add_to_library", { p_jobs: jobs.map((j) => j.id) })
      : await sb.rpc("remove_from_library", { p_items: null, p_jobs: jobs.map((j) => j.id) });
    if (error) { toast(error.message, "bad"); return loadAll(); }
    await loadAll();
    if (!data) return toast("操作未生效，已刷新", "bad");
  }
  jobs.forEach((j) => S.sel.delete(j.id));
  if (add) toast(jobs.length > 1 ? `已加入产品库 ${jobs.length} 个` : "已加入产品库", "ok",
    [{ label: "撤销", run: () => undoAdd(jobs) }, { label: "去产品库 →", href: "#/library" }], 6000);
  else toast(`已移出产品库 ${jobs.length} 个，已回到任务列表`, "ok");
  render(); renderLibrary(); if (drawerState) refreshDrawerActs();
}
// 撤销加入：任务已不在列表数据里（服务端过滤），直接按任务 id 调 remove_from_library
async function undoAdd(jobs) {
  if (DEMO) jobs.forEach((j) => { S.lib.delete(lidOf(j)); S.jobs.has(j.id) || S.jobs.set(j.id, j); });
  else { const { error } = await sb.rpc("remove_from_library", { p_items: null, p_jobs: jobs.map((j) => j.id) });
    if (error) return toast(error.message, "bad"); await loadAll(); }
  toast("已撤销，任务已回到列表", "ok"); render(); renderLibrary(); if (drawerState) refreshDrawerActs();
}
async function removeLibItems(items, ask = false) {
  if (!items.length) return;
  if (ask && !await confirmDialog({ title: `移出 ${items.length} 个产品？`, danger: true, ok: "移出", body: "<p>将从产品库删除这些条目（阶段、原因与备注一并删除），任务与分析结果不受影响。</p>" })) return;
  items.forEach((r) => L.sel.delete(r.id));
  if (!DEMO) { const { error } = await sb.rpc("remove_from_library", { p_items: items.map((r) => r.id), p_jobs: null });
    if (error) return toast(error.message, "bad"); }
  items.forEach((r) => S.lib.delete(Number(r.etsy_listing_id)));
  toast(`已移出 ${items.length} 个${items.some((r) => r.job_id) ? "，对应任务已回到“我的任务”" : ""}`, "ok"); render(); renderLibrary();
  if (!DEMO) loadAll(); // 服务端过滤：重新拉取以让任务重新出现
}
async function deleteJobs(ids) {
  const all = ids.map((id) => S.jobs.get(id)).filter(Boolean), run = all.filter((j) => j.status === "running"), jobs = all.filter((j) => j.status !== "running");
  if (!jobs.length) return confirmDialog({ title: "无法删除", body: "运行中的任务不能删除，请先点“■ 停止”，停下后再删除。", ok: "知道了" });
  const libN = jobs.filter(inLib).length;
  if (libN === jobs.length) return confirmDialog({ title: "已在产品库，不会删除", ok: "知道了", body: `<p>${libN > 1 ? `这 ${libN} 个任务` : "这个任务"}对应的产品在产品库中，分析结果会保留，供产品库“查看分析”使用。如需删除，请先将产品移出产品库。</p>` });
  const ok = await confirmDialog({ title: `删除 ${jobs.length - libN} 个任务？`, danger: true, ok: libN === jobs.length ? "知道了" : `删除 ${jobs.length - libN} 个`,
    body: `<p>将永久删除任务及其分析结果、亚马逊匹配和证据截图，<b>不可恢复</b>。</p>
      ${run.length ? `<p class="warn-t">已跳过 ${run.length} 个运行中的任务（请先停止）。</p>` : ""}
      ${libN ? `<p class="warn-t">其中 ${libN} 个已在产品库：这些<b>不会删除</b>，分析结果保留，可在产品库“查看分析”。实际删除 ${jobs.length - libN} 个。</p>` : ""}` });
  if (!ok) return;
  if (!DEMO) {
    const { data, error } = await sb.rpc("delete_jobs", { p_jobs: jobs.map((j) => j.id) });
    if (error) { toast(error.message, "bad"); return loadAll(); }
    const paths = data?.paths || [];
    for (let i = 0; i < paths.length; i += 100) await sb.storage.from(BUCKET).remove(paths.slice(i, i + 100)); // 尽力清理截图文件
    toast(`已删除 ${data?.deleted ?? 0} 个任务${data?.skipped_running ? `，跳过运行中 ${data.skipped_running} 个` : ""}${data?.kept_library ? `，保留产品库中的 ${data.kept_library} 个（分析仍可查看）` : ""}`, "ok");
  } else toast(`已删除 ${jobs.length - libN} 个任务（演示）${libN ? `，保留产品库中的 ${libN} 个` : ""}`, "ok");
  jobs.filter((j) => !inLib(j)).forEach((j) => { S.jobs.delete(j.id); S.sel.delete(j.id); });
  jobs.forEach((j) => S.sel.delete(j.id));
  if (drawerState && jobs.some((j) => j.id === drawerState.id && !inLib(j))) closeDrawer();
  render(); renderLibrary(); if (!DEMO) loadAll();
}
async function retryJobs(ids) {
  const jobs = ids.map((id) => S.jobs.get(id)).filter((j) => j && (j.status === "failed" || j.status === "canceled"));
  if (!jobs.length) return;
  if (!await confirmDialog({ title: `重新提交 ${jobs.length} 个任务？`, ok: "重新提交", body: "<p>任务会重新排队分析，可能再次消耗 EHunt/Keepamore 额度（24 小时内的数据走缓存）。</p>" })) return;
  if (!DEMO) { const { data, error } = await sb.rpc("retry_jobs", { p_jobs: jobs.map((j) => j.id) });
    if (error) { toast(error.message, "bad"); return loadAll(); }
    toast(`已重新提交 ${data} 个`, "ok"); jobs.forEach((j) => S.sel.delete(j.id)); return loadAll(); }
  jobs.forEach((j) => { Object.assign(j, { status: "queued", error: null, queued_at: new Date().toISOString(), started_at: null, finished_at: null }); S.sel.delete(j.id); });
  toast(`已重新提交 ${jobs.length} 个`, "ok"); render();
}
const selJobs = () => [...S.sel].map((id) => S.jobs.get(id)).filter(Boolean);
function bulk(kind) {
  const js = selJobs();
  if (kind === "lib") return libToggle(js.map((j) => j.id), true);
  if (kind === "retry") return retryJobs(js.map((j) => j.id));
  if (kind === "delete") return deleteJobs(js.map((j) => j.id));
}
function bulkWhy(js, canLib, canRetry, canDel) {
  const n = js.length, run = js.filter((j) => j.status === "running").length, why = [];
  if (run) why.push(`运行中的 ${run} 个不能删除`);
  if (canLib < n) why.push("仅已完成的可加入产品库");
  if (canRetry < n) why.push("仅失败的可重新提交");
  return why.length && (canLib !== n || canRetry !== n || canDel !== n) ? `<div class="bulk-why">${why.join("；")}</div>` : "";
}
function renderBulk(visible) {
  const js = selJobs(), n = js.length;
  const canLib = js.filter((j) => j.status === "succeeded" && !inLib(j)).length, canRetry = js.filter((j) => j.status === "failed" || j.status === "canceled").length,
    canDel = js.filter((j) => j.status !== "running").length;
  const allOn = visible.length && visible.every((j) => S.sel.has(j.id));
  $("bulkBar").className = `bulk-bar${n ? " on" : ""}`;
  $("bulkBar").innerHTML = visible.length ? `<label class="selall"><input type="checkbox" data-selall="1"${allOn ? " checked" : ""}> 全选当前 ${visible.length} 个</label>
    ${n ? `<span class="bulk-n">已选 <b>${n}</b></span><span class="spacer"></span>
    <button class="btn btn-ghost btn-sm" data-bulk="lib" title="仅“已完成”的任务可加入产品库"${canLib ? "" : " disabled"}>★ 加入产品库${canLib ? ` (${canLib})` : ""}</button>
    <button class="btn btn-ghost btn-sm" data-bulk="retry" title="仅“异常/已取消”的任务可重新提交"${canRetry ? "" : " disabled"}>↻ 重新提交${canRetry ? ` (${canRetry})` : ""}</button>
    <button class="btn btn-danger-ghost btn-sm" data-bulk="delete" title="运行中的任务不能删除，需先停止"${canDel ? "" : " disabled"}>🗑 删除${canDel ? ` (${canDel})` : ""}</button>
    <button class="btn btn-ghost btn-sm" data-bulk="clear">取消选择</button>${bulkWhy(js, canLib, canRetry, canDel)}` : '<span class="hint">勾选任务可批量加入产品库 / 重新提交 / 删除</span>'}` : "";
}
// ---------- 产品库：阶段（备选/进行中/不考虑）----------
const STAGES = [["candidate", "备选", "◇"], ["active", "进行中", "▶"], ["rejected", "不考虑", "✕"]];
const STAGE_NAME = Object.fromEntries(STAGES.map(([k, n]) => [k, n]));
const REASONS = [["roi_low", "ROI 太低"], ["cost_high", "成本太高"], ["ip_risk", "侵权风险"], ["competition", "竞争太激烈"], ["demand_low", "市场需求太小"],
  ["seasonal", "季节性太强"], ["logistics", "物流难做（易碎/超大/超重）"], ["compliance", "认证合规问题"], ["supply", "供应链难找"], ["other", "其他"]];
const REASON_NAME = Object.fromEntries(REASONS);
const L = { stage: "all", q: "", reason: null, sel: new Set() };
const stageOf = (r) => r.stage || "candidate";
const libById = (id) => [...S.lib.values()].find((x) => x.id === id);
const libMatch = (r, q) => !q || [r.title, r.etsy_listing_id, r.product_summary, r.target_audience, r.shop_name, r.stage_note, ...(r.keywords || [])]
  .some((v) => String(v ?? "").toLowerCase().includes(q));
function libVisible() {
  const q = L.q.trim().toLowerCase();
  return [...S.lib.values()].filter((r) => (L.stage === "all" || stageOf(r) === L.stage) && libMatch(r, q) && (L.stage !== "rejected" || !L.reason || (r.reject_reasons || []).includes(L.reason)))
    .sort((a, b) => String(b.stage_changed_at || b.added_at).localeCompare(String(a.stage_changed_at || a.added_at)));
}
function reasonDialog(n) {
  const body = `<p>请选择不考虑${n > 1 ? "这 " : "这"}${n > 1 ? `<b>${n}</b> 个产品` : "个产品"}的原因（至少一项，可多选）：</p>
    <div class="reason-grid">${REASONS.map(([k, v]) => `<label class="reason-opt"><input type="checkbox" name="rj" value="${k}"> <span>${esc(v)}</span></label>`).join("")}</div>
    <textarea id="rjNote" class="input textarea rj-note" maxlength="2000" placeholder="补充说明（可选）"></textarea><p id="rjErr" class="rj-err"></p>`;
  const p = confirmDialog({ title: "移到「不考虑」", ok: "确认不考虑", danger: true, body, validate: () => {
    const reasons = [...document.querySelectorAll('input[name="rj"]:checked')].map((x) => x.value), note = $("rjNote").value.trim();
    const err = !reasons.length ? "请至少选择一个原因" : reasons.includes("other") && !note ? "选择“其他”时请填写说明" : "";
    $("rjErr").textContent = err; return err ? false : { reasons, note }; } });
  const sync = () => { const other = document.querySelector('input[name="rj"][value="other"]')?.checked;
    $("rjNote").placeholder = other ? "请说明“其他”原因（必填）" : "补充说明（可选）"; $("rjNote").classList.toggle("req", !!other);
    document.querySelectorAll(".reason-opt").forEach((l) => l.classList.toggle("on", l.querySelector("input").checked)); };
  $("modalBody").addEventListener("change", sync);
  return p;
}
async function moveStage(ids, stage) {
  const allow = stage === "candidate" ? ["active", "rejected"] : ["candidate"];
  const items = ids.map(libById).filter((r) => r && allow.includes(stageOf(r)));
  if (!items.length) return toast("只有“备选”中的产品可以移到进行中或不考虑", "bad");
  let reasons = [], note = null;
  if (stage === "rejected") { const v = await reasonDialog(items.length); if (!v) return; ({ reasons, note } = v); }
  if (!DEMO) {
    const { data, error } = await sb.rpc("set_library_stage", { p_items: items.map((r) => r.id), p_stage: stage, p_reasons: reasons, p_note: note || null });
    if (error) { toast(error.message, "bad"); return loadAll(); }
    if (data !== items.length) toast(`部分条目状态已变化，已刷新（成功 ${data}/${items.length}）`, "bad");
    await loadAll();
  } else items.forEach((r) => Object.assign(r, { stage, reject_reasons: stage === "rejected" ? reasons : [], stage_note: stage === "rejected" ? note || null : null, stage_changed_at: new Date().toISOString() }));
  items.forEach((r) => L.sel.delete(r.id));
  const prev = items.map((r) => ({ r, stage: stageOf(r) })); renderLibrary();
  toast(`已移到「${STAGE_NAME[stage]}」${items.length > 1 ? ` ${items.length} 个` : ""}`, "ok", [
    ...(stage !== "candidate" ? [{ label: "撤销", run: () => undoStage(prev.map((p) => p.r.id)) }] : []),
    { label: "查看", run: () => { L.stage = stage; L.reason = null; L.sel.clear(); renderLibrary(); } }], 7000);
}
async function undoStage(ids) { // 撤销 = 移回备选（set_library_stage 允许 active|rejected → candidate）
  if (!DEMO) { const { error } = await sb.rpc("set_library_stage", { p_items: ids, p_stage: "candidate", p_reasons: null, p_note: null });
    if (error) return toast(error.message, "bad"); await loadAll(); }
  else ids.map(libById).forEach((r) => r && Object.assign(r, { stage: "candidate", reject_reasons: [], stage_note: null, stage_changed_at: new Date().toISOString() }));
  toast("已撤销，移回「备选」", "ok"); renderLibrary();
}
function libCard(r) {
  const img = imgUrl(r.image_url), job = r.job_id && S.jobs.get(r.job_id), st = stageOf(r), on = L.sel.has(r.id);
  const url = safeUrl(r.etsy_url || `https://www.etsy.com/listing/${r.etsy_listing_id}`);
  const ic = r.identical_count, sc = r.similar_count;
  const acts = st === "candidate"
    ? `<button class="btn btn-primary btn-sm" data-lmove="active" data-id="${esc(r.id)}">▶ 进行中</button><button class="btn btn-danger-ghost btn-sm" data-lmove="rejected" data-id="${esc(r.id)}">✕ 不考虑</button>`
    : `<button class="btn btn-link btn-sm" data-lmove="candidate" data-id="${esc(r.id)}" title="撤销，移回备选">↩ 移回备选</button>`;
  return `<article class="pcard${on ? " sel" : ""}">
    <div class="pimg">${img ? `<img src="${img}" data-zoom="${img}" data-cap="${esc(r.title)}" alt="" loading="lazy">` : '<div class="pimg-empty">◆</div>'}
      <label class="psel" title="选择"><input type="checkbox" data-lsel="${esc(r.id)}"${on ? " checked" : ""}></label>
      <div class="pbadges">${L.stage === "all" ? `<span class="pb pb-st st-${st}">${esc(STAGE_NAME[st])}</span>` : ""}${!r.job_id ? `<span class="pb pb-del" title="分析任务已删除，仅保留加入时的快照">${S.libPending.has(Number(r.etsy_listing_id)) ? "重新分析中…" : "分析已删除"}</span>` : ""}</div></div>
    <div class="pinfo"><a class="ptitle" href="${url}" target="_blank" rel="noopener" title="${esc(r.title)}">${esc(r.title || `Etsy #${r.etsy_listing_id}`)}</a>
      <div class="pprice">${r.price != null ? `$${esc(Number(r.price).toFixed(2))}` : '<span class="muted">—</span>'}</div>
      ${r.keywords?.length ? `<div class="pline pkw" title="${esc(r.keywords.join(", "))}">${esc(r.keywords.slice(0, 3).join(", "))}</div>` : ""}
      <div class="pline muted" title="加入产品库时的匹配数量">加入时：同款 ${ic ?? "—"} · 类似 ${sc ?? "—"} · ${esc(new Date(r.added_at).toLocaleDateString(undefined, { month: "numeric", day: "numeric" }))}</div>
      ${st === "rejected" ? `<div class="rchips">${(r.reject_reasons || []).map((k) => `<span class="rchip">${esc(REASON_NAME[k] || k)}</span>`).join("")}</div>
        ${r.stage_note ? `<div class="pline pnote" title="${esc(r.stage_note)}">“${esc(r.stage_note)}”</div>` : ""}` : ""}
      <div class="pacts">${acts}</div>
      <div class="pacts2">${r.job_id && (!job || job.status === "succeeded") ? `<button class="btn btn-link btn-sm" data-detail="${esc(r.job_id)}">查看分析 →</button>`
        : S.libPending.has(Number(r.etsy_listing_id)) ? '<span class="hint">重新分析中…</span>' : `<button class="btn btn-link btn-sm" data-reana="${esc(r.id)}" title="为这个商品提交新的分析（24 小时内的数据走缓存，几乎不耗额度）">↻ 重新分析</button>`}
        <span class="spacer"></span><button class="btn btn-link btn-sm muted" data-unlib="${esc(r.id)}">移出产品库</button></div></div></article>`;
}
function libAllBulk() {
  const sel = [...L.sel].map(libById).filter(Boolean), c = sel.filter((r) => stageOf(r) === "candidate").length, o = sel.length - c;
  return `${c ? `<button class="btn btn-primary btn-sm" data-lbulk="active">▶ 进行中 (${c})</button><button class="btn btn-danger-ghost btn-sm" data-lbulk="rejected">✕ 不考虑 (${c})</button>` : ""}${o ? `<button class="btn btn-ghost btn-sm" data-lbulk="candidate">↩ 移回备选 (${o})</button>` : ""}`;
}
async function reanalyze(r) {
  const lid = Number(r.etsy_listing_id), url = r.etsy_url || `https://www.etsy.com/listing/${lid}`;
  if (S.libPending.has(lid)) return;
  if (!DEMO) {
    const { data: b, error } = await sb.from("batches").insert({ name: `重新分析 #${lid}` }).select().single();
    const e2 = error || (await sb.from("jobs").insert({ batch_id: b.id, etsy_url: url, etsy_listing_id: lid })).error;
    if (e2) return toast(`提交失败：${e2.message}`, "bad");
  }
  S.libPending.add(lid); renderLibrary();
  toast("已提交重新分析，完成后自动关联到产品库（任务列表中不显示）", "ok", null, 4000);
}
function renderLibrary() {
  const all = [...S.lib.values()], cnt = Object.fromEntries(STAGES.map(([k]) => [k, all.filter((r) => stageOf(r) === k).length]));
  [...L.sel].forEach((id) => { const r = libById(id); if (!r || (L.stage !== "all" && stageOf(r) !== L.stage)) L.sel.delete(id); });
  $("libCount").textContent = all.length; $("libHeadN").textContent = all.length ? `${all.length} 个` : "";
  $("libSide").innerHTML = `<div class="side-t">阶段</div><button class="side-i${L.stage === "all" ? " on" : ""} st-all" role="tab" data-lstage="all"><span class="side-ic">▦</span>全部<span class="side-n">${all.length}</span></button>` + STAGES.map(([k, n, ic]) => `<button class="side-i${L.stage === k ? " on" : ""} st-${k}" role="tab" data-lstage="${k}"><span class="side-ic">${ic}</span>${n}<span class="side-n">${cnt[k]}</span></button>`).join("");
  const vis = libVisible(), n = L.sel.size, allOn = vis.length && vis.every((r) => L.sel.has(r.id));
  let bar = "";
  if (L.stage === "rejected") { const rc = {}; all.filter((r) => stageOf(r) === "rejected").forEach((r) => (r.reject_reasons || []).forEach((k) => (rc[k] = (rc[k] || 0) + 1)));
    bar += `<div class="rfilter"><span class="hint">按原因：</span><button class="fchip${!L.reason ? " active" : ""}" data-lreason="all">全部</button>${REASONS.filter(([k]) => rc[k]).map(([k, v]) => `<button class="fchip${L.reason === k ? " active" : ""}" data-lreason="${k}">${esc(v)} <span class="n">${rc[k]}</span></button>`).join("")}</div>`; }
  bar += vis.length ? `<div class="lbulk${n ? " on" : ""}"><label class="selall"><input type="checkbox" data-lselall="1"${allOn ? " checked" : ""}> 全选 ${vis.length} 个</label>
    ${n ? `<span class="bulk-n">已选 <b>${n}</b></span><span class="spacer"></span>${L.stage === "all" ? libAllBulk() : L.stage === "candidate"
      ? `<button class="btn btn-primary btn-sm" data-lbulk="active">▶ 进行中 (${n})</button><button class="btn btn-danger-ghost btn-sm" data-lbulk="rejected">✕ 不考虑 (${n})</button>`
      : `<button class="btn btn-ghost btn-sm" data-lbulk="candidate">↩ 移回备选 (${n})</button>`}
      <button class="btn btn-ghost btn-sm" data-lbulk="remove">移出产品库</button><button class="btn btn-ghost btn-sm" data-lbulk="clear">取消选择</button>`
      : `<span class="hint">勾选卡片可批量${L.stage === "candidate" ? "移到进行中 / 不考虑" : L.stage === "all" ? "移动阶段 / 移出" : "移回备选"}</span>`}</div>` : "";
  $("libBar").innerHTML = bar;
  $("libGrid").innerHTML = vis.length ? vis.map(libCard).join("") : `<div class="card empty lib-empty"><div class="ico">★</div><h3>${all.length ? (L.q || L.reason ? "没有匹配的产品" : `「${L.stage === "all" ? "全部" : STAGE_NAME[L.stage]}」里还没有产品`) : "产品库还是空的"}</h3>
    <p class="muted">${all.length ? (L.stage === "candidate" || L.stage === "all" ? "在任务中点“☆ 加入产品库”会进入备选" : "在备选中选择产品移到这里") : "在已完成任务上点“☆ 加入产品库”，或勾选多个任务批量加入"}</p></div>`;
}
function route() {
  const lib = location.hash.startsWith("#/library");
  if (!$("nav").classList.contains("hidden")) { $("app").classList.toggle("hidden", lib); $("libraryView").classList.toggle("hidden", !lib); }
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", (a.dataset.nav === "library") === lib));
  if (lib) renderLibrary();
}
window.addEventListener("hashchange", () => { closeDrawer(); route(); window.scrollTo(0, 0); });
setInterval(() => { document.querySelectorAll("[data-tick]").forEach((el) => { const j = S.jobs.get(el.dataset.tick); if (j) el.textContent = timeText(j); }); }, 1000);
setInterval(renderBanners, 30000);
function timeText(j) {
  const w = waitMs(j), r = runMs(j);
  if (j.status === "queued") return w != null ? `已排队 ${dur(w)}` : "";
  if (j.status === "running") return [w != null ? `排队 ${dur(w)}` : "", `已运行 ${dur(r)}`].filter(Boolean).join(" · ");
  if (r != null && (j.status === "succeeded" || j.status === "failed")) return [w != null ? `排队 ${dur(w)}` : "", `耗时 ${dur(r)}`].filter(Boolean).join(" · ");
  return w != null && j.status !== "paused" ? `排队 ${dur(w)}` : "";
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
  const all = [...S.jobs.values()].filter((j) => !inLib(j)); // 已在产品库（任一阶段）的任务不出现在“我的任务”
  const q = F.q.trim().toLowerCase();
  const match = (j) => { if (!q) return true; const e = S.etsy.get(j.id) || {};
    return String(j.etsy_listing_id || e.listing_id || "").includes(q) || (e.title || "").toLowerCase().includes(q)
      || j.etsy_url.toLowerCase().includes(q) || [...(S.asins.get(j.id) || [])].some((a) => a.toLowerCase().includes(q)); };
  const base = all.filter(match);
  $("chips").innerHTML = S.loaded ? CATS.map(([k, t]) => `<button class="fchip ${F.cat === k ? "active" : ""} fc-${k}" data-cat="${k}">${t} <span class="n">${base.filter((j) => inCat(j, k)).length}</span></button>`).join("") + (q ? '<span class="hint chips-q">（搜索结果）</span>' : "") : "";
  $("qClear").classList.toggle("hidden", !q);
  const nShown = base.filter((j) => inCat(j, F.cat)).length;
  $("searchBanner").innerHTML = q ? `<div class="sbanner">正在搜索“<b>${esc(F.q.trim())}</b>”，共 <b>${nShown}</b> 条${F.cat !== "all" ? `（${esc(CATS.find(([k]) => k === F.cat)[1])}）` : ""} · <a href="#" data-clear="1">清除</a></div>` : "";
  const doneD = all.map(runMs).filter((v, i) => v != null && all[i].status === "succeeded");
  $("avgDur").textContent = doneD.length ? `平均每条分析 ${dur(doneD.reduce((a, b) => a + b, 0) / doneD.length)}（基于 ${doneD.length} 条已完成）` : "";
  if (!S.loaded) return ($("list").innerHTML = skeleton());
  if (!all.length) $("bulkBar").innerHTML = "";
  if (!all.length) return ($("list").innerHTML = `<div class="card empty"><div class="ico">🔍</div><h3>还没有分析记录</h3><p class="muted">在上方粘贴 Etsy 商品链接，点击“开始分析”</p></div>`);
  const jobs = base.filter((j) => inCat(j, F.cat)).sort((x, y) => (PRIO[catOf(x)] - PRIO[catOf(y)]) || y.created_at.localeCompare(x.created_at));
  renderBulk(jobs); renderLibCount();
  const more = S.hasMore ? `<button class="btn btn-ghost load-more" data-more="1">加载更多（已显示 ${all.length} 个任务）</button>` : "";
  if (!jobs.length) return ($("list").innerHTML = `<div class="card empty"><div class="ico">🗂️</div><h3>没有符合条件的任务</h3><p class="muted">换个分类或<a href="#" data-clear="1">清除筛选</a>${S.hasMore ? "；较早的任务需先“加载更多”" : ""}</p></div>${more}`);
  $("list").innerHTML = jobs.map((j) => {
    const a = S.analyses.get(j.id) || {}, e = S.etsy.get(j.id) || {}, sn = S.snaps.get(j.id) || [];
    const cnt = (k) => sn.filter((s) => s.kind === k).length;
    const [stText, stCls] = STATUS[j.status] || [j.status, ""];
    const img = imgUrl(e.image_url);
    const errText = j.error && (j.status === "failed" || j.status === "queued" || j.status === "canceled") ? friendlyErr(j) : "";
    return `<article class="card row${S.flash.has(j.id) ? " flash" : ""}${S.sel.has(j.id) ? " selected" : ""}" data-row="${esc(j.id)}">
      <label class="selbox" title="选择"><input type="checkbox" data-sel="${esc(j.id)}"${S.sel.has(j.id) ? " checked" : ""}></label>
      ${img ? `<img class="thumb" src="${img}" data-zoom="${img}" data-cap="${esc(e.title)}" alt="" loading="lazy">` : `<div class="thumb ${j.status === "running" || j.status === "queued" ? "sk" : ""}"></div>`}
      <div class="c-main">
        <a class="title" href="${safeUrl(j.etsy_url)}" target="_blank" rel="noopener">${esc(e.title || `Etsy #${j.etsy_listing_id || ""}`)}</a>
        <div class="sub">${esc(a.product_summary || (j.status === "succeeded" || j.status === "failed" ? "" : j.status === "paused" ? "已暂停，点击继续后重新排队" : j.status === "queued" && quota().exhausted ? "等待额度刷新" : "等待分析…"))}</div>
        <div class="meta">${pill(stText + (j.status === "running" && STAGE[j.stage] ? " · " + STAGE[j.stage] : "") + (j.cancel_requested && j.status === "running" ? " · 停止中" : ""), stCls)}${j.status === "queued" && quota().exhausted ? '<span class="pill pill-warn">等待额度刷新</span>' : ""}${S.flash.has(j.id) ? '<span class="pill pill-new">新提交</span>' : ""}
          ${e.price ? `<span class="chip">$${esc(e.price)}</span>` : ""}${ehuntChips(e)}${kwChips(a.keywords)}</div>
        ${errText ? `<div class="err">${errText}</div>` : ""}
        <div class="timing"><span data-tick="${esc(j.id)}">${esc(timeText(j))}</span>${j.status === "succeeded" && !j.reviewed_at ? ' <span class="pill pill-warn">待我处理</span>' : ""}${S.keep.has(j.id) && j.reviewed_at ? ' <span class="pill pill-ok" title="已标记为已查看，刷新页面后移到“已完成”">✓ 已查看</span>' : ""}</div>
      </div>
      <div class="c-aud"><div class="label">目标人群</div><div class="aud">${audienceSummary(a) || '<span class="hint">—</span>'}</div>${audienceDetail(a)}</div>
      <div class="c-act actions">${sn.some((s) => s.kind !== "search_result") ? `<div class="meta" style="margin:0;justify-content:flex-end">
          <span class="pill kpill k-identical">同款 ${cnt("identical")}</span><span class="pill kpill k-similar">类似 ${cnt("similar")}</span></div>` : ""}
        ${a.decision_trace && j.status === "succeeded" ? `<button class="btn btn-ghost btn-sm" data-detail="${esc(j.id)}">决策过程 →</button>` : ""}
        ${actBtns(j)}</div>
    </article>`;
  }).join("") + more;
}
function friendlyErr(j) {
  const e = j.error || "";
  if (j.status === "canceled") return esc(e);
  if (/商品不存在或已下架/.test(e)) return "商品不存在或已下架：请确认链接能在 Etsy 打开（未继续消耗亚马逊/AI 额度）";
  if (/短链无法展开/.test(e)) return esc(e);
  if (/额度/.test(e) && j.status === "queued") return "";
  return `${j.status === "queued" ? "上次尝试失败，将自动重试：" : ""}${esc(e)}`;
}

const TIPS = { pause: "仅对排队中的任务生效；点“继续”后重新排队", cancel: "排队/暂停中：直接取消。运行中：在当前步骤完成后停下，已发出的请求仍会计费", resume: "重新加入队列" };
const actBtns = (j) => { const b = (act, t, cls = "btn-ghost") => { const tip = act === "cancel" && j.status === "running" ? "会在当前步骤完成后停下，已发出的请求仍会计费" : TIPS[act];
    return `<button class="btn ${cls} btn-sm" data-act="${act}" data-id="${esc(j.id)}"${tip ? ` title="${esc(tip)}" aria-label="${esc(t.replace(/^\S+\s/, "") + "：" + tip)}"` : ""}>${t}${tip ? '<span class="ib" aria-hidden="true">ⓘ</span>' : ""}</button>`; };
  switch (j.status) {
    case "queued": return b("pause", "⏸ 暂停") + b("cancel", "✕ 取消");
    case "running": return j.cancel_requested ? '<button class="btn btn-ghost btn-sm" disabled>停止中…</button>' : b("cancel", "■ 停止");
    case "paused": return b("resume", "▶ 继续", "btn-primary") + b("cancel", "✕ 取消");
    case "failed": case "canceled": return b("retry", "↻ 重新提交", "btn-primary") + b("del", "🗑 删除", "btn-danger-ghost");
    case "succeeded": return libBtn(j) + (j.reviewed_at ? "" : b("reviewed", "✓ 标记已查看")) + b("rerun", "↻ 重新分析") + b("del", "🗑 删除", "btn-danger-ghost");
    default: return "";
  } };
const libBtn = (j) => inLib(j) ? `<button class="btn btn-lib-on btn-sm" data-act="lib" data-id="${esc(j.id)}" title="点击移出产品库"><span class="on-t">★ 已加入产品库</span><span class="off-t">移出产品库</span></button>`
  : `<button class="btn btn-lib btn-sm" data-act="lib" data-id="${esc(j.id)}">☆ 加入产品库</button>`;
// 每个搜索词短语一个 chip（不拆词）；chip 之间放屏幕阅读器可读的“, ”分隔
const kwChips = (ks) => (ks || []).map((k) => `<span class="chip chip-copy chip-kw" data-copy="${esc(k)}" title="${esc(k)}（点击复制）" role="button" tabindex="0">${esc(k)}</span>`).join('<span class="sr-only">, </span>');
const renderLibCount = () => { $("libCount").textContent = S.lib.size; };
function refreshDrawerActs() { const el = $("drawerActs"), j = drawerState && S.jobs.get(drawerState.id); if (el && j) el.innerHTML = libBtn(j) + `<button class="btn btn-danger-ghost btn-sm" data-act="del" data-id="${esc(j.id)}">🗑 删除</button>`; }
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
    <div id="drawerActs" class="drawer-acts"></div>
    <div class="summary">
      ${a.product_summary ? `<div class="kv"><b>产品</b>　${esc(a.product_summary)}</div>` : ""}
      ${a.target_audience ? `<div class="kv"><b>人群</b>　${esc(a.target_audience)}</div>` : ""}
      ${(a.keywords || []).length ? `<div class="kv"><b>搜索词</b>　${kwChips(a.keywords)}</div>` : ""}
      <div class="kv"><b>匹配</b>　<span class="pill kpill k-identical">同款 ${nI}</span> <span class="pill kpill k-similar">类似品 ${nS}</span></div></div>
    <div class="toolbar">
      <label class="sort">排序 <select id="sortSel" class="input input-sm">${Object.entries(SORTS).map(([k, [t]]) => `<option value="${k}"${k === sortKey ? " selected" : ""}>${t}</option>`).join("")}</select></label>
      <span class="spacer"></span><span class="tb-btns">
      ${(a.keywords || []).length ? `<button class="btn btn-ghost btn-sm" data-copyall="1">复制搜索词</button>` : ""}
      <button class="btn btn-ghost btn-sm" data-export="job">⇩ 导出 CSV</button></span></div>
    <h3>亚马逊同款 <span class="hint">${nI} 个</span></h3><div id="grid-identical" class="grid"></div>
    <h3>亚马逊类似品 <span class="hint">${nS} 个</span></h3><div id="grid-similar" class="grid"></div>
    <h3 id="traceHead">决策过程</h3><ol class="timeline">${steps}</ol>
    <p class="note">亚马逊不公开精确销量：有“过去一月购买”(monthlySold) 的商品用该值，其余为 BSR 推算的估计值；同款/类似品判定方法见“匹配同款/类似”步骤。</p>`;
  refreshDrawerActs();
  renderSnaps();
  loadMatches(id);
  $("drawerMask").classList.remove("hidden"); $("drawer").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false");
  $("drawerBody").scrollTop = 0;
}
function renderSnaps() {
  if (!drawerState) return;
  ["identical", "similar"].forEach((k) => { const g = $("grid-" + k); if (!g) return;
  const mm = new Map((S.matches.get(drawerState.id) || []).map((m) => [m.asin, m]));
  const list = (S.snaps.get(drawerState.id) || []).filter((s) => s.kind === k).map((s) => ({ ...s, _m: mm.get(s.asin) }))
    .sort(SORTS[sortKey][1]);
  g.innerHTML = list.length ? list.map((s) => {
    const u = imgUrl(signedUrl(s.image_path)), mo = monthly(s) ?? monthly(s._m);
    return `<div class="snap k-${esc(s.kind)}">${u ? `<img src="${u}" data-path="${esc(s.image_path)}" data-zoom="${u}" data-cap="${esc(s.title)}" alt="" loading="lazy">` : '<div class="sk" style="aspect-ratio:16/10"></div>'}
      <div class="snap-b"><div class="snap-t">${esc(s.title)}</div>
        ${s.keyword ? `<div>搜索词 <span class="chip chip-copy" data-copy="${esc(s.keyword)}" title="点击复制">${esc(s.keyword)}</span></div>` : ""}
        ${s.asin ? `<div class="facts"><span>ASIN <b>${esc(s.asin)}</b></span><span><b>$${esc(s.price)}</b></span>${mo != null ? `<span title="${salesSrc(s, s._m) === "monthlySold" ? "亚马逊页面“过去一个月购买”" : "按 BSR 推算的估计值，仅供参考"}">月销 <b>${salesSrc(s, s._m) === "monthlySold" ? "" : "≈"}${esc(mo)}</b> <small class="muted">${salesSrc(s, s._m) === "monthlySold" ? "亚马逊月购" : "BSR 估算"}</small></span>` : ""}<span>BSR <b>${esc(s.bsr ?? "-")}</b></span><span>评论 <b>${esc(s.review_count ?? "-")}</b></span>${s.rating ? `<span>★ <b>${esc(s.rating)}</b></span>` : ""}</div>` : ""}
        ${s.similarity != null ? `<div>相似度 <b>${esc(pct(s.similarity))}</b></div>` : ""}
        <div class="reason">${esc(s.similarity_reason)}</div>
        <div class="meta" style="margin-top:4px"><span class="pill kpill k-${esc(s.kind)}">${KIND[s.kind]}</span><span class="hint">${esc(IMG_SRC[s.image_source] || s.image_source)}</span><span class="spacer"></span><a href="${safeUrl(s.source_url)}" target="_blank" rel="noopener">来源 ↗</a></div>
      </div></div>`;
  }).join("") : '<p class="hint">无</p>'; });
}
const salesSrc = (...xs) => { for (const x of xs) { const r = (x || {}).raw || {}; if (r.sales_source) return r.sales_source; if (r.monthlySold > 0) return "monthlySold"; } return "bsr_estimate"; };
const pct = (v) => { const n = Number(v); return isNaN(n) ? v : `${Math.round(n <= 1 ? n * 100 : n)}%`; };
const num = (v) => (v == null || v === "" || isNaN(+v) ? null : +v);
const by = (f, asc) => (x, y) => { const a = num(f(x)), b = num(f(y)); if (a == null && b == null) return 0; if (a == null) return 1; if (b == null) return -1; return asc ? a - b : b - a; };
const SORTS = { sim: ["相似度 高→低", by((s) => s.similarity, false)], sales: ["月销 高→低", by((s) => monthly(s) ?? monthly(s._m), false)],
  bsr: ["BSR 小→大（越畅销）", by((s) => s.bsr, true)], price: ["价格 低→高", by((s) => s.price, true)], reviews: ["评论 多→少", by((s) => s.review_count, false)] };
let sortKey = "sim";
// 产品库条目的任务不在任务列表数据里：按需单独拉取
async function ensureJob(id) {
  if (S.jobs.has(id) || DEMO || !sb) return S.jobs.has(id);
  const [j, a, e, s] = await Promise.all([sb.from("jobs").select("*").eq("id", id).maybeSingle(), sb.from("analyses").select("*").eq("job_id", id),
    sb.from("etsy_products").select("job_id,title,image_url,price,shop_name,listing_id,raw").eq("job_id", id), sb.from("evidence_snapshots").select("*").eq("job_id", id).order("id")]);
  if (!j.data) { toast("分析任务已不存在", "bad"); return false; }
  S.jobs.set(id, j.data); (a.data || []).forEach((r) => S.analyses.set(id, r)); (e.data || []).forEach((r) => S.etsy.set(id, r)); S.snaps.set(id, s.data || []);
  return true;
}
async function loadMatches(id) {
  if (DEMO) { S.matches.set(id, (S.snaps.get(id) || []).filter((s) => s.asin && s.kind !== "search_result").map((s) => ({ ...s, match_type: s.kind, url: s.source_url }))); return; }
  const sn = S.snaps.get(id) || [];
  const [{ data }] = await Promise.all([sb.from("amazon_matches").select("*").eq("job_id", id), signPaths(sn.map((s) => s.image_path))]);
  S.matches.set(id, data || []);
  if (drawerState?.id === id) renderSnaps();
}
function exportJob(id) {
  const j = S.jobs.get(id), a = S.analyses.get(id) || {}, e = S.etsy.get(id) || {};
  const sn = new Map((S.snaps.get(id) || []).filter((s) => s.asin).map((s) => [s.asin, s]));
  const rows = (S.matches.get(id) || []).map((m) => ({ ...sn.get(m.asin), ...m })).sort(SORTS[sortKey][1]);
  downloadCsv(`etsy-${j.etsy_listing_id || e.listing_id || id.slice(0, 8)}-amazon.csv`,
    ["Etsy listing", "Etsy 标题", "类型", "ASIN", "亚马逊标题", "价格", "月销", "销量来源", "BSR", "评论数", "评分", "相似度", "判定理由", "搜索词", "链接"],
    rows.map((m) => [j.etsy_listing_id || e.listing_id, e.title, KIND[m.match_type || m.kind] || m.match_type, m.asin, m.title, m.price, monthly(m),
      (m.raw || {}).sales_source === "monthlySold" ? "亚马逊过去一月购买" : monthly(m) != null ? "BSR 估算" : "", m.bsr, m.review_count, m.rating, m.similarity != null ? pct(m.similarity) : "",
      m.similarity_reason, m.keyword, m.url || m.source_url]));
}
function exportList() {
  const jobs = [...document.querySelectorAll("[data-row]")].map((el) => S.jobs.get(el.dataset.row)).filter(Boolean);
  const cnt = (id, k) => (S.snaps.get(id) || []).filter((s) => s.kind === k).length;
  downloadCsv(`etsy-analyzer-${new Date().toISOString().slice(0, 10)}.csv`,
    ["Etsy listing", "标题", "状态", "产品", "目标人群", "搜索词", "同款数", "类似品数", "价格", "提交时间", "Etsy 链接", "错误"],
    jobs.map((j) => { const a = S.analyses.get(j.id) || {}, e = S.etsy.get(j.id) || {};
      return [j.etsy_listing_id || e.listing_id, e.title, (STATUS[j.status] || [j.status])[0], a.product_summary, a.target_audience, a.keywords,
        cnt(j.id, "identical"), cnt(j.id, "similar"), e.price, j.created_at, j.etsy_url, j.error]; }));
}
function closeDrawer() {
  const j = drawerState && S.jobs.get(drawerState.id); // 关闭抽屉时才标记已查看；本次会话卡片位置不变
  if (j && j.status === "succeeded" && !j.reviewed_at && !inLib(j)) { S.keep.add(j.id); jobAction(j.id, "reviewed"); }
  $("drawer").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); $("drawerMask").classList.add("hidden"); drawerState = null; }

document.addEventListener("click", (ev) => {
  const t = ev.target.closest("[data-zoom],[data-detail],[data-tab],[data-cat],[data-act],[data-clear],[data-copy],[data-copyall],[data-export],[data-more],[data-dupe],[data-view],[data-bulk],[data-unlib],[data-lstage],[data-lmove],[data-lreason],[data-lbulk],[data-libgo],[data-reana]") || ev.target;
  if (t.dataset?.bulk) { if (t.dataset.bulk === "clear") { S.sel.clear(); return render(); } return bulk(t.dataset.bulk); }
  if (t.dataset?.unlib) { const r = [...S.lib.values()].find((x) => x.id === t.dataset.unlib); return r && removeLibItems([r]); }
  if (t.dataset?.lstage) { L.stage = t.dataset.lstage; L.reason = null; L.sel.clear(); return renderLibrary(); }
  if (t.dataset?.lreason) { L.reason = t.dataset.lreason === "all" || L.reason === t.dataset.lreason ? null : t.dataset.lreason; return renderLibrary(); }
  if (t.dataset?.reana) { const r = libById(t.dataset.reana); return r && reanalyze(r); }
  if (t.dataset?.lmove) return moveStage([t.dataset.id], t.dataset.lmove);
  if (t.dataset?.lbulk) { if (t.dataset.lbulk === "clear") { L.sel.clear(); return renderLibrary(); }
    if (t.dataset.lbulk === "remove") return removeLibItems([...L.sel].map(libById).filter(Boolean), true);
    return moveStage([...L.sel], t.dataset.lbulk); }
  if (t.dataset?.libgo) { L.stage = t.dataset.libgo; L.sel.clear(); if (location.hash.startsWith("#/library")) renderLibrary(); return; }
  if (t.dataset?.copy) return copyText(t.dataset.copy);
  if (t.dataset?.copyall && drawerState) return copyText((S.analyses.get(drawerState.id)?.keywords || []).join(", "));
  if (t.dataset?.export) return t.dataset.export === "job" ? drawerState && exportJob(drawerState.id) : exportList();
  if (t.dataset?.more) { S.limit += PAGE; t.disabled = true; t.textContent = "加载中…"; return loadAll(); }
  if (t.dataset?.dupe && pending) { const { ok, bad, dupes } = pending, ids = new Set(dupes.map((d) => d.etsy_listing_id));
    if (t.dataset.dupe === "cancel") { pending = null; $("dupeBox").innerHTML = ""; return; }
    if (DEMO) { pending = null; $("dupeBox").innerHTML = ""; return showSubmitResult({ ok: true, text: "演示模式不会提交" }); }
    return submitJobs(t.dataset.dupe === "skip" ? ok.filter((r) => !ids.has(r.id)) : ok, bad.length); }
  if (t.dataset?.view) { ev.preventDefault(); const id = t.dataset.view;
    if (location.hash.startsWith("#/library")) location.hash = "#/";
    if (S.jobs.get(id)?.status === "succeeded") return openDetail(id);
    F.cat = "all"; F.q = t.dataset.lid; $("q").value = F.q; render(); return document.querySelector("[data-row]")?.scrollIntoView({ behavior: "smooth", block: "center" }); }
  if (t.dataset?.cat) { F.cat = t.dataset.cat; return render(); }
  if (t.dataset?.act) return jobAction(t.dataset.id, t.dataset.act);
  if (t.dataset?.clear) { ev.preventDefault(); F.cat = "all"; F.q = ""; $("q").value = ""; return render(); }
  if (t.dataset?.zoom) { $("lbImg").src = t.dataset.zoom; $("lbCap").textContent = t.dataset.cap || ""; $("lightbox").classList.remove("hidden"); return; }
  if (t.dataset?.detail) { const id = t.dataset.detail; return S.jobs.has(id) ? openDetail(id) : ensureJob(id).then((ok) => ok && openDetail(id)); }
  if (t.closest?.("#lightbox")) $("lightbox").classList.add("hidden");
  if (t.id === "drawerMask" || t.closest?.("#drawerClose")) closeDrawer();
});
document.addEventListener("keydown", (ev) => {
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.dataset?.copy) { ev.preventDefault(); return copyText(ev.target.dataset.copy); }
  if (ev.key !== "Escape") return;
  if (!$("modal").classList.contains("hidden")) return closeModal(false);
  if (!$("lightbox").classList.contains("hidden")) $("lightbox").classList.add("hidden"); else closeDrawer();
});

$("q").addEventListener("input", () => { F.q = $("q").value; render(); });
document.addEventListener("change", (ev) => { if (ev.target.id === "sortSel") { sortKey = ev.target.value; renderSnaps(); }
  const t = ev.target;
  if (t.dataset?.sel) { t.checked ? S.sel.add(t.dataset.sel) : S.sel.delete(t.dataset.sel); render(); }
  if (t.dataset?.lsel) { t.checked ? L.sel.add(t.dataset.lsel) : L.sel.delete(t.dataset.lsel); return renderLibrary(); }
  if (t.dataset?.lselall) { libVisible().forEach((r) => t.checked ? L.sel.add(r.id) : L.sel.delete(r.id)); return renderLibrary(); }
  if (t.dataset?.selall) { const ids = [...document.querySelectorAll("[data-sel]")].map((x) => x.dataset.sel); ids.forEach((id) => t.checked ? S.sel.add(id) : S.sel.delete(id)); render(); } });
$("libQ").addEventListener("input", () => { L.q = $("libQ").value; renderLibrary(); });
$("qClear").onclick = () => { F.q = ""; $("q").value = ""; render(); $("q").focus(); };
$("btnClear").onclick = () => { F.cat = "all"; F.q = ""; $("q").value = ""; render(); };
boot();
