// 获取链接（#/selscreen[/<run>]，导航在「任务」前）。Hongyan 2026-10-16 定稿：
// ① 关键词 → 查 EHunt 结果数（价格 8–40 · 总销量 ≥100 · 7 天销量 >3 · 按 7 天销量降序）→ ② 填最多读取条数 → 抓取（box 上的 worker，不在阿里云）
// ③ 标题规则先筛：排除的不调豆包、不显示、不入库（页面只一行汇总；原因写 box 本地日志 3 天）→ ④ 通过的调豆包视觉 → 显示符合要求 N 条 + 纯 Etsy 链接
// ⑤ >1000 分批，每批 1000，最多 5 批 / 5,000；“还剩 N 条未读”只在每批完成后更新 ⑥ 只有符合要求的入库，保留 3 天 ⑦ 网页无搜索，查询找序猿
const BATCH = 1000, MAX_ROWS = 5000; // 每批 1000，最多读到 EHunt 第 5,000 条（分批次数不再单独限制，第 1 批可能不足 1000）
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
  if (r.status === "counted") return r.total_count == null ? "待抓取" : `待抓取 · 共 ${n(r.total_count)} 条`;
  if (["queued", "running"].includes(r.status)) return `读取中 · 已符合 ${n(r.passed_count)} 条`;
  if (r.status === "failed") return r.total_count == null ? "查询失败" : `失败 · 符合 ${n(r.passed_count)} 条`;
  if (r.status === "canceled") return `已停止 · 符合 ${n(r.passed_count)} 条`;
  return `符合 ${n(r.passed_count)} 条`;
}
// 等待了多久（box 暂停时排队会很久，给出提示，不是卡死）
export const waitedMin = (iso, now = Date.now()) => (iso ? Math.max(0, Math.floor((now - Date.parse(iso)) / 60000)) : 0);
export function createSelScreen({ $, esc, toast, getSb, DEMO, confirmDialog, sendToPrescreen }) {
  const QSP = new URLSearchParams(location.search);
  const V = { addErr: "", run: null, batches: [], links: {}, open: new Set(), timer: null, fx: null, demoRuns: {}, recent: null, limErr: "", busy: new Set(), seq: 0, lastSt: null };
  const FILTERS = { price_min: 8, price_max: 40, min_sales: 100, min_sales_7d: 4, sort: "↓7-day sales" };
  const active = (r) => r && ["count_queued", "counting", "queued", "running"].includes(r.status);
  // 实测数（全部筛选含 7 天销量 Min 4，2026-10-16）。其他词在演示里会生成数字并标“演示数字”
  const MEASURED = { "acrylic decoration": [20, "Find 20 products"], "sign": [1688, "共 1,688 条 / 85 页"] };
  const DEMO_ONLY = { "acrylic sign": 3260, "no results": 0 }; // 演示数字（用于演示多批 / 一键读取全部剩余），界面标“演示数字”
  const demoTotal = (kw) => MEASURED[kw.toLowerCase()]?.[0] ?? DEMO_ONLY[kw.toLowerCase()] ?? (40 + [...kw].reduce((a, c) => a + c.charCodeAt(0) * 7, 0) % 900);
  const sumReasons = (o) => RORDER.filter((k) => o?.[k]).map((k) => `${k} ${o[k]}`).join("、");
  const FIN = ["done", "canceled", "failed"]; // 批次已结束（停止/失败的批次也保留已通过的链接）
  const KW_MAX = 200;
  // 防重复点击：同一动作进行中再点/回车直接忽略（QA 2026-10-19 #1：连点“查询数量”会建 2 个任务）
  async function guard(key, fn) { if (V.busy.has(key)) return; V.busy.add(key); try { return await fn(); } finally { V.busy.delete(key); } }

  // ---------- 演示（不连 Supabase） ----------
  function demoCount(kw) {
    const id = `demo-${Date.now()}`, run = { id, keyword: kw, status: "counting", total_count: null, batch_size: BATCH, current_batch: 0, remaining_count: null, measured: !!MEASURED[kw.toLowerCase()] };
    V.demoRuns[id] = { run, batches: [], links: {} }; location.hash = `#/selscreen/${id}`;
    setTimeout(() => { if (run.status === "canceled") return; run.status = "counted"; run.total_count = demoTotal(kw); refresh(); }, Number(QSP.get("countms")) || 1000);
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
      if (b.scraped_count < total) { refresh(); return setTimeout(tick, Number(QSP.get("speed")) || 120); }
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
    $("ssKw").value = kw; const btn = $("ssCount"); if (btn) btn.disabled = true;
    if (DEMO) return demoCount(kw);
    const { data, error } = await getSb().rpc("sel_count", { p_keyword: kw, p_params: FILTERS });
    if (error) {
      if (btn) btn.disabled = false;
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
  async function prescreen(bid) {
    const list = V.links[bid] || []; if (!list.length) return;
    const b = V.batches.find((x) => x.id === bid);
    if (!await confirmDialog({ title: `把第 ${b?.batch_no} 批送去预筛选？`, ok: `送去预筛选（${list.length} 条）`, body: `<p>将提交 <b>${list.length}</b> 条链接为预筛选任务（每条新 listing 消耗 1 次 EHunt 额度，24 小时内重复的走缓存），提交后跳到「预筛选」页。</p>` })) return;
    await sendToPrescreen(list);
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
        { id: "demo-recent-2", keyword: "wedding sign", status: "batch_done", created_at: new Date(Date.now() - 30 * 36e5).toISOString(), passed_count: 312 }];
      V.recent = [...own, ...fx].filter((r) => Date.parse(r.created_at) > since).sort((a, b) => b.created_at.localeCompare(a.created_at)); return renderRecent();
    }
    const sb = getSb(); if (!sb) return;
    const { data, error } = await sb.from("sel_runs").select("id,keyword,status,created_at,passed_count,total_count").gte("created_at", new Date(Date.now() - 3 * 864e5).toISOString()).order("created_at", { ascending: false }).limit(20);
    V.recent = error ? [] : data || []; renderRecent();
  }
  function renderRecent() {
    const el = $("ssRecent"); if (!el) return; const L = V.recent || [];
    const ST = { count_queued: "查询中", counting: "查询中", counted: "待抓取", queued: "排队中", running: "读取中", batch_done: "本批完成", succeeded: "完成", failed: "失败", canceled: "已停止" };
    const t = (iso) => { const d = new Date(iso); return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
    el.innerHTML = !L.length ? "" : `<div class="ss-recent"><span class="hint">最近 3 天：</span>${L.map((r) => `<a class="ss-rchip${V.run?.id === r.id ? " active" : ""}" href="#/selscreen/${esc(r.id)}" title="${esc(ST[r.status] || r.status)}"><b>${esc(r.keyword)}</b> <span class="hint">${t(r.created_at)} · ${esc(chipText(r))}</span></a>`).join("")}</div>`;
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
    const kw = id === "demo-recent-1" ? "acrylic decoration" : "wedding sign", n = id === "demo-recent-1" ? 20 : 1400;
    const run = { id, keyword: kw, status: id === "demo-recent-1" ? "succeeded" : "batch_done", total_count: n, max_rows: n, batch_size: BATCH, current_batch: 1, remaining_count: n - Math.min(n, BATCH), created_at: new Date(Date.now() - (id === "demo-recent-1" ? 5 : 30) * 36e5).toISOString() };
    const pass = id === "demo-recent-1" ? 7 : 312, b = { id: `${id}-b1`, batch_no: 1, offset_start: 1, offset_end: Math.min(n, BATCH), status: "done", scraped_count: Math.min(n, BATCH), passed_count: pass, excluded_count: Math.min(n, BATCH) - pass, excluded_by_reason: { "定制": Math.min(n, BATCH) - pass }, demo: true };
    V.demoRuns[id] = { run, batches: [b], links: { [b.id]: demoLinks(run, 1, pass) } };
  }
  function schedule() { clearTimeout(V.timer); if (!DEMO && active(V.run) && location.hash.startsWith("#/selscreen")) V.timer = setTimeout(() => load(V.run.id), 3000); }
  async function load(runId) {
    if (DEMO) { V.fx ||= (await import("./demo-selection.js")).selectionFixtures(); demoRecentRun(runId); loadRecent(); V.run = V.demoRuns[runId]?.run || null; return refresh(); }
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
  // ---------- 界面 ----------
  function countPanel(r) {
    const locked = `<div class="ss-form"><label class="hint">最多读取 <input class="input input-sm ss-thr" type="number" disabled placeholder="—"> 条</label><button class="btn btn-primary" disabled title="先查询数量">抓取 →</button><span class="hint">先查询数量，显示结果数后才能抓取</span></div>`;
    if (!r || r.total_count == null) {
      const w = r && active(r) ? waitedMin(r.created_at) : 0;
      return `<div class="ss-count">${r && active(r) ? `<div class="ss-prog-row"><span><span class="spin"></span> ${r.status === "count_queued" ? "排队等待 box 查询" : "正在读取"} EHunt「${esc(r.keyword)}」的结果数…</span>${w >= 2 ? `<span class="warn">已等待 ${w} 分钟（box 可能暂停，恢复后自动继续；也可以取消后重试）</span>` : ""}<button class="btn btn-ghost btn-sm" id="ssStop">取消查询</button></div>` : ""}${locked}</div>`;
    }
    const n = r.total_count, nb = Math.ceil(Math.min(n, MAX_ROWS) / BATCH);
    const label = DEMO ? (MEASURED[r.keyword.toLowerCase()] ? `<span class="pill pill-ok">EHunt 实测（10-16 ${esc(MEASURED[r.keyword.toLowerCase()][1])}）</span>` : '<span class="pill pill-bad">演示数字，非实测</span>') : "";
    const tip = n > BATCH ? `<span class="pill pill-info">超过 1000：分批读取，每批 1000，共 ${nb} 批${n > MAX_ROWS ? "（EHunt 最多显示前 5,000 条）" : ""}</span>` : "";
    const head = `<div class="ss-count-n"><b>${esc(r.keyword)}</b>：<span class="ss-n ss-n-info">${n.toLocaleString()}</span> 条 ${tip}${label}</div>
      <p class="hint">筛选：$8–40 · 总销量 ≥100 · 7 天销量 >3 · 按 7 天销量降序</p>`;
    if (r.status === "counted" && n === 0) return `<div class="ss-count">${head}<div class="ss-form"><button class="btn btn-primary" disabled>抓取 →</button><span class="warn">0 条：${/[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/.test(r.keyword) ? "EHunt 只收录英文标题，请改用英文关键词再查" : "建议放宽关键词或换个词再查"}</span></div></div>`;
    if (r.status !== "counted") return `<div class="ss-count">${head}<p class="hint">本次最多读取 ${esc((r.max_rows ?? 0).toLocaleString())} 条（每页 20 条${(r.max_rows || 0) > BATCH ? `，每批 1000` : ""}）</p></div>`;
    return `<div class="ss-count">${head}<div class="ss-form"><label>最多读取 <input id="ssLimit" class="input input-sm ss-thr" type="number" min="1" max="${Math.min(n, MAX_ROWS)}" value="${esc(V.limErr && $("ssLimit") ? $("ssLimit").value : String(Math.min(n, MAX_ROWS)))}"> 条</label>
      <button class="btn btn-primary" id="ssGo">抓取 →</button><span id="ssLimErr" class="warn" role="alert">${esc(V.limErr)}</span>${n > BATCH ? '<span class="hint">先读第 1 批（1–1000），完成后可读下一批或一键读取全部剩余</span>' : ""}</div></div>`;
  }
  function batchBlock(b) {
    const links = V.links[b.id] || [], range = `${b.offset_start.toLocaleString()}–${b.offset_end.toLocaleString()}`;
    const demo = b.demo ? '<span class="pill pill-bad">演示数字</span>' : "";
    const exl = b.excluded_count ? `<span class="hint">已排除 ${b.excluded_count}：${esc(sumReasons(b.excluded_by_reason))}（不调豆包、不显示、不入库）</span>` : "";
    const tot = b.offset_end - b.offset_start + 1;
    const psBtn = `<button class="btn btn-primary btn-sm" data-ssps="${esc(b.id)}"${links.length ? "" : " disabled"}>送去预筛选（${links.length} 条）</button>`;
    const btns = `<button class="btn btn-ghost btn-sm" data-sscopy="${esc(b.id)}"${links.length ? "" : " disabled"}>复制本批</button><button class="btn btn-ghost btn-sm" data-ssdl="${esc(b.id)}"${links.length ? "" : " disabled"}>下载 TXT</button>${psBtn}`;
    if (b.status === "canceled" || b.status === "failed") { // 失败的批次也显示已通过的链接（原来只显示“失败”两个字，已入库的链接看不到也复制不了）
      const f = b.status === "failed", canRe = V.run?.status === (f ? "failed" : "canceled") && b === lastStopped();
      return `<section class="ss-batch ss-batch-stop"><div class="ss-res-head"><h3>第 ${b.batch_no} 批（${range}）· <span class="pill pill-bad">${f ? "失败" : "已停止"}</span> 已读 ${(b.scraped_count || 0).toLocaleString()} / ${tot.toLocaleString()} 条，通过 <span class="ss-n ss-n-ok">${links.length}</span> 条</h3>${demo}<span class="spacer"></span>
        ${canRe ? `<button class="btn btn-primary btn-sm" id="ssResume">${f ? "重试本批（从断点）" : "继续本批"}</button>` : ""}${btns}</div>
      ${links.length ? `<pre class="ss-links">${esc(links.join("\n"))}</pre>` : ""}${exl}</section>`; }
    if (b.status !== "done") { const w = b.status === "queued" ? waitedMin(b.created_at) : 0;
      return `<div class="ss-batch ss-batch-run"><b>第 ${b.batch_no} 批（${range}）</b> <span class="hint">${b.status === "queued" ? `排队中，等待 box 抓取进程${w >= 2 ? `（已等待 ${w} 分钟，box 可能暂停，恢复后自动开始）` : ""}` : `读取中… 已读 ${(b.scraped_count || 0).toLocaleString()} 条，通过 ${b.passed_count || 0} 条`}</span></div>`; }
    if (b.copied_at && !V.open.has(b.id)) return `<div class="ss-batch ss-batch-copied"><span class="pill pill-ok">✓ 已复制</span> <b>第 ${b.batch_no} 批（${range}）</b> 符合要求 ${links.length} 条 ${demo}<span class="spacer"></span>${psBtn}<button class="btn btn-link btn-sm" data-ssopen="${esc(b.id)}">展开</button></div>`;
    return `<section class="ss-batch"><div class="ss-res-head"><h3>第 ${b.batch_no} 批（${range}）· 符合要求 <span class="ss-n ss-n-ok">${links.length}</span> 条</h3>${demo}<span class="spacer"></span>
        ${b.copied_at ? `<button class="btn btn-link btn-sm" data-ssfold="${esc(b.id)}">收起</button>` : ""}
        ${btns}</div>
      <pre class="ss-links">${esc(links.join("\n"))}</pre>${exl}</section>`;
  }
  function render() {
    const body = $("ssBody"); if (!body) return; const r = V.run;
    $("ssSum").innerHTML = ""; $("ssTabs").innerHTML = ""; $("ssFoot").innerHTML = "";
    const cur = V.batches.find((b) => b.status === "running" || b.status === "queued");
    const ST = { count_queued: "排队查询数量", counting: "读取结果数中", queued: "排队中（等待 box 抓取进程）", running: "读取 EHunt 列表页中", batch_done: "本批完成", succeeded: "全部完成", failed: "失败", canceled: "已停止" };
    const prog = r && cur && active(r) ? (() => { const tot = cur.offset_end - cur.offset_start + 1, pct = Math.round(100 * (cur.scraped_count || 0) / tot);
      return `<div class="ss-prog-row"><span class="pill pill-info"><span class="spin"></span> 第 ${cur.batch_no} 批（${cur.offset_start.toLocaleString()}–${cur.offset_end.toLocaleString()}）${esc(ST[r.status])}</span>
        <span class="sc-prog ss-prog"><span style="width:${pct}%"></span></span><span class="hint">${pct}%</span><button class="btn btn-ghost btn-sm" id="ssStop">■ 停止</button></div>`; })()
      : r && ["failed", "canceled"].includes(r.status) ? `<div class="ss-prog-row"><span class="pill pill-bad">${esc(ST[r.status])}</span>${r.error ? `<span class="warn" title="${esc(r.error)}">${esc(friendlyErr(r.error))}</span>` : ""}</div>` : "";
    $("ssStart").innerHTML = `<div id="ssRecent"></div><div class="card ss-start"><div class="ss-form"><input id="ssKw" class="input" maxlength="${KW_MAX}" placeholder="EHunt 关键词，例如 acrylic decoration" value="${esc($("ssKw")?.value || r?.keyword || "")}">
        <button class="btn btn-primary" id="ssCount"${active(r) ? " disabled" : ""}>查询数量</button></div>${countPanel(r)}${prog}</div>`; DEMO ? loadRecent() : renderRecent();
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
        <button class="btn btn-primary btn-sm" data-sscopy="all"${all.length ? "" : " disabled"}>复制全部批次</button><button class="btn btn-ghost btn-sm" data-ssdl="all"${all.length ? "" : " disabled"}>下载 TXT</button></div>` : "";
    body.innerHTML = !r ? '<div class="card empty"><div class="ico">◎</div><h3>输入关键词，先“查询数量”</h3><p class="muted">再填写最多读取条数点“抓取”</p></div>'
      : `${cum}${V.batches.map(batchBlock).join("")}${nextBar}
        ${done.length ? `<p class="hint ss-note">已排除合计 ${exAll.toLocaleString()}${exAll ? `：${esc(sumReasons(exR))}` : ""}（原因只记在 box 本地日志，3 天后删除）· 仅保留最近 3 天的筛选结果 · 网页不提供搜索，查历史请在对话里问序猿</p>` : ""}`;
  }
  document.addEventListener("click", (e) => {
    const t = e.target, id = t.id;
    if (id === "ssCount") return guard("count", count); if (id === "ssGo") return guard("start", start); if (id === "ssStop") return guard("cancel", cancel);
    if (id === "ssResume") return guard("resume", resume);
    if (t.dataset?.ssps) return prescreen(t.dataset.ssps);
    if (id === "ssNext") return guard("next", () => next(false)); if (id === "ssAll") return guard("next", () => next(true));
    if (t.dataset?.sscopy) return copy(t.dataset.sscopy);
    if (t.dataset?.ssdl) return download(t.dataset.ssdl);
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
  return { show: (runId) => load(runId), _state: V };
}
