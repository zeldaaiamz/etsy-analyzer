// 获取链接（#/selscreen[/<run>]，导航在「任务」前）。Hongyan 2026-10-16 定稿：
// ① 关键词 → 查 EHunt 结果数（价格 8–40 · 总销量 ≥100 · 7 天销量 >3 · 按 7 天销量降序）
// ② 查到数量（counted 且 >0）后自动入队抓取（052，lim=min(total,5000)=EHunt/MAX_ROWS；0 条不抓）；也可手动改条数兜底
// ③ 标题规则先筛：排除的不调豆包、不显示、不入库（页面只一行汇总；原因写 box 本地日志 3 天）→ ④ 通过的调豆包视觉 → 显示符合要求 N 条 + 纯 Etsy 链接
// ⑤ >1000 分批，每批 1000，最多 5 批 / 5,000；“还剩 N 条未读”只在每批完成后更新 ⑥ 只有符合要求的入库，保留 3 天 ⑦ 网页无搜索，查询找序猿
import { createSelQueue } from "./selqueue.js?v=608800a5";
const BATCH = 1000, MAX_ROWS = 5000; // 每批 1000；自动入队上限 min(total_count, MAX_ROWS)=5000（与 052 / EHunt 前 5,000 条一致）
const RORDER = ["定制", "IP", "材质", "素材/数字文件", "7天销量≤3", "其他"];
// ---- 纯函数（web/tests/selscreen.test.mjs）----
// 关键词：去首尾空格、连续空白合成一个（EHunt 的 Keyword 标签按单空格显示）
export const normKw = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
// 下载文件名：保留中日韩/字母数字（原来 [^\w-] 会把中文关键词变成 ___）
export const safeName = (s) => (String(s ?? "").replace(/[^\p{L}\p{N}_-]+/gu, "_").replace(/^_+|_+$/g, "").slice(0, 60) || "links");
// worker 原始英文报错 → 给人看的中文（原文放 tooltip）
export function friendlyErr(e) {
  const t = String(e || "");
  if (!t) return "";
  if (/Playwright Sync API inside the asyncio loop/i.test(t)) return "box 抓取进程异常（上一次失败后浏览器没有释放），需要重启 box 上的抓取进程后再试";
  if (/Remote end closed|RemoteDisconnected|Connection reset|URLError|timed out|Temporary failure in name resolution/i.test(t)) return "box 与数据库的连接中断（网络抖动），可点“重试本批”或重新查询";
  if (/EHunt 登录已失效|EHunt 未登录/.test(t)) return "EHunt 登录已失效：需要在 box 上重新人工登录 EHunt";
  if (/Timeout \d+ms exceeded|翻页到第 \d+ 页超时/i.test(t)) return "EHunt 页面响应超时（网页卡住或改版），请稍后重试";
  if (/关键词未生效|筛选未生效|排序不是/.test(t)) return t;
  return /[\u4e00-\u9fff]/.test(t) ? t : `抓取出错：${t.slice(0, 120)}`;
}
// 最近 3 天标签的副文字：按状态显示（原来全部显示“符合 0 条”）
export function chipText(r) {
  const n = (v) => (v || 0).toLocaleString();
  if (["count_queued", "counting"].includes(r.status)) return "查询中";
  if (r.status === "counted") return r.total_count == null ? "待抓取" : (r.total_count > 0 ? `待抓取 · 共 ${n(r.total_count)} 条` : "0 条 · 不抓取");
  if (["queued", "running"].includes(r.status)) return `读取中 · 已符合 ${n(r.passed_count)} 条`;
  if (r.status === "failed") return r.total_count == null ? "查询失败" : `失败 · 符合 ${n(r.passed_count)} 条`;
  if (r.status === "canceled") return `已停止 · 符合 ${n(r.passed_count)} 条`;
  if (r.status === "paused") return `已暂停 · 已符合 ${n(r.passed_count)} 条`;
  return `符合 ${n(r.passed_count)} 条`;
}
// 最近 3 天查找记录：最多 10 条、新的在前；折叠状态记在 localStorage（默认：桌面展开、手机 ≤560px 收起）
export const RECENT_N = 10, RECENT_KEY = "sel.recentOpen";
export function recentOpen(store, narrow) { const v = store?.getItem?.(RECENT_KEY); return v === "1" ? true : v === "0" ? false : !narrow; }
export function recentList(rows, now = Date.now()) { const since = now - 3 * 864e5; return (rows || []).filter((r) => Date.parse(r.created_at) > since).sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, RECENT_N); }
// 送去预筛选状态（按批次，sel_batches.sent_to_screen_at，038）：显示最近一次发送的批次，如「第二批已发送」；从未发送 →「未发送」
import { cnNum, batchLabel, historySendNos } from "./batchname.js?v=7b158eb8";
export { cnNum };
// 039：sel_batches.sent_count = 本批已发送多少条（按 rank 顺序）；038 旧标记（sent_count 为空）视为整批已发送
export function sendState(b, total = b?.passed_count) {
  const t = Math.max(0, Number(total) || 0);
  if (!b?.sent_to_screen_at) return { sent: 0, total: t, full: false, partial: false, left: t, at: null };
  const n = b.sent_count == null ? t : Math.min(t, Number(b.sent_count) || 0);
  return { sent: n, total: t, full: n >= t, partial: n > 0 && n < t, left: t - n, at: b.sent_to_screen_at };
}
// keyword + nos（historySendNos 的结果）时显示「<关键词> 第N批」，N = 同一关键词在历史里的发送顺序
// 每条链接是否已送去预筛选（只读，来自库里的 sel_batches.sent_to_screen_at / sent_count）：
// 039/041/042 都按 rank 顺序（rank nulls last, id，与这里读 sel_items 的顺序一致）发送，额度满即停 → 已发送的总是前 sent_count 条；
// 其中「已在预筛选列表里被去重跳过」的也计入 sent_count（算已发送）；038 旧标记 sent_count 为空 = 整批已发送
export function linkSentFlags(b, n) { const st = sendState(b, n); return Array.from({ length: n }, (_, i) => i < st.sent); }
export function sentInfo(batches, keyword, nos, runId) {
  const S = (batches || []).filter((b) => b.sent_to_screen_at).sort((a, b) => Date.parse(b.sent_to_screen_at) - Date.parse(a.sent_to_screen_at));
  if (!S.length) return { sent: false, text: "未发送", tip: "这次查找的链接还没有送去预筛选", tags: [] };
  const st = sendState(S[0]);
  if (keyword && nos) {
    const tags = S.slice().reverse().map((b) => batchLabel(keyword, nos.get(`${runId}:${b.batch_no}`) || b.batch_no));
    return { sent: true, partial: st.partial, auto: !!S[0].auto_sent_at, text: `${tags[tags.length - 1]}${S[0].auto_sent_at ? " · 已自动发送" : ""}${st.partial ? `（部分 ${st.sent}/${st.total}）` : ""}`, tags, tip: `已发送到预筛选：${tags.join("、")}` };
  }
  const nosB = [...new Set(S.map((b) => b.batch_no))].sort((a, b) => a - b);
  return { sent: true, partial: st.partial, text: `第${cnNum(S[0].batch_no)}批已发送${st.partial ? `（部分 ${st.sent}/${st.total}）` : ""}`, tip: `第${nosB.map(cnNum).join("、")}批已发送到预筛选` };
}
export { historySendNos };
export const fmtAt = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }); };
// 「送去预筛选」确认框文字：未发送 / 已全部发送（明确再发一次）/ 部分发送（发剩余）
export function sendPlan(batches, linksOf, single) {
  const rows = batches.map((b) => ({ b, st: sendState(b, (linksOf(b.id) || []).length) })).filter((x) => x.st.total > 0);
  if (single) { const x = rows[0]; if (!x) return null; return { ids: [x.b.id], resend: x.st.full, n: x.st.full ? x.st.total : x.st.left, rows }; }
  const todo = rows.filter((x) => !x.st.full), skipped = rows.filter((x) => x.st.full);
  // 047：全部已发送时仍可点 → 不重复发送，只把在预筛选里删除过的恢复显示
  if (!todo.length && skipped.length) return { ids: skipped.map((x) => x.b.id), resend: false, n: 0, rows: [], skipped, restoreOnly: true };
  return { ids: todo.map((x) => x.b.id), resend: false, n: todo.reduce((a, x) => a + x.st.left, 0), rows: todo, skipped };
}
// 等待了多久（box 暂停时排队会很久，给出提示，不是卡死）
export const waitedMin = (iso, now = Date.now()) => (iso ? Math.max(0, Math.floor((now - Date.parse(iso)) / 60000)) : 0);

// 043 关键词查询记录：提交前提示「该关键词在X月X日已经查询过」（日期按 Asia/Shanghai，新的在前，同日只列一次）
const SH_MD = (t) => { const d = new Date(Date.parse(t) + 8 * 36e5); return `${d.getUTCMonth() + 1}月${d.getUTCDate()}日`; };
export const kwKey = (s) => normKw(s).toLowerCase();
export function seenDates(times) { const out = []; for (const t of [...(times || [])].sort((a, b) => Date.parse(b) - Date.parse(a))) { const m = SH_MD(t); if (!out.includes(m)) out.push(m); } return out; }
export function seenWarn(times) { const d = seenDates(times); return d.length ? `该关键词在${d.join("、")}已经查询过` : ""; }
export function kwLogCsv(rows) { const c = (v) => { let x = String(v ?? ""); if (/^[=+\-@\t\r]/.test(x)) x = "'" + x; return /[",\n]/.test(x) ? `"${x.replace(/"/g, '""')}"` : x; };
  const fmt = (t) => new Date(Date.parse(t) + 8 * 36e5).toISOString().slice(0, 19).replace("T", " ");
  return "\ufeff关键词,查询时间（北京时间）,来源\n" + (rows || []).map((r) => [r.keyword, fmt(r.queried_at), r.source].map(c).join(",")).join("\n") + "\n"; }

// 送去预筛选结果 → 提示文案 + 跳转目标（044：被删除过的任务恢复显示 restored；sent=0 也跳到预筛选，避免“发送成功但看不到”）
export function sendOutcome(res, left = 0) {
  const sent = res?.sent || 0, dup = res?.dup || 0, reused = res?.reused || 0, rest = res?.restored || 0, hidden = Math.max(0, reused - rest);
  const extra = (dup ? `；${dup} 条已在预筛选列表里` : "") + (rest ? `；${rest} 条之前在预筛选里删除过，已恢复显示（不重新获取）` : "")
    + (hidden ? `；${hidden} 条已获取过但在预筛选里删除过，未恢复（需要后台执行 044）` : "");
  const go = sent && res.first_screen_batch ? `#/screen/${res.first_screen_batch}` : (sent || rest || dup) ? "#/screen" : null;
  if (!sent) return { kind: rest || dup ? "ok" : "bad", go, text: rest || dup || reused ? `没有需要新获取的链接${extra}` : "这些批次都已发送过，没有重复发送" };
  if (res.limited) return { kind: "bad", go, text: `已发送 ${sent} 条（${res.screen_batches} 个预筛选批次）；还有 ${left} 条因为预筛选排队已满 1000 条没有发送，等预筛选跑完一些后点「发送剩余」${extra}` };
  return { kind: "ok", go, text: `✓ 已发送 ${sent} 条到预筛选${res.screen_batches > 1 ? `（分成 ${res.screen_batches} 个批次，每个 ≤500 条）` : ""}${extra}` };
}
// 「查询数量」只在本次提交请求进行中禁用（防连点）；当前任务进行中也能查询新词，进入队列按顺序执行（037/048）
export const countDisabled = (run, submitting) => !!submitting;
export function createSelScreen({ $, esc, toast, getSb, DEMO, confirmDialog }) {
  const QSP = new URLSearchParams(location.search);
  const V = { addErr: "", run: null, batches: [], links: {}, open: new Set(), timer: null, fx: null, demoRuns: {}, recent: null, limErr: "", busy: new Set(), seq: 0, lastSt: null, sending: false };
  const FILTERS = { price_min: 8, price_max: 40, min_sales: 100, min_sales_7d: 4, sort: "↓7-day sales" };
  const ACTIVE_ST = ["count_queued", "counting", "queued", "running", "paused"];  // 与 sel_delete_run 一致（037/038）
  const active = (r) => r && ["count_queued", "counting", "queued", "running"].includes(r.status);
  // 实测数（全部筛选含 7 天销量 Min 4，2026-10-16）。其他词在演示里会生成数字并标“演示数字”
  const MEASURED = { "acrylic decoration": [20, "Find 20 products"], "sign": [1688, "共 1,688 条 / 85 页"] };
  const DEMO_ONLY = { "acrylic sign": 3260, "no results": 0 }; // 演示数字（用于演示多批 / 一键读取全部剩余），界面标“演示数字”
  const demoTotal = (kw) => MEASURED[kw.toLowerCase()]?.[0] ?? DEMO_ONLY[kw.toLowerCase()] ?? (40 + [...kw].reduce((a, c) => a + c.charCodeAt(0) * 7, 0) % 900);
  const sumReasons = (o) => RORDER.filter((k) => o?.[k]).map((k) => `${k} ${o[k]}`).join("、");
  const FIN = ["done", "canceled", "failed"]; // 批次已结束（停止/失败的批次也保留已通过的链接）
  const KW_MAX = 200;
  // 防重复点击：同一动作进行中再点/回车直接忽略（QA 2026-10-19 #1：连点“查询数量”会建 2 个任务）
  // 037 抓取队列（竖向列表，见 selqueue.js）：本页任务的状态被队列按钮改了 → 刷新本页
  const DEMO_KWLOG = DEMO ? [ // 演示数字
    { keyword: "acrylic decoration", queried_at: "2026-10-18T02:10:00Z", source: "sel_count" }, { keyword: "Acrylic  Decoration", queried_at: "2026-10-12T06:30:00Z", source: "sel_count" },
    { keyword: "wedding sign", queried_at: "2026-10-09T03:00:00Z", source: "backfill_batches" }] : [];
  async function exportKwLog() {
    let rows = DEMO_KWLOG;
    if (!DEMO) { const r = await getSb().from("sel_keyword_log").select("keyword,queried_at,source").order("queried_at", { ascending: false }).limit(10000); if (r.error) return toast(`导出失败：${r.error.message}`, "bad"); rows = r.data; }
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([kwLogCsv(rows)], { type: "text/csv" })); a.download = `关键词查询记录_${rows.length}.csv`; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }
  const SQ = createSelQueue({ $, esc, toast, getSb, DEMO, confirmDialog, friendlyErr, onChange: (id) => { if (V.run?.id === id) load(id); } });
  async function guard(key, fn) { if (V.busy.has(key)) return; V.busy.add(key); try { return await fn(); } finally { V.busy.delete(key); } }

  // ---------- 演示（不连 Supabase） ----------
  function demoCount(kw) {
    const id = `demo-${Date.now()}`, run = { id, keyword: kw, status: "counting", total_count: null, batch_size: BATCH, current_batch: 0, remaining_count: null, measured: !!MEASURED[kw.toLowerCase()] };
    V.demoRuns[id] = { run, batches: [], links: {} }; location.hash = `#/selscreen/${id}`;
    const ms = Number(QSP.get("countms")) || 1000; run.progress = { stage: "count_open", at: new Date().toISOString() };
    [["count_filter", 0.3], ["count_read", 0.7]].forEach(([st, f]) => setTimeout(() => { if (run.status === "counting") { run.progress = { stage: st, at: new Date().toISOString() }; refresh(); } }, ms * f));
    setTimeout(() => {
      if (run.status === "canceled") return;
      run.status = "counted"; run.total_count = demoTotal(kw); run.progress = { stage: "count_done", total: run.total_count, at: new Date().toISOString() }; refresh();
      // 052：演示也自动入队（lim=min(n, MAX_ROWS)）；0 条保持 counted
      if (run.total_count > 0) {
        const lim = Math.min(run.total_count, MAX_ROWS), e = Math.min(lim, BATCH);
        Object.assign(run, { max_rows: lim, status: "queued", current_batch: 1, queue_at: new Date().toISOString() });
        const bat = { id: `${run.id}-b1`, batch_no: 1, offset_start: 1, offset_end: e, status: "queued", scraped_count: 0, passed_count: 0, excluded_count: 0, demo: true };
        (V.demoRuns[run.id] ||= { run, batches: [], links: {} }).batches = [bat];
        if (QSP.has("demoqueue")) SQ.demoAdd({ id: run.id, keyword: run.keyword, status: "queued", max_rows: lim, current_batch: 1, queue_at: run.queue_at, total_count: run.total_count }, bat);
        refresh();
      }
    }, ms);
  }
  const demoLinks = (run, bno, n) => { const seed = run.keyword.length * 7919 + bno * 104729; return Array.from({ length: n }, (_, i) => `https://www.etsy.com/listing/${9000000000 + ((seed + i * 7121) % 99999999)}`); };
  function demoBatch(run, bno, resume, s0) {
    const D = V.demoRuns[run.id], s = resume ? resume.offset_start : s0 || (D.batches.at(-1)?.offset_end || 0) + 1, e = resume ? resume.offset_end : Math.min(s + BATCH - 1, run.max_rows);
    if (resume) { Object.assign(resume, { status: "running" }); run.status = "running"; }
    const b = resume || { id: `${run.id}-b${bno}`, batch_no: bno, offset_start: s, offset_end: e, status: "running", scraped_count: 0, passed_count: 0, excluded_count: 0, excluded_by_reason: {}, copied_at: null, demo: run.keyword.toLowerCase() !== "acrylic decoration" }; // 只有 acrylic decoration 有回放真数据，其余批次结果都是演示数字
    if (!resume) D.batches.push(b); Object.assign(run, { status: "running", current_batch: bno }); refresh();
    const real = run.keyword.toLowerCase() === "acrylic decoration";
    const total = e - s + 1, step = Math.max(1, Math.ceil(total / 30));
    const tick = () => {
      if (run.status === "canceled") { b.status = "canceled"; D.links[b.id] = demoLinks(run, bno, Math.round(b.scraped_count * 0.35)); b.passed_count = D.links[b.id].length; return refresh(); }
      b.scraped_count = Math.min(total, b.scraped_count + step);
      if (QSP.has("failat") && b.scraped_count >= Number(QSP.get("failat")) && !b.failedOnce) { // 演示：批次中途失败（如 box 与数据库连接中断）
        b.failedOnce = true; Object.assign(b, { status: "failed" }); Object.assign(run, { status: "failed", error: "Remote end closed connection without response" });
        D.links[b.id] = demoLinks(run, bno, Math.round(b.scraped_count * 0.35)); b.passed_count = D.links[b.id].length; return refresh(); }
      const rd = Math.min(total, b.scraped_count + Math.min(step * 2, total - b.scraped_count)); // 演示：读在前、豆包判断在后
      run.progress = { stage: b.scraped_count < total ? (rd >= total ? "vision" : "reading") : "done", batch_no: bno, target: total, read: rd, judged: b.scraped_count, vision_pending: rd - b.scraped_count,
        passed: Math.round(b.scraped_count * 0.35), excluded: b.scraped_count - Math.round(b.scraped_count * 0.35), page: Math.ceil((s - 1 + rd) / 20), at: new Date().toISOString() };
      if (b.scraped_count < total) { b.passed_count = run.progress.passed; b.excluded_count = run.progress.excluded; refresh(); return setTimeout(tick, Number(QSP.get("speed")) || 120); }
      if (real) { // 回放数据：20 行 → 规则排除 13（定制 11、IP 1、素材 1）→ 7 条过豆包 → 保留 6 + 待确认 1
        const items = V.fx.items["demo-run"].filter((x) => x.sales_7d >= FILTERS.min_sales_7d);
        const ok = items.filter((x) => x.verdict !== "排除"), ex = items.filter((x) => x.verdict === "排除");
        D.links[b.id] = ok.sort((a, c) => a.ranks[0] - c.ranks[0]).map((x) => `https://www.etsy.com/listing/${x.etsy_listing_id}`);
        ex.forEach((x) => { const g = /定制/.test(x.reason) ? "定制" : x.reason === "IP" ? "IP" : /材质/.test(x.reason) ? "材质" : /数字|素材/.test(x.reason) ? "素材/数字文件" : "其他"; b.excluded_by_reason[g] = (b.excluded_by_reason[g] || 0) + 1; });
        b.excluded_count = ex.length;
      } else { // 演示数字：按回放比例生成（约 35% 符合要求），链接为演示 ID
        const pass = Math.round(total * 0.35), exn = total - pass, seed = run.keyword.length * 7919 + bno * 104729;
        D.links[b.id] = demoLinks(run, bno, pass);
        if (QSP.has("dupdemo") && bno > 1) D.links[b.id].splice(0, 5, ...(D.links[D.batches[0].id] || []).slice(0, 5)); // 演示：跨批重复
        b.excluded_by_reason = { "定制": Math.round(exn * 0.86), "IP": Math.round(exn * 0.06), "材质": Math.round(exn * 0.05) }; b.excluded_by_reason["素材/数字文件"] = exn - b.excluded_by_reason["定制"] - b.excluded_by_reason["IP"] - b.excluded_by_reason["材质"];
        b.excluded_count = exn;
      }
      b.passed_count = D.links[b.id].length; b.status = "done";
      run.remaining_count = Math.max(0, run.max_rows - e); // 只在批次完成时更新
      if (run.remaining_count <= 0) run.status = "succeeded";
      else if (run.read_all) return (refresh(), setTimeout(() => demoBatch(run, bno + 1, null, e + 1), 400));
      else run.status = "batch_done";
      refresh();
    };
    setTimeout(tick, 400);
  }
  // ---------- 动作 ----------
  async function count() {
    const kw = normKw($("ssKw").value); if (!kw) return toast("请输入关键词", "bad");
    if (kw.length > KW_MAX) return toast(`关键词太长（${kw.length} 字），最多 ${KW_MAX} 字`, "bad");
    $("ssKw").value = kw; const btn = $("ssCount"); if (btn) btn.disabled = true; V.counting = true;
    try { return await countInner(kw, btn); } finally { V.counting = false; const b2 = $("ssCount"); if (b2) b2.disabled = false; }
  }
  async function countInner(kw, btn) {
    // 043：一次索引查询；RPC 不存在（043 未执行）或出错时不拦提交
    let seen = [];
    if (DEMO) seen = DEMO_KWLOG.filter((r) => kwKey(r.keyword) === kwKey(kw)).map((r) => r.queried_at);
    else { const s = await getSb().rpc("sel_keyword_seen", { p_keyword: kw }); if (!s.error && Array.isArray(s.data)) seen = s.data; }
    if (seen.length && !await confirmDialog({ title: "这个关键词查询过", ok: "仍然查询", body: `<p class="warn ss-seen">${esc(seenWarn(seen))}</p><p class="hint">共 ${seen.length} 次（新的在前）。仍可继续查询。${DEMO ? "</p><p><span class=\"pill pill-bad\">演示数字</span>" : ""}</p>` })) { if (btn) btn.disabled = false; return; }
    if (DEMO) { DEMO_KWLOG.unshift({ keyword: kw, queried_at: new Date().toISOString(), source: "sel_count" }); return demoCount(kw); }
    const { data, error } = await getSb().rpc("sel_count", { p_keyword: kw, p_params: FILTERS });
    if (error) {
      if (btn) btn.disabled = false;
      if (/已有正在查询数量的任务/.test(error.message)) return toast("另一个关键词正在查询数量，请稍等几十秒再查（后台更新 048 后可直接排队）", "bad");
      if (/已有进行中的任务/.test(error.message)) { // 带过去看正在进行的那个，而不是只报错
        const a = await getSb().from("sel_runs").select("id,keyword").in("status", ["count_queued", "counting", "queued", "running"]).order("created_at", { ascending: false }).limit(1).maybeSingle();
        if (a.data?.id) { toast(`已有进行中的任务「${a.data.keyword}」，已为你打开；可以先停止它`, "bad"); location.hash = `#/selscreen/${a.data.id}`; return; }
      }
      return toast(`查询失败：${error.message}`, "bad");
    }
    location.hash = `#/selscreen/${data}`;
  }
  function limitError() {
    const r = V.run, max = Math.min(r?.total_count || 0, MAX_ROWS), raw = ($("ssLimit")?.value || "").trim(), v = Number(raw);
    if (!max) return "结果数为 0，无法抓取";
    if (!raw || !Number.isInteger(v) || v < 1 || v > max) return `请输入 1–${max.toLocaleString()} 之间的整数`;
    return "";
  }
  async function start() {
    const err = limitError(); V.limErr = err;
    if (err) { const el = $("ssLimErr"); if (el) el.textContent = err; $("ssLimit")?.classList.add("input-bad"); $("ssLimit")?.focus(); return; }
    const r = V.run, lim = Number($("ssLimit").value);
    if (DEMO && QSP.has("demoqueue")) { const e = Math.min(lim, BATCH); Object.assign(r, { max_rows: lim, status: "queued", current_batch: 1, queue_at: new Date().toISOString() });
      SQ.demoAdd({ id: r.id, keyword: r.keyword, status: "queued", max_rows: lim, current_batch: 1, queue_at: r.queue_at }, { id: `${r.id}-b1`, run_id: r.id, batch_no: 1, offset_start: 1, offset_end: e, status: "queued", scraped_count: 0, passed_count: 0 });
      (V.demoRuns[r.id] ||= { run: r, batches: [], links: {} }).batches = [{ id: `${r.id}-b1`, batch_no: 1, offset_start: 1, offset_end: e, status: "queued", scraped_count: 0, passed_count: 0, excluded_count: 0, demo: true }]; return refresh(); }
    if (DEMO) { r.max_rows = lim; return demoBatch(r, 1); }
    const { data, error } = await getSb().rpc("sel_start", { p_run: r.id, p_limit: lim }); if (error) return toast(`开始失败：${error.message}`, "bad");
    if (data === false) toast("任务状态已变化（可能在另一个标签页已经开始），已刷新", "bad"); load(r.id);
  }
  // 已读到第几条（只看已有批次的 offset_end，不重读）；还能读多少（EHunt 最多前 5,000 条）
  const readEnd = () => V.batches.reduce((a, b) => Math.max(a, b.offset_end || 0), 0);
  const canRead = (r) => Math.max(0, Math.min(r?.total_count || 0, MAX_ROWS) - readEnd());
  function addError() {
    const max = canRead(V.run), raw = ($("ssAdd")?.value || "").trim(), v = Number(raw);
    return !raw || !Number.isInteger(v) || v < 1 || v > max ? `请输入 1–${max.toLocaleString()} 之间的整数` : "";
  }
  function addLabels() { // 改“再读取”条数时只更新按钮文字，不重绘（保留光标）
    V.addErr = ""; $("ssAdd")?.classList.remove("input-bad"); const er = $("ssAddErr"); if (er) er.textContent = "";
    const left = canRead(V.run), re = readEnd(), v = Math.floor(Number($("ssAdd")?.value)); if (!(v >= 1)) return;
    const add = Math.min(v, left), nb = Math.ceil(add / BATCH);
    $("ssNext").textContent = `读取下一批（${(re + 1).toLocaleString()}–${(re + Math.min(add, BATCH)).toLocaleString()}）`;
    const a = $("ssAll"); a.textContent = `一键读取全部（${add.toLocaleString()} 条，${nb} 批）`; a.classList.toggle("hidden", nb <= 1);
  }
  // 继续读取：从已读的下一条开始，每批 1000；all=true 时连续读完填写的条数
  async function next(all) {
    const r = V.run, err = addError(); V.addErr = err;
    if (err) { const el = $("ssAddErr"); if (el) el.textContent = err; $("ssAdd")?.classList.add("input-bad"); $("ssAdd")?.focus(); return; }
    // “读取下一批”只排 1 批，但计划条数按填写的保留（原来传 min(填写,1000)，读完第 2 批就被当成“全部完成”）
    const add = Number($("ssAdd").value), s = readEnd() + 1, n = all ? add : Math.min(add, BATCH), nb = Math.ceil(n / BATCH);
    if (all && nb > 1 && !await confirmDialog({ title: "一键读取全部？", ok: `读取 ${n.toLocaleString()} 条`, body: `<p>将从第 <b>${s.toLocaleString()}</b> 条开始连续读取 <b>${n.toLocaleString()}</b> 条（${nb} 批，每批 1000），已读的 1–${(s - 1).toLocaleString()} 条不会重读。每批完成后结果自动出现在下方；标题规则先筛，只有通过规则的才调用豆包。</p><p class="hint">EHunt 最多只显示前 5,000 条。</p>` })) return;
    if (DEMO) { Object.assign(r, { read_all: all, max_rows: s - 1 + add }); return demoBatch(r, (V.batches.at(-1)?.batch_no || 0) + 1, null, s); }
    const { data, error } = await getSb().rpc("sel_extend", { p_run: r.id, p_add: add, p_all: all }); if (error) return toast(/batch_no_check/.test(error.message) ? "继续读取失败：批次数超过数据库上限 5（需要后台更新 035）" : `继续读取失败：${error.message}`, "bad");
    if (!data) toast("没有可继续读取的条数，或任务状态已变化，已刷新", "bad"); load(r.id);
  }
  async function cancel() {
    const cur = V.batches.find((b) => b.status === "running" || b.status === "queued"), counting = V.run && V.run.total_count == null;
    if (counting) { if (!await confirmDialog({ title: "取消查询？", ok: "取消查询", body: "<p>取消后可以马上换个关键词重新查询（box 暂停时查询会一直排队，取消不影响别的数据）。</p>" })) return; }
    else if (!await confirmDialog({ title: "停止读取？", ok: "停止", body: `<p>停止后，已读到的部分和已通过的链接会保留${cur ? `（第 ${cur.batch_no} 批已读 ${(cur.scraped_count || 0).toLocaleString()} 条）` : ""}，之后可以点「继续本批」从断点接着读。</p>` })) return;
    if (DEMO) { V.run.status = "canceled"; return refresh(); }
    const { data, error } = await getSb().rpc("sel_cancel", { p_run: V.run.id }); if (error) return toast(error.message, "bad");
    if (!data) toast("任务已经结束，无需停止", "bad"); load(V.run.id);
  }
  const lastStopped = () => [...V.batches].reverse().find((x) => x.status === "canceled" || x.status === "failed");
  async function resume() {
    const b = lastStopped(); if (!b) return;
    if (DEMO) { V.run.status = "running"; return demoBatch(V.run, b.batch_no, b); }
    const { data, error } = await getSb().rpc("sel_resume", { p_run: V.run.id }); if (error) return toast(`继续失败：${error.message}`, "bad");
    if (data === false) toast(b.status === "failed" ? "暂时不能重试失败的批次（需要后台更新 035），请重新查询数量后再抓取" : "任务状态已变化，已刷新", "bad"); load(V.run.id);
  }
  // 复制/下载全部时按链接去重（EHunt 实时排序，批次之间可能有重复）
  function linksOf(bid) {
    if (bid !== "all") return { list: V.links[bid] || [], dup: 0 };
    const raw = V.batches.flatMap((b) => V.links[b.id] || []), list = [...new Set(raw)];
    return { list, dup: raw.length - list.length };
  }
  // 送去预筛选（039 sel_send_to_screen：一个事务建预筛选批次（每个 ≤500 条）+ 任务 + 标记本批；服务端按用户串行，已发送的默认跳过 → 连点不会重复）
  async function prescreen(bid, all = false) {
    if (V.sending) return;
    const fin = V.batches.filter((b) => FIN.includes(b.status) && (V.links[b.id] || []).length);
    const plan = sendPlan(all ? fin : fin.filter((b) => b.id === bid), (id) => V.links[id], !all); if (!plan || !plan.ids.length) return toast(all ? "没有可发送的批次" : "本批没有可发送的链接", "bad");
    const quota = "<p class=\"hint\">每条新 listing 消耗 1 次 EHunt 额度（已获取成功的直接沿用，不再重复获取）；每个预筛选批次最多 500 条，超过自动分成多个；预筛选排队+运行中最多 1000 条，超出的部分之后可以点「发送剩余」。</p>";
    let title, ok, body;
    if (all && plan.restoreOnly) { title = "全部批次都已发送"; ok = "恢复已删除的"; body = "<p>不会重复发送。之前在预筛选里删除过的链接（已获取过的）会恢复显示，不重新获取、不耗额度。</p>"; }
    else if (all) { title = "发送全部批次到预筛选？"; ok = `发送 ${plan.n.toLocaleString()} 条`;
      body = `<p>将发送以下批次（已发送过的跳过）：</p><ul class="ss-send-list">${plan.rows.map((x) => `<li>第 ${x.b.batch_no} 批：<b>${x.st.left.toLocaleString()}</b> 条${x.st.partial ? `（剩余，已发 ${x.st.sent}/${x.st.total}）` : ""}</li>`).join("")}</ul>${plan.skipped.length ? `<p class="hint">跳过已发送：${plan.skipped.map((x) => `第 ${x.b.batch_no} 批`).join("、")}</p>` : ""}<p>合计 <b>${plan.n.toLocaleString()}</b> 条（批次之间重复的链接只发一次）。</p>${quota}`; }
    else { const x = plan.rows[0];
      if (x.st.full) { title = `第 ${x.b.batch_no} 批已发送过`; ok = "再发一次（恢复已删除的 + 重新获取失败的）"; body = `<p class="warn">这一批已于 <b>${esc(fmtAt(x.st.at))}</b> 发送过，确定要再发一次？</p><p>已经在预筛选列表里的链接会自动跳过（同一链接只出现一次）；<b>在预筛选里删除过的会恢复显示</b>（不重新获取）。<b>除了获取失败，不重复获取</b>：已获取成功、排队中、处理中的链接直接沿用原结果；只有之前获取失败的会重新获取。</p>${quota}`; }
      else if (x.st.partial) { title = `发送第 ${x.b.batch_no} 批剩余的链接？`; ok = `发送剩余 ${x.st.left} 条`; body = `<p>这一批已于 ${esc(fmtAt(x.st.at))} 发送了 ${x.st.sent}/${x.st.total} 条，将接着发送剩余 <b>${x.st.left}</b> 条。</p>${quota}`; }
      else { title = `把第 ${x.b.batch_no} 批送去预筛选？`; ok = `送去预筛选（${x.st.total} 条）`; body = `<p>将提交 <b>${x.st.total}</b> 条链接为预筛选任务，提交后跳到「预筛选」页。</p>${quota}`; } }
    if (!await confirmDialog({ title, ok, body })) return;
    V.sending = all ? "all" : bid; render();
    try {
      let res, error;
      if (DEMO) res = await demoSend(plan.ids, plan.resend);
      else ({ data: res, error } = await getSb().rpc("sel_send_to_screen", { p_batches: plan.ids, p_resend: plan.resend }));
      if (error) return toast(/Could not find the function|does not exist/.test(error.message) ? "发送失败：需要后台先执行 039 迁移（没有发送任何链接）" : `发送失败（没有发送任何链接）：${error.message}`, "bad");
      for (const r of res?.batches || []) { const b = V.batches.find((x) => x.id === r.batch_id); if (b && (r.sent || r.dup || r.reused)) Object.assign(b, { sent_to_screen_at: r.sent_at, sent_count: r.sent_count }); }
      render(); loadRecent();   // 先标记、刷新，再跳转（同一事务里已写好标记）
      const left = (res?.batches || []).reduce((a, r) => a + Math.max(0, r.total - r.sent_count), 0);
      const o = sendOutcome(res, left); toast(o.text, o.kind);
      if (o.go) { window.dispatchEvent(new CustomEvent("screen:dirty")); if (!DEMO || QSP.has("sendgo")) location.hash = o.go; }
    } catch (e) { toast(`发送失败（没有发送任何链接）：${e.message || e}`, "bad"); }
    finally { V.sending = false; render(); }
  }
  // 演示：与 039 相同的规则（额度 ?sendcap=，耗时 ?sendms=），不连数据库
  async function demoSend(ids, resend) {
    await new Promise((r) => setTimeout(r, Number(QSP.get("sendms")) || 700));
    let cap = QSP.has("sendcap") ? Number(QSP.get("sendcap")) : 1000; const seen = new Set(), out = []; let sent = 0, nb = 0, limited = false, reusedAll = 0; const at = new Date().toISOString();
    const nFail = Number(QSP.get("demofail")) || 3; // 演示：之前发送过的链接里前 N 条“获取失败”，其余视为已获取成功
    for (const id of ids) { const b = V.batches.find((x) => x.id === id), L = V.links[id] || [], st = sendState(b, L.length), d0 = resend ? 0 : st.sent; let n = 0, k = 0, reused = 0;
      for (const [i, u] of L.slice(d0).entries()) { if (seen.has(u)) { n++; continue; }
        if (d0 + i < st.sent && d0 + i >= nFail) { seen.add(u); n++; reused++; continue; } /* 042：除了获取失败，不重复获取 */ if (cap <= 0) { limited = true; break; } if (!k || k >= 500) { nb++; k = 0; } seen.add(u); k++; n++; cap--; sent++; }
      reusedAll += reused; out.push({ batch_id: id, batch_no: b.batch_no, sent: n - reused, reused, sent_count: d0 + n, total: L.length, sent_at: n ? at : b.sent_to_screen_at }); }
    if (!sent && limited) throw new Error("预筛选排队中+运行中的任务已达上限 1000 条，请等预筛选跑完一些后再发送");
    return { batches: out, sent, reused: reusedAll, ...(QSP.has("pre044") ? {} : { restored: reusedAll }), screen_batches: nb, limited }; // 演示：044 起被删除过的复用项恢复显示
  }
  async function copy(bid) {
    const { list, dup } = linksOf(bid);
    try { await navigator.clipboard.writeText(list.join("\n")); } catch { return toast("复制失败，请手动选择", "bad"); }
    toast(`已复制 ${list.length} 条链接${dup ? `（已去重 ${dup} 条）` : ""}`, "ok");
    const bs = (bid === "all" ? V.batches : V.batches.filter((b) => b.id === bid)).filter((b) => FIN.includes(b.status)); // 还在读的批次不标“已复制”
    for (const b of bs) { b.copied_at = new Date().toISOString(); V.open.delete(b.id); if (!DEMO) await getSb().rpc("sel_mark_copied", { p_batch: b.id }); }
    render();
  }
  function download(bid) {
    const { list, dup } = linksOf(bid); if (!list.length) return;
    if (dup) toast(`已去重 ${dup} 条`, "ok");
    const kw = safeName(V.run?.keyword), tag = bid === "all" ? "all" : `b${V.batches.find((b) => b.id === bid)?.batch_no}`;
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([list.join("\n") + "\n"], { type: "text/plain" }));
    a.download = `etsy_${kw}_${tag}_${list.length}.txt`; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }
  // ---------- 最近 3 天（只读列表，不是搜索） ----------
  async function loadRecent() {
    if (DEMO) {
      const since = Date.now() - 3 * 864e5, own = Object.values(V.demoRuns).filter((d) => !d.run.id.startsWith("demo-recent")).map((d) => ({ ...d.run, created_at: d.run.created_at || new Date(Number(d.run.id.slice(5))).toISOString(), passed_count: d.batches.reduce((a, b) => a + (b.passed_count || 0), 0) }));
      const fx = [{ id: "demo-recent-gift", keyword: "gift", status: "succeeded", created_at: new Date(Date.now() - 6 * 36e5).toISOString(), passed_count: 6 }, { id: "demo-recent-1", keyword: "acrylic decoration", status: "succeeded", created_at: new Date(Date.now() - 5 * 36e5).toISOString(), passed_count: 7 },
        { id: "demo-recent-2", keyword: "wedding sign", status: "batch_done", created_at: new Date(Date.now() - 30 * 36e5).toISOString(), passed_count: 312, sel_batches: [{ batch_no: 1, sent_to_screen_at: new Date(Date.now() - 28 * 36e5).toISOString() }] }];
      if (QSP.has("manyhist")) // 演示：14 条历史（只应显示最近 10 条），数字为演示数字
        ["teacher gift", "dog mom", "nurse badge", "christmas ornament", "bookmark", "cake topper", "keychain", "door sign", "plant marker", "earrings", "magnet"].forEach((k, i) =>
          fx.push({ id: `demo-recent-x${i}`, keyword: k, status: ["counted", "running", "succeeded", "canceled", "failed"][i % 5], created_at: new Date(Date.now() - (1 + i * 5) * 36e5).toISOString(), passed_count: 10 + i * 7, total_count: 100 + i * 37, demo: true,
            sel_batches: i === 2 ? [{ batch_no: 1, sent_to_screen_at: new Date(Date.now() - 9 * 36e5).toISOString() }, { batch_no: 2, sent_to_screen_at: new Date(Date.now() - 8 * 36e5).toISOString() }] : i === 5 ? [{ batch_no: 1, sent_to_screen_at: new Date(Date.now() - 20 * 36e5).toISOString() }] : [] }));
      if (QSP.has("batchdemo")) { const h = (x) => new Date(Date.now() - x * 36e5).toISOString(); // 演示：批次命名（数字为演示数字）
        fx.push({ id: "demo-recent-bn1", keyword: "reason", status: "succeeded", created_at: h(20), passed_count: 640, demo: true, sel_batches: [{ batch_no: 1, sent_to_screen_at: h(19) }] },
          { id: "demo-recent-bn2", keyword: "Alex kitchen", status: "succeeded", created_at: h(18), passed_count: 1210, demo: true, sel_batches: [{ batch_no: 1, sent_to_screen_at: h(17) }, { batch_no: 2, sent_to_screen_at: h(16), sent_count: 300, passed_count: 410 }] },
          { id: "demo-recent-bn3", keyword: "reason", status: "succeeded", created_at: h(4), passed_count: 288, demo: true, sel_batches: [{ batch_no: 1, sent_to_screen_at: h(3) }] }); }
      V.recentFx = Object.fromEntries(fx.map((r) => [r.id, r]));
      V.recent = recentList([...own, ...fx].filter((r) => Date.parse(r.created_at) > since && !V.deleted?.has(r.id))); return renderRecent();
    }
    const sb = getSb(); if (!sb) return;
    const q = (cols) => sb.from("sel_runs").select(cols).gte("created_at", new Date(Date.now() - 3 * 864e5).toISOString()).order("created_at", { ascending: false }).limit(RECENT_N);
    let { data, error } = await q("id,keyword,status,created_at,passed_count,total_count,sel_batches(batch_no,sent_to_screen_at,sent_count,passed_count,auto_sent_at)"); // 046 auto_sent_at
    if (error) ({ data, error } = await q("id,keyword,status,created_at,passed_count,total_count,sel_batches(batch_no,sent_to_screen_at,sent_count,passed_count)")); // 同一次请求带上各批发送状态（039 sent_count：部分发送）
    if (error) ({ data, error } = await q("id,keyword,status,created_at,passed_count,total_count,sel_batches(batch_no,sent_to_screen_at)")); // 039 未执行
    if (error) ({ data, error } = await q("id,keyword,status,created_at,passed_count,total_count")); // 038 未执行：没有 sent_to_screen_at 列 → 全部显示未发送
    V.recent = error ? [] : data || []; renderRecent();
  }
  function renderRecent() {
    const el = $("ssRecent"); if (!el) return; const L = V.recent || [];
    const ST = { count_queued: "查询中", counting: "查询中", counted: "待抓取", queued: "排队中", running: "读取中", batch_done: "本批完成", succeeded: "完成", failed: "失败", canceled: "已停止" };
    const t = (iso) => { const d = new Date(iso); return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
    if (!L.length) { el.innerHTML = ""; return; }
    const sbOf = (r) => { const D = V.demoRuns[r.id]; return D && !D.fxOnly ? D.batches : r.sel_batches; }, nos = historySendNos(L.map((r) => ({ ...r, sel_batches: sbOf(r) })));
    const open = recentOpen(globalThis.localStorage, globalThis.matchMedia?.("(max-width:560px)").matches), demo = L.some((r) => r.demo) ? ' <span class="pill pill-bad">演示数字</span>' : "";
    el.innerHTML = `<div class="ss-recent${open ? " open" : ""}"><button type="button" class="ss-rhead" data-ssrtog aria-expanded="${open}" aria-label="最近 3 天查找记录（${L.length}）" title="最近 3 天查找记录（${L.length}）">${demo}<span class="spacer"></span><span class="ss-rtog">${open ? "收起 ▴" : `展开（${L.length}）▾`}</span></button>
      ${open ? `<div class="ss-rlist">${L.map((r) => { const busy = ACTIVE_ST.includes(r.status);
        return `<div class="ss-rrow${V.run?.id === r.id ? " active" : ""}"><a class="ss-rlink" href="#/selscreen/${esc(r.id)}" title="${esc(ST[r.status] || r.status)}"${V.run?.id === r.id ? ' aria-current="true"' : ""}><b class="ss-rkw">${esc(r.keyword)}</b><span class="ss-rtime hint">${t(r.created_at)}</span><span class="ss-rst">${esc(chipText(r))}</span>${(() => { const si = sentInfo(sbOf(r), r.keyword, nos, r.id); return si.sent ? `<span class="ss-rsents">${si.tags.map((t, i) => `<span class="pill pill-ok ss-rsent" title="${esc(si.tip)}">${esc(i === si.tags.length - 1 ? si.text : t)}</span>`).join("")}</span>` : `<span class="pill pill-muted ss-rsent" title="${esc(si.tip)}">${esc(si.text)}</span>`; })()}</a><button type="button" class="ss-rdel" data-ssrdel="${esc(r.id)}"${busy ? ' disabled title="进行中的任务不能删除，请先停止"' : ' title="删除这条记录"'} aria-label="删除 ${esc(r.keyword)}">🗑</button></div>`; }).join("")}</div>` : ""}</div>`;
  }
  // 删除一条查找记录（连同批次和链接）。进行中（查询中/排队中/读取中）的不能删：按钮禁用，需先停止（RPC 也会拒绝）
  async function delRecent(id) {
    const r = (V.recent || []).find((x) => x.id === id); if (!r) return;
    if (ACTIVE_ST.includes(r.status)) return toast("进行中或已暂停的任务不能删除，请先停止", "bad");
    if (!await confirmDialog({ title: "删除这条查找记录？", ok: "删除", body: `<p>将删除「<b>${esc(r.keyword)}</b>」这次查找的全部结果（${esc(chipText(r))}），包括已通过的链接，删除后不能恢复。</p><p class="hint">已送去预筛选的任务不受影响。</p>` })) return;
    if (DEMO) { (V.deleted ||= new Set()).add(id); delete V.demoRuns[id]; }
    else {
      const { data, error } = await getSb().rpc("sel_delete_run", { p_run: id });
      if (error) return toast(/sel_delete_run|function .* does not exist|schema cache/i.test(error.message) ? "删除功能需要后台更新 038（尚未执行）" : `删除失败：${error.message}`, "bad");
      if (!data) { toast("这条记录已不存在或已被删除", "bad"); }
    }
    toast(`已删除「${r.keyword}」`, "ok");
    if (V.run?.id === id) { V.run = null; V.batches = []; V.links = {}; location.hash = "#/selscreen"; }
    loadRecent();
  }
  // ---------- 数据 ----------
  function refresh() { if (DEMO && V.run) { const D = V.demoRuns[V.run.id]; if (D) { V.batches = D.batches; V.links = D.links; } } render(); }
  function demoRecentRun(id) { // 演示：最近列表里的固定条目
    if (V.demoRuns[id] || !id?.startsWith("demo-recent")) return;
    if (id === "demo-recent-gift") { // 与线上 gift 相同：19,893 条只读了前 200 条，已完成
      const run = { id, keyword: "gift", status: "succeeded", total_count: 19893, max_rows: 200, batch_size: BATCH, current_batch: 1, remaining_count: 0, created_at: new Date(Date.now() - 6 * 36e5).toISOString() };
      const b = { id: `${id}-b1`, batch_no: 1, offset_start: 1, offset_end: 200, status: "done", scraped_count: 200, passed_count: 6, excluded_count: 194, excluded_by_reason: { "定制": 170, "IP": 14, "材质": 10 }, demo: true };
      V.demoRuns[id] = { run, batches: [b], links: { [b.id]: demoLinks(run, 1, 6) } }; return;
    }
    const fxr = V.recentFx?.[id];
    if (fxr && !["demo-recent-1", "demo-recent-2"].includes(id)) { V.demoRuns[id] = { fxOnly: true, run: { ...fxr, batch_size: BATCH, current_batch: 0, max_rows: null, remaining_count: null, measured: false }, batches: [], links: {} }; return; } // 演示历史：只显示数量
    const kw = id === "demo-recent-1" ? "acrylic decoration" : "wedding sign", n = id === "demo-recent-1" ? 20 : 1400;
    const run = { id, keyword: kw, status: id === "demo-recent-1" ? "succeeded" : "batch_done", total_count: n, max_rows: n, batch_size: BATCH, current_batch: 1, remaining_count: n - Math.min(n, BATCH), created_at: new Date(Date.now() - (id === "demo-recent-1" ? 5 : 30) * 36e5).toISOString() };
    const pass = id === "demo-recent-1" ? 7 : 312, b = { id: `${id}-b1`, batch_no: 1, offset_start: 1, offset_end: Math.min(n, BATCH), status: "done", scraped_count: Math.min(n, BATCH), passed_count: pass, excluded_count: Math.min(n, BATCH) - pass, excluded_by_reason: { "定制": Math.min(n, BATCH) - pass }, demo: true };
    (V.recentFx?.[id]?.sel_batches || []).forEach((x) => { if (x.batch_no === b.batch_no) Object.assign(b, { sent_to_screen_at: x.sent_to_screen_at, sent_count: x.sent_count ?? null }); });
    V.demoRuns[id] = { run, batches: [b], links: { [b.id]: demoLinks(run, 1, pass) } };
  }
  // 演示截图：?demo=1&sprog=counting|counted|zero|reading|vision|done|failed#/selscreen/demo-prog（数字都是演示数字）
  function demoProgress(kind) {
    const id = "demo-prog", now = Date.now(), iso = (s) => new Date(now - s * 1000).toISOString();
    const run = { id, keyword: "acrylic sign", status: "running", total_count: 3260, max_rows: 2000, batch_size: BATCH, current_batch: 2, remaining_count: 1000, created_at: iso(900), heartbeat_at: iso(3), demoProgress: true };
    const b1 = { id: `${id}-b1`, batch_no: 1, offset_start: 1, offset_end: 1000, status: "done", scraped_count: 1000, passed_count: 352, excluded_count: 641, dup_count: 7, excluded_by_reason: { "定制": 551, "IP": 38, "材质": 31, "素材/数字文件": 21 }, finished_at: iso(400), copied_at: iso(300), demo: true };
    const b2 = { id: `${id}-b2`, batch_no: 2, offset_start: 1001, offset_end: 2000, status: "running", scraped_count: 0, passed_count: 0, excluded_count: 0, excluded_by_reason: {}, demo: true };
    let batches = [b1, b2];
    if (kind === "counting") { Object.assign(run, { status: "counting", total_count: null, max_rows: null, current_batch: 0 }); batches = []; run.progress = { stage: "count_filter", at: iso(2) }; }
    else if (kind === "counted") { // 052 截图：查完待自动入队（演示数字）
      Object.assign(run, { status: "counted", total_count: 3260, max_rows: null, current_batch: 0 }); batches = []; run.progress = { stage: "count_done", total: 3260, at: iso(1) };
    } else if (kind === "zero") { // 052：0 条不自动抓
      Object.assign(run, { status: "counted", keyword: "no results", total_count: 0, max_rows: null, current_batch: 0 }); batches = []; run.progress = { stage: "count_done", total: 0, at: iso(1) };
    }
    else if (kind === "reading" || kind === "vision") {
      const P = kind === "reading" ? { stage: "reading", read: 468, judged: 431, vision_pending: 37, passed: 149, excluded: 279, dup: 3, page: 74 } : { stage: "vision", read: 1000, judged: 962, vision_pending: 38, passed: 333, excluded: 621, dup: 8, page: 100 };
      Object.assign(b2, { scraped_count: P.judged, passed_count: P.passed, excluded_count: P.excluded }); run.progress = { ...P, batch_no: 2, target: 1000, at: iso(2) };
    } else if (kind === "done") {
      Object.assign(b2, { status: "done", scraped_count: 1000, passed_count: 341, excluded_count: 651, dup_count: 8, excluded_by_reason: { "定制": 560, "IP": 41, "材质": 29, "素材/数字文件": 21 }, finished_at: iso(20) });
      const sf = QSP.get("sendfx"); if (sf === "sent") Object.assign(b1, { sent_to_screen_at: iso(3 * 3600) }); else if (sf === "partial") Object.assign(b1, { sent_to_screen_at: iso(3600), sent_count: 200 }); else if (sf === "auto") Object.assign(b1, { sent_to_screen_at: iso(3600), sent_count: 352, auto_sent_at: iso(3600) }), Object.assign(b2, { sent_to_screen_at: iso(20), sent_count: 341, auto_sent_at: iso(20) }); else if (sf === "autopartial") Object.assign(b1, { sent_to_screen_at: iso(3600), sent_count: 352, auto_sent_at: iso(3600) }), Object.assign(b2, { sent_to_screen_at: iso(20), sent_count: 120, auto_sent_at: iso(20), auto_pending: true }); // 演示：已发送 / 部分发送
      Object.assign(run, { status: "succeeded", remaining_count: 0, progress: { stage: "done", batch_no: 2, target: 1000, read: 1000, judged: 1000, vision_pending: 0, passed: 341, excluded: 651, at: iso(20) } });
    } else if (kind === "failed") {
      Object.assign(b2, { status: "failed", scraped_count: 286, passed_count: 97, excluded_count: 187, finished_at: iso(60) });
      Object.assign(run, { status: "failed", error: "EHunt 登录已失效（页面顶栏显示 Login/Register，搜索弹出登录提示）：需要在 box 上运行 deploy/box-selection-worker.sh login 重新人工登录", progress: { stage: "reading", batch_no: 2, target: 1000, read: 300, judged: 286, vision_pending: 14, passed: 97, excluded: 187, page: 65, at: iso(60) } });
    }
    const links = Object.fromEntries(batches.filter((b) => b.status !== "running").map((b) => [b.id, demoLinks(run, b.batch_no, b.passed_count)]));
    V.demoRuns[id] = { run, batches, links };
  }
  function schedule() { clearTimeout(V.timer); if (!DEMO && active(V.run) && location.hash.startsWith("#/selscreen")) V.timer = setTimeout(() => load(V.run.id), 3000); }
  async function load(runId) {
    SQ.load();
    if (DEMO) { V.fx ||= (await import("./demo-selection.js")).selectionFixtures(); if (runId === "demo-prog" && !V.demoRuns[runId]) demoProgress(QSP.get("sprog") || "reading"); demoRecentRun(runId); loadRecent(); V.run = V.demoRuns[runId]?.run || null; return refresh(); }
    const sb = getSb(); if (!sb) return;
    const seq = ++V.seq, stale = () => seq !== V.seq; // 快速切换标签/轮询时，旧请求晚到不能覆盖当前任务
    if (!runId) { V.run = null; V.batches = []; V.links = {}; loadRecent(); return render(); }
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(runId)) { V.run = null; V.batches = []; V.links = {}; loadRecent(); render(); $("ssBody").innerHTML = '<div class="card empty"><h3>链接里的任务编号无效</h3><p class="muted">请从上方“最近 3 天”重新打开，或输入关键词重新查询</p></div>'; return; }
    const r = await sb.from("sel_runs").select("*").eq("id", runId).maybeSingle(); if (stale()) return;
    if (r.error) { $("ssBody").innerHTML = `<div class="card empty">读取任务失败：${esc(r.error.message)}</div>`; return; }
    if (!r.data) { V.run = null; V.batches = []; V.links = {}; loadRecent(); render(); $("ssBody").innerHTML = '<div class="card empty"><h3>没有找到这个任务</h3><p class="muted">可能已超过 3 天被自动清理，或不属于当前账号</p></div>'; return; }
    if (!V.recent || V.lastSt !== `${runId}:${r.data.status}`) loadRecent(); // 状态变化时刷新“最近 3 天”（原来进行中不刷新，标签一直显示旧状态）
    V.lastSt = `${runId}:${r.data.status}`;
    const b = await sb.from("sel_batches").select("*").eq("run_id", runId).order("batch_no"); if (stale()) return;
    // sel_items 分页读取：PostgREST 默认一次最多返回 1,000 行，>1000 条符合要求时原来会静默少显示/少复制
    const links = {}; let rows = [];
    for (let off = 0; ; off += 1000) {
      const it = await sb.from("sel_items").select("batch_id,etsy_url,rank").eq("run_id", runId).order("rank").order("id").range(off, off + 999); if (stale()) return;
      if (it.error) { toast(`读取链接失败：${it.error.message}`, "bad"); break; }
      rows = rows.concat(it.data || []); if ((it.data || []).length < 1000) break;
    }
    rows.forEach((x) => (links[x.batch_id] ||= []).push(x.etsy_url));
    V.run = r.data; V.batches = b.data || []; V.links = links;
    render(); schedule();
  }
  // ---------- 实时进度（034 sel_runs.progress，box worker 每 ≤2 秒写一次；未部署时回退到 sel_batches 计数） ----------
  const CSTEPS = [["count_open", "打开 EHunt 产品研究页"], ["count_filter", "设置关键词与筛选"], ["count_read", "读取结果数"]];
  const n0 = (v) => Number(v || 0).toLocaleString();
  const ago = (t) => { const s = Math.max(0, Math.round((Date.now() - Date.parse(t || "")) / 1000)); return Number.isFinite(s) ? (s < 60 ? `${s} 秒前` : `${Math.round(s / 60)} 分钟前`) : ""; };
  const STALE_S = 180;
  function progressPanel(r) {
    if (!r) return "";
    const P = r.progress || {}, cur = V.batches.find((b) => b.status === "running" || b.status === "queued") || null;
    const last = [...V.batches].reverse().find((b) => ["done", "failed", "canceled"].includes(b.status)) || null;
    const demo = DEMO && (r.demoProgress || V.batches.some((b) => b.demo)) ? '<span class="pill pill-bad">演示数字</span>' : "";
    const at = P.at || r.heartbeat_at, stale = active(r) && !["count_queued", "queued"].includes(r.status) && at && (Date.now() - Date.parse(at)) / 1000 > STALE_S;
    const upd = at && active(r) ? `<span class="hint ss-pg-at${stale ? " warn" : ""}" title="box 抓取进程最后一次写入进度">${stale ? `⚠ box 抓取进程 ${ago(at)}没有更新进度` : `更新于 ${ago(at)}`}</span>` : "";
    const stop = active(r) ? `<button class="btn btn-ghost btn-sm" id="ssStop">${["queued", "running"].includes(r.status) ? "■ 停止" : "取消查询"}</button>` : ""; // 查询阶段也能取消（QA #7）
    const errH = r.error ? `<span class="warn" title="${esc(r.error)}">${esc(friendlyErr(r.error))}</span>` : "";
    const bar = (rd, jd, cls = "") => `<div class="ss-pg-bar ${cls}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(jd)}"><i class="rd" style="width:${Math.min(100, rd)}%"></i><i class="jd" style="width:${Math.min(100, jd)}%"></i></div>`;
    const wrap = (cls, pill, step, barH, stats) => `<div class="ss-pg ${cls}"><div class="ss-pg-top">${pill}<span class="ss-pg-step">${step}</span>${demo}<span class="spacer"></span>${upd}${stop}</div>${barH}${stats ? `<div class="ss-pg-stats">${stats}</div>` : ""}</div>`;
    // ① 查询数量
    if (r.status === "count_queued" || r.status === "counting") {
      const i = r.status === "count_queued" ? -1 : Math.max(0, CSTEPS.findIndex(([k]) => k === P.stage));
      const steps = CSTEPS.map(([, t], k) => `<span class="ss-pg-dot${k < i ? " ok" : k === i ? " on" : ""}">${k < i ? "✓" : k + 1}</span><span class="${k === i ? "" : "hint"}">${t}</span>`).join('<span class="ss-pg-sep">›</span>');
      const pct = i < 0 ? 3 : [15, 50, 85][i];
      const w = waitedMin(r.created_at), wt = w >= 2 ? `<span class="warn">已等待 ${w} 分钟（box 可能暂停，恢复后自动继续；也可以取消后重试）</span>` : "";
      return wrap("ss-pg-run", `<span class="pill pill-info"><span class="spin"></span> ${i < 0 ? "排队查询数量（等待 box 抓取进程）" : "查询数量中"}</span>`, i < 0 ? wt : `第 ${i + 1}/3 步`, bar(pct, pct, "ss-pg-ind"), `<span class="ss-pg-steps">${steps}</span>`);
    }
    if (r.status === "paused") return wrap("ss-pg-paused", '<span class="pill pill-warn">⏸ 已暂停</span>', "不会被抓取；在上方「抓取队列」点「继续」从断点接着读", "", "");
    // ② 抓取（本批）
    const b = cur || (["batch_done", "succeeded", "failed"].includes(r.status) ? last : null);
    if (!b) return r.status === "canceled" ? (r.error ? wrap("ss-pg-bad", '<span class="pill pill-bad">已停止</span>', errH, "", "") : "") : r.status === "failed" ? wrap("ss-pg-bad", '<span class="pill pill-bad">失败</span>', errH, "", "") : "";
    const same = P.batch_no == null || P.batch_no === b.batch_no;   // progress 只描述当前批
    const tot = b.offset_end - b.offset_start + 1, target = same && P.target ? P.target : tot;
    const judged = same && P.judged != null ? Math.max(P.judged, b.scraped_count || 0) : b.scraped_count || 0;
    const read = Math.max(judged, same && P.read != null ? P.read : judged), vp = same ? P.vision_pending || 0 : 0;
    const passed = same && P.passed != null ? Math.max(P.passed, b.passed_count || 0) : b.passed_count || 0, excl = same && P.excluded != null ? Math.max(P.excluded, b.excluded_count || 0) : b.excluded_count || 0;
    const pctR = target ? (100 * read) / target : 0, pctJ = target ? (100 * judged) / target : 0, pct = Math.floor(pctJ);
    const doneR = V.batches.filter((x) => x !== b && x.status === "done").reduce((a, x) => a + (x.scraped_count || 0), 0), maxR = r.max_rows || 0;
    const overall = maxR > tot ? ` · 总进度 已读 <b>${n0(doneR + read)}</b> / 最多读取 ${n0(maxR)} 条` : "";
    const nums = `已读 <b>${n0(read)}</b> / 目标 <b>${n0(target)}</b> 条 · 已判断 ${n0(judged)}${vp ? ` · <span class="ss-pg-v">豆包判断中 ${n0(vp)}</span>` : ""} · 通过 <b class="ss-n-ok">${n0(passed)}</b> · 排除 ${n0(excl)}${overall}`;
    const range = `第 ${b.batch_no} 批（${b.offset_start.toLocaleString()}–${b.offset_end.toLocaleString()}）`;
    if (b.status === "failed" || r.status === "failed") {
      const where = { read_setup: "打开 EHunt / 设置筛选", reading: `抓取中（第 ${P.page || "?"} 页）`, page_retry: `第 ${P.page || "?"} 页重试中`, vision: "调豆包判断" }[P.stage] || "抓取";
      const numsF = nums.replace(/ · <span class="ss-pg-v">豆包判断中 [\d,]+<\/span>/, vp ? ` · 未判断 ${n0(vp)}` : "");   // 已停止：不再说“判断中”
      return wrap("ss-pg-bad", '<span class="pill pill-bad">失败</span>', `${range} · 停在「${where}」 ${pct}%${errH ? ` · ${errH}` : ""}`, bar(pctR, pctJ, "ss-pg-fail"), numsF);
    }
    if (b.status === "done") {
      const all = r.status === "succeeded";
      return wrap("ss-pg-ok", `<span class="pill pill-ok">✓ ${all ? "全部完成" : "本批完成"}</span>`, `${range} · 100%${b.finished_at ? ` · ${new Date(b.finished_at).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false })} 完成` : ""}`, bar(100, 100, "ss-pg-done"),
        `已读 <b>${n0(b.scraped_count)}</b> / 目标 <b>${n0(tot)}</b> 条 · 通过 <b class="ss-n-ok">${n0(b.passed_count)}</b> · 排除 ${n0(b.excluded_count)}${b.dup_count ? ` · 重复 ${n0(b.dup_count)}` : ""}${maxR > tot ? ` · 总进度 已读 ${n0(doneR + (b.scraped_count || 0))} / 最多读取 ${n0(maxR)} 条` : ""}`);
    }
    const bw = b.status === "queued" && !b.started_at ? waitedMin(b.created_at) : 0;
    const stg = r.status === "queued" || b.status === "queued" ? ["排队中（等待 box 抓取进程）", bw >= 2 ? `<span class="warn">已等待 ${bw} 分钟，box 可能暂停，恢复后自动开始</span>` : ""]
      : P.stage === "vision" ? ["调豆包判断中", `列表已读完，剩 ${n0(vp || Math.max(0, read - judged))} 条等豆包判断`]
      : P.stage === "read_setup" || (!P.stage && !judged) ? ["打开 EHunt · 设置筛选中", P.page > 1 ? `从第 ${P.page} 页续读` : ""]
      : ["抓取中", P.page ? `第 ${P.page} 页${vp ? ` · 同时豆包判断 ${n0(vp)} 条` : ""}` : ""];
    if (P.retry > 0 && (P.stage === "page_retry" || P.stage === "read_setup" || P.stage === "reading"))   // worker 单页超时自动重试中
      stg[1] = `<span class="warn">第 ${P.page || "?"} 页${P.retry_why === "翻页卡住" ? "翻页卡住" : P.retry_why === "关键词未生效" ? "关键词未生效" : "读取超时"}，正在重新打开重试（第 ${P.retry}${P.retry_max ? `/${P.retry_max}` : ""} 次）</span>`;
    return wrap("ss-pg-run", `<span class="pill pill-info"><span class="spin"></span> ${stg[0]}</span>`, `${range} · <b>${pct}%</b>${stg[1] ? ` · ${stg[1]}` : ""}`, bar(pctR, pctJ), nums);
  }
  // ---------- 界面 ----------
  function countPanel(r) {
    const locked = `<div class="ss-form"><label class="hint">最多读取 <input class="input input-sm ss-thr" type="number" disabled placeholder="—"> 条</label><button class="btn btn-primary" disabled title="先查询数量">抓取 →</button><span class="hint">先查询数量，显示结果数后才能抓取</span></div>`;
    if (!r || r.total_count == null) return `<div class="ss-count">${r && active(r) ? `<p class="hint">正在查询 EHunt「${esc(r.keyword)}」的结果数（进度见下方）</p>` : ""}${locked}</div>`;
    const n = r.total_count, nb = Math.ceil(Math.min(n, MAX_ROWS) / BATCH);
    const label = DEMO ? (MEASURED[r.keyword.toLowerCase()] ? `<span class="pill pill-ok">EHunt 实测（10-16 ${esc(MEASURED[r.keyword.toLowerCase()][1])}）</span>` : '<span class="pill pill-bad">演示数字，非实测</span>') : "";
    const tip = n > BATCH ? `<span class="pill pill-info">超过 1000：分批读取，每批 1000，共 ${nb} 批${n > MAX_ROWS ? "（EHunt 最多显示前 5,000 条）" : ""}</span>` : "";
    const head = `<div class="ss-count-n"><b>${esc(r.keyword)}</b>：<span class="ss-n ss-n-info">${n.toLocaleString()}</span> 条 ${tip}${label}</div>
      <p class="hint">筛选：$8–40 · 总销量 ≥100 · 7 天销量 >3 · 按 7 天销量降序</p>`;
    if (r.status === "counted" && n === 0) return `<div class="ss-count">${head}<div class="ss-form"><button class="btn btn-primary" disabled>抓取 →</button><span class="warn">0 条：${/[぀-ヿ㐀-鿿가-힯]/.test(r.keyword) ? "EHunt 只收录英文标题，请改用英文关键词再查" : "建议放宽关键词或换个词再查"}</span></div></div>`;
    if (r.status !== "counted") return `<div class="ss-count">${head}<p class="hint">本次最多读取 ${esc((r.max_rows ?? 0).toLocaleString())} 条（每页 20 条${(r.max_rows || 0) > BATCH ? `，每批 1000` : ""}）</p></div>`;
    // 052：counted 仍是待抓取（未入队）；真正 queued 才算已入队。保留手动改条数兜底（触发未跑或失败时）
    const autoLim = Math.min(n, MAX_ROWS);
    return `<div class="ss-count">${head}<div class="ss-form"><span class="pill pill-info">待抓取</span><span class="hint">最多 ${autoLim.toLocaleString()} 条（EHunt 前 5,000）${n > BATCH ? " · 先读第 1 批（1–1000）" : ""} · 有结果将自动入队 · 可点取消</span></div>
      <details class="ss-auto-fb"><summary class="hint">手动改条数（一般不用）</summary><div class="ss-form"><label>最多读取 <input id="ssLimit" class="input input-sm ss-thr" type="number" min="1" max="${autoLim}" value="${esc(V.limErr && $("ssLimit") ? $("ssLimit").value : String(autoLim))}"> 条</label>
      <button class="btn btn-primary" id="ssGo">抓取 →</button><span id="ssLimErr" class="warn" role="alert">${esc(V.limErr)}</span></div></details></div>`;
  }
  // 链接列表：每条前面一个只读勾选框，已送去预筛选 = 勾选「已发送」，其余「未发送」
  function linkList(b, links) {
    const F = linkSentFlags(b, links.length), nS = F.filter(Boolean).length;
    return `<ol class="ss-links ss-llist" aria-label="链接列表（已发送 ${nS} / ${links.length}）">${links.map((u, i) => `<li class="${F[i] ? "ss-l-sent" : ""}"><label class="ss-lchk" title="${F[i] ? "已送去预筛选（含已在预筛选列表里被去重的）" : "还没有送去预筛选"}"><input type="checkbox" disabled${F[i] ? " checked" : ""} aria-label="${F[i] ? "已发送" : "未发送"}"><span>${F[i] ? "已发送" : "未发送"}</span></label><span class="ss-lurl" title="${esc(u)}">${esc(u)}</span></li>`).join("")}</ol>`;
  }
  function batchBlock(b) {
    const links = V.links[b.id] || [], range = `${b.offset_start.toLocaleString()}–${b.offset_end.toLocaleString()}`;
    const demo = b.demo ? '<span class="pill pill-bad">演示数字</span>' : "";
    const exl = b.excluded_count ? `<span class="hint">已排除 ${b.excluded_count}：${esc(sumReasons(b.excluded_by_reason))}（不调豆包、不显示、不入库）</span>` : "";
    const tot = b.offset_end - b.offset_start + 1;
    const st = sendState(b, links.length), busy = V.sending === b.id || V.sending === "all";
    const auto = b.auto_sent_at ? (b.auto_pending ? `<span class="pill pill-info ss-auto" title="预筛选排队已满 1000 条，空出后自动补发剩余 ${st.left} 条">已自动发送 · 等待补发 ${st.left} 条</span>` : `<span class="pill pill-info ss-auto" title="${esc(fmtAt(b.auto_sent_at))} 读完后自动送去预筛选">已自动发送</span>`) : "";
    const mark0 = st.full ? `<span class="pill pill-ok" title="${esc(fmtAt(st.at))} 已送去预筛选">✓ 已发送</span>` : st.partial ? `<span class="pill pill-warn" title="${esc(fmtAt(st.at))} 发送了 ${st.sent}/${st.total} 条">已发送（部分 ${st.sent}/${st.total}）</span>` : "";
    const mark = auto + mark0;
    const psBtn = `${mark}<button class="btn ${st.full ? "btn-ghost" : "btn-primary"} btn-sm" data-ssps="${esc(b.id)}"${st.full ? ' title="除了获取失败，不重复获取：已获取成功的不会再获取，只重新获取失败的"' : ""}${links.length && !V.sending ? "" : " disabled"}${busy ? ' aria-busy="true"' : ""}>${busy ? '<span class="spin"></span> 发送中…' : st.full ? `再次送去预筛选（${links.length} 条）` : st.partial ? `发送剩余（${st.left} 条）` : `送去预筛选（${links.length} 条）`}</button>`;
    const btns = `<button class="btn btn-ghost btn-sm" data-sscopy="${esc(b.id)}"${links.length ? "" : " disabled"}>复制本批</button><button class="btn btn-ghost btn-sm" data-ssdl="${esc(b.id)}"${links.length ? "" : " disabled"}>下载 TXT</button>${psBtn}`;
    if (b.status === "canceled" || b.status === "failed") { // 失败的批次也显示已通过的链接（原来只显示“失败”两个字，已入库的链接看不到也复制不了）
      const f = b.status === "failed", canRe = V.run?.status === (f ? "failed" : "canceled") && b === lastStopped();
      return `<section class="ss-batch ss-batch-stop"><div class="ss-res-head"><h3>第 ${b.batch_no} 批（${range}）· <span class="pill pill-bad">${f ? "失败" : "已停止"}</span> 已读 ${(b.scraped_count || 0).toLocaleString()} / ${tot.toLocaleString()} 条，通过 <span class="ss-n ss-n-ok">${links.length}</span> 条</h3>${demo}<span class="spacer"></span>
        ${canRe ? `<button class="btn btn-primary btn-sm" id="ssResume">${f ? "重试本批（从断点）" : "继续本批"}</button>` : ""}${btns}</div>
      ${links.length ? `${linkList(b, links)}` : ""}${exl}</section>`; }
    if (b.status !== "done") { const w = b.status === "queued" && !b.started_at ? waitedMin(b.created_at) : 0;
      return `<div class="ss-batch ss-batch-run"><b>第 ${b.batch_no} 批（${range}）</b> <span class="hint">${b.status === "queued" ? `排队中，等待 box 抓取进程${w >= 2 ? `（已等待 ${w} 分钟，box 可能暂停，恢复后自动开始）` : ""}` : "读取中（进度见上方）"}</span></div>`; }
    if (b.copied_at && !V.open.has(b.id)) return `<div class="ss-batch ss-batch-copied"><span class="pill pill-ok">✓ 已复制</span> <b>第 ${b.batch_no} 批（${range}）</b> 符合要求 ${links.length} 条 ${demo}<span class="spacer"></span>${psBtn}<button class="btn btn-link btn-sm" data-ssopen="${esc(b.id)}">展开</button></div>`;
    return `<section class="ss-batch"><div class="ss-res-head"><h3>第 ${b.batch_no} 批（${range}）· 符合要求 <span class="ss-n ss-n-ok">${links.length}</span> 条</h3>${demo}<span class="spacer"></span>
        ${b.copied_at ? `<button class="btn btn-link btn-sm" data-ssfold="${esc(b.id)}">收起</button>` : ""}
        ${btns}</div>
      ${linkList(b, links)}${exl}</section>`;
  }
  function render() {
    const body = $("ssBody"); if (!body) return; const r = V.run;
    $("ssSum").innerHTML = ""; $("ssTabs").innerHTML = ""; $("ssFoot").innerHTML = "";
    const cur = V.batches.find((b) => b.status === "running" || b.status === "queued");
    const ST = { count_queued: "排队查询数量", counting: "读取结果数中", queued: "排队中（等待 box 抓取进程）", running: "读取 EHunt 列表页中", batch_done: "本批完成", succeeded: "全部完成", failed: "失败", canceled: "已停止" };
    const prog = progressPanel(r);
    $("ssStart").innerHTML = `<div id="ssRecent"></div><div class="card ss-start"><div class="ss-form"><input id="ssKw" class="input" maxlength="${KW_MAX}" placeholder="EHunt 关键词，例如 acrylic decoration" value="${esc($("ssKw")?.value || r?.keyword || "")}">
        <button class="btn btn-primary" id="ssCount"${countDisabled(r, V.counting) ? " disabled" : ""} title="${active(r) ? "当前任务进行中也可以查询新关键词，会排进队列按顺序执行" : ""}">查询数量</button><button class="btn btn-ghost btn-sm" id="ssKwLog" title="导出全部关键词查询记录（永久保留）">导出查询记录 CSV</button></div>${countPanel(r)}${prog}</div>`; DEMO ? loadRecent() : renderRecent();
    const done = V.batches.filter((b) => FIN.includes(b.status));
    const all = [...new Set(done.flatMap((b) => V.links[b.id] || []))], exAll = done.reduce((a, b) => a + (b.excluded_count || 0), 0);
    const exR = {}; done.forEach((b) => Object.entries(b.excluded_by_reason || {}).forEach(([k, v]) => exR[k] = (exR[k] || 0) + v));
    const left = r ? canRead(r) : 0;
    const nextBar = r && ["batch_done", "succeeded"].includes(r.status) && left > 0 ? (() => {
      const re = readEnd(), planned = Math.max(0, (r.max_rows || 0) - re), def = planned || left;
      const val = V.addErr && $("ssAdd") ? $("ssAdd").value : String(def), add = Math.min(Math.max(1, Math.floor(Number(val)) || def), left);
      const e = re + Math.min(add, BATCH), nb = Math.ceil(add / BATCH);
      return `<div class="card ss-next"><div>${r.error ? `<p class="warn">${esc(friendlyErr(r.error))}</p>` : ""}<b>已读 1–${re.toLocaleString()} 条，还可继续读 ${left.toLocaleString()} 条</b><p class="hint">共 ${(r.total_count || 0).toLocaleString()} 条${r.total_count > MAX_ROWS ? "，EHunt 最多显示前 5,000 条" : ""} · 从第 ${(re + 1).toLocaleString()} 条接着读，已读的不重读 · 每批 1000</p></div><span class="spacer"></span>
        <label class="hint">再读取 <input id="ssAdd" class="input input-sm ss-thr" type="number" min="1" max="${left}" value="${esc(val)}"> 条</label>
        <button class="btn btn-primary" id="ssNext">读取下一批（${(re + 1).toLocaleString()}–${e.toLocaleString()}）</button><button class="btn btn-ghost${nb > 1 ? "" : " hidden"}" id="ssAll">一键读取全部（${add.toLocaleString()} 条，${nb} 批）</button><span id="ssAddErr" class="warn" role="alert">${esc(V.addErr)}</span></div>`; })() : "";
    const cum = done.length > 1 || (r && r.status !== "succeeded" && done.length) ? `<div class="card ss-cum"><b>累计符合要求 <span class="ss-n ss-n-ok">${all.length.toLocaleString()}</span> 条</b><span class="hint">（${done.length} 批 · 已读 ${done.reduce((a, b) => a + b.scraped_count, 0).toLocaleString()} 条）</span>${V.batches.some((b) => b.demo) ? '<span class="pill pill-bad">演示数字</span>' : ""}<span class="spacer"></span>
        <button class="btn btn-primary btn-sm" data-sscopy="all"${all.length ? "" : " disabled"}>复制全部批次</button><button class="btn btn-ghost btn-sm" data-ssdl="all"${all.length ? "" : " disabled"}>下载 TXT</button>${(() => { const P = sendPlan(done.filter((b) => (V.links[b.id] || []).length), (id) => V.links[id], false); return `<button class="btn btn-primary btn-sm" id="ssSendAll"${P.ids.length && !V.sending ? "" : " disabled"} title="${P.restoreOnly ? "所有批次都已发送；点此只恢复在预筛选里删除过的" : P.ids.length ? `发送 ${P.ids.length} 个未发送/部分发送的批次` : "所有批次都已发送"}">${V.sending === "all" ? '<span class="spin"></span> 发送中…' : P.restoreOnly ? "全部已发送 · 恢复已删除的" : P.ids.length ? `发送全部批次（${P.n.toLocaleString()} 条）` : "全部批次已发送"}</button>`; })()}</div>` : "";
    body.innerHTML = !r ? '<div class="card empty"><div class="ico">◎</div><h3>输入关键词，先“查询数量”</h3><p class="muted">查到数量后会自动加入抓取队列（0 条不抓；也可手动改条数）</p></div>'
      : `${cum}${V.batches.map(batchBlock).join("")}${nextBar}
        ${done.length ? `<p class="hint ss-note">已排除合计 ${exAll.toLocaleString()}${exAll ? `：${esc(sumReasons(exR))}` : ""}（原因只记在 box 本地日志，3 天后删除）· 仅保留最近 3 天的筛选结果 · 网页不提供搜索，查历史请在对话里问序猿</p>` : ""}`;
  }
  document.addEventListener("click", (e) => {
    const t = e.target, id = t.id;
    if (id === "ssKwLog") return exportKwLog();
    if (id === "ssCount") return guard("count", count); if (id === "ssGo") return guard("start", start); if (id === "ssStop") return guard("cancel", cancel);
    if (id === "ssResume") return guard("resume", resume);
    const ps = t.closest?.("[data-ssps]"); if (ps) return guard("send", () => prescreen(ps.dataset.ssps));
    if (t.closest?.("#ssSendAll")) return guard("send", () => prescreen(null, true));
    if (id === "ssNext") return guard("next", () => next(false)); if (id === "ssAll") return guard("next", () => next(true));
    if (t.dataset?.sscopy) return copy(t.dataset.sscopy);
    if (t.dataset?.ssdl) return download(t.dataset.ssdl);
    const rd = t.closest?.("[data-ssrdel]"); if (rd) { e.preventDefault(); return guard("rdel", () => delRecent(rd.dataset.ssrdel)); }
    const rt = t.closest?.("[data-ssrtog]");
    if (rt) { const open = !rt.closest(".ss-recent").classList.contains("open"); try { localStorage.setItem(RECENT_KEY, open ? "1" : "0"); } catch {} return renderRecent(); }
    if (t.dataset?.ssopen) { V.open.add(t.dataset.ssopen); return render(); }
    if (t.dataset?.ssfold) { V.open.delete(t.dataset.ssfold); return render(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    if (e.target.id === "ssKw" && !$("ssCount")?.disabled) { e.preventDefault(); guard("count", count); }
    else if (e.target.id === "ssLimit") { e.preventDefault(); guard("start", start); }
    else if (e.target.id === "ssAdd") { e.preventDefault(); guard("next", () => next(false)); }
  });
  document.addEventListener("input", (e) => { if (e.target.id === "ssAdd") return addLabels(); if (e.target.id === "ssLimit") { V.limErr = ""; e.target.classList.remove("input-bad"); const el = $("ssLimErr"); if (el) el.textContent = ""; } });
  // 046 自动送去预筛选开关（sel_prefs.auto_send，没有记录 = 开启）；表不存在（046 未执行）→ 不显示开关
  async function loadPrefs() {
    if (DEMO) { V.auto = QSP.get("autosend") !== "0"; V.autoOk = true; return renderAuto(); }
    const sb = getSb(); if (!sb) return; const { data, error } = await sb.from("sel_prefs").select("auto_send").maybeSingle();
    V.autoOk = !error; V.auto = error ? null : data ? !!data.auto_send : true; renderAuto();
  }
  function renderAuto() {
    const el = $("ssAuto"); if (!el) return; if (!V.autoOk) { el.innerHTML = ""; return; }
    el.innerHTML = `<label class="ss-autosw" title="每批读完后，数据库自动把这批链接送去预筛选（每条只发一次；排队满 1000 条时先发一部分，空出后自动补发）"><input type="checkbox" id="ssAutoChk"${V.auto ? " checked" : ""}${V.autoBusy ? " disabled" : ""}> 读完自动送去预筛选</label>`;
  }
  document.addEventListener("change", async (e) => {
    if (e.target.id !== "ssAutoChk" || V.autoBusy) return; const on = e.target.checked; V.autoBusy = true; renderAuto();
    let err = null; if (!DEMO) { const sb = getSb(), { data: u } = await sb.auth.getUser(); ({ error: err } = await sb.from("sel_prefs").upsert({ user_id: u?.user?.id, auto_send: on, updated_at: new Date().toISOString() })); }
    V.autoBusy = false; if (err) { toast(`保存失败：${err.message}`, "bad"); } else { V.auto = on; toast(on ? "已开启：每批读完自动送去预筛选" : "已关闭自动发送（可手动点「送去预筛选」）", "ok"); } renderAuto();
  });
  loadPrefs();
  return { show: (runId) => { if (V.autoOk == null) loadPrefs(); else renderAuto(); return load(runId); }, _state: V };
}
