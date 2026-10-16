// 获取链接（#/selscreen[/<run>]，导航在「任务」前）。Hongyan 2026-10-16 定稿：
// ① 关键词 → 查 EHunt 结果数（价格 8–40 · 总销量 ≥100 · 7 天销量 >3 · 按 7 天销量降序）→ ② 填最多读取条数 → 抓取（box 上的 worker，不在阿里云）
// ③ 标题规则先筛：排除的不调豆包、不显示、不入库（页面只一行汇总；原因写 box 本地日志 3 天）→ ④ 通过的调豆包视觉 → 显示符合要求 N 条 + 纯 Etsy 链接
// ⑤ >1000 分批，每批 1000，最多 5 批 / 5,000；“还剩 N 条未读”只在每批完成后更新 ⑥ 只有符合要求的入库，保留 3 天 ⑦ 网页无搜索，查询找序猿
const BATCH = 1000, MAX_ROWS = 5000, MAX_BATCHES = 5;
const RORDER = ["定制", "IP", "材质", "素材/数字文件", "7天销量≤3", "其他"];
export function createSelScreen({ $, esc, toast, getSb, DEMO, confirmDialog, sendToPrescreen }) {
  const QSP = new URLSearchParams(location.search);
  const V = { run: null, batches: [], links: {}, open: new Set(), timer: null, fx: null, demoRuns: {}, recent: null, limErr: "" };
  const FILTERS = { price_min: 8, price_max: 40, min_sales: 100, min_sales_7d: 4, sort: "↓7-day sales" };
  const active = (r) => r && ["count_queued", "counting", "queued", "running"].includes(r.status);
  // 实测数（全部筛选含 7 天销量 Min 4，2026-10-16）。其他词在演示里会生成数字并标“演示数字”
  const MEASURED = { "acrylic decoration": [20, "Find 20 products"], "sign": [1688, "共 1,688 条 / 85 页"] };
  const DEMO_ONLY = { "acrylic sign": 3260, "no results": 0 }; // 演示数字（用于演示多批 / 一键读取全部剩余），界面标“演示数字”
  const demoTotal = (kw) => MEASURED[kw.toLowerCase()]?.[0] ?? DEMO_ONLY[kw.toLowerCase()] ?? (40 + [...kw].reduce((a, c) => a + c.charCodeAt(0) * 7, 0) % 900);
  const sumReasons = (o) => RORDER.filter((k) => o?.[k]).map((k) => `${k} ${o[k]}`).join("、");

  // ---------- 演示（不连 Supabase） ----------
  function demoCount(kw) {
    const id = `demo-${Date.now()}`, run = { id, keyword: kw, status: "counting", total_count: null, batch_size: BATCH, current_batch: 0, remaining_count: null, measured: !!MEASURED[kw.toLowerCase()] };
    V.demoRuns[id] = { run, batches: [], links: {} }; location.hash = `#/selscreen/${id}`;
    setTimeout(() => { run.status = "counted"; run.total_count = demoTotal(kw); refresh(); }, Number(QSP.get("countms")) || 1000);
  }
  const demoLinks = (run, bno, n) => { const seed = run.keyword.length * 7919 + bno * 104729; return Array.from({ length: n }, (_, i) => `https://www.etsy.com/listing/${9000000000 + ((seed + i * 7121) % 99999999)}`); };
  function demoBatch(run, bno, resume) {
    const D = V.demoRuns[run.id], s = (bno - 1) * BATCH + 1, e = Math.min(bno * BATCH, run.max_rows);
    if (resume) { Object.assign(resume, { status: "running" }); run.status = "running"; }
    const b = resume || { id: `${run.id}-b${bno}`, batch_no: bno, offset_start: s, offset_end: e, status: "running", scraped_count: 0, passed_count: 0, excluded_count: 0, excluded_by_reason: {}, copied_at: null, demo: run.keyword.toLowerCase() !== "acrylic decoration" }; // 只有 acrylic decoration 有回放真数据，其余批次结果都是演示数字
    if (!resume) D.batches.push(b); Object.assign(run, { status: "running", current_batch: bno }); refresh();
    const real = run.keyword.toLowerCase() === "acrylic decoration";
    const total = e - s + 1, step = Math.max(1, Math.ceil(total / 30));
    const tick = () => {
      if (run.status === "canceled") { b.status = "canceled"; D.links[b.id] = demoLinks(run, bno, Math.round(b.scraped_count * 0.35)); b.passed_count = D.links[b.id].length; return refresh(); }
      b.scraped_count = Math.min(total, b.scraped_count + step);
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
      if (run.remaining_count <= 0 || bno >= MAX_BATCHES) run.status = "succeeded";
      else if (run.read_all) return (refresh(), setTimeout(() => demoBatch(run, bno + 1), 400));
      else run.status = "batch_done";
      refresh();
    };
    setTimeout(tick, 400);
  }
  // ---------- 动作 ----------
  async function count() {
    const kw = $("ssKw").value.trim(); if (!kw) return toast("请输入关键词", "bad");
    if (DEMO) return demoCount(kw);
    const { data, error } = await getSb().rpc("sel_count", { p_keyword: kw, p_params: FILTERS });
    if (error) return toast(`查询失败：${error.message}`, "bad");
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
    const { error } = await getSb().rpc("sel_start", { p_run: r.id, p_limit: lim }); if (error) return toast(`开始失败：${error.message}`, "bad"); load(r.id);
  }
  async function next(all) {
    const r = V.run, rem = r.remaining_count, nb = Math.min(MAX_BATCHES - r.current_batch, Math.ceil(rem / BATCH));
    if (all && !await confirmDialog({ title: "一键读取全部剩余？", ok: `读取 ${rem.toLocaleString()} 条`, body: `<p>将连续读取剩余 <b>${rem.toLocaleString()}</b> 条（${nb} 批，每批 1000），每批完成后结果自动出现在下方。标题规则先筛，只有通过规则的才调用豆包；预计视觉调用约 ${Math.round(rem * 0.4 * 3).toLocaleString()} 次（按回放比例估算）。</p><p class="hint">EHunt 最多只显示前 5,000 条，最多 5 批。</p>` })) return;
    if (DEMO) { r.read_all = all; return demoBatch(r, r.current_batch + 1); }
    const { error } = await getSb().rpc("sel_next_batch", { p_run: r.id, p_all: all }); if (error) return toast(error.message, "bad"); load(r.id);
  }
  async function cancel() {
    const cur = V.batches.find((b) => b.status === "running" || b.status === "queued");
    if (!await confirmDialog({ title: "停止读取？", ok: "停止", body: `<p>停止后，已读到的部分和已通过的链接会保留${cur ? `（第 ${cur.batch_no} 批已读 ${(cur.scraped_count || 0).toLocaleString()} 条）` : ""}，之后可以点「继续本批」从断点接着读。</p>` })) return;
    if (DEMO) { V.run.status = "canceled"; return refresh(); }
    const { error } = await getSb().rpc("sel_cancel", { p_run: V.run.id }); if (error) return toast(error.message, "bad"); load(V.run.id);
  }
  async function resume() {
    const b = [...V.batches].reverse().find((x) => x.status === "canceled"); if (!b) return;
    if (DEMO) { V.run.status = "running"; return demoBatch(V.run, b.batch_no, b); }
    const { error } = await getSb().rpc("sel_resume", { p_run: V.run.id }); if (error) return toast(`继续失败：${error.message}`, "bad"); load(V.run.id);
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
    const bs = bid === "all" ? V.batches : V.batches.filter((b) => b.id === bid);
    for (const b of bs) { b.copied_at = new Date().toISOString(); V.open.delete(b.id); if (!DEMO) await getSb().rpc("sel_mark_copied", { p_batch: b.id }); }
    render();
  }
  function download(bid) {
    const { list, dup } = linksOf(bid); if (!list.length) return;
    if (dup) toast(`已去重 ${dup} 条`, "ok");
    const kw = (V.run?.keyword || "links").replace(/[^\w-]+/g, "_"), tag = bid === "all" ? "all" : `b${V.batches.find((b) => b.id === bid)?.batch_no}`;
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([list.join("\n") + "\n"], { type: "text/plain" }));
    a.download = `etsy_${kw}_${tag}_${list.length}.txt`; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }
  // ---------- 最近 3 天（只读列表，不是搜索） ----------
  async function loadRecent() {
    if (DEMO) {
      const since = Date.now() - 3 * 864e5, own = Object.values(V.demoRuns).filter((d) => !d.run.id.startsWith("demo-recent")).map((d) => ({ ...d.run, created_at: d.run.created_at || new Date(Number(d.run.id.slice(5))).toISOString(), passed_count: d.batches.reduce((a, b) => a + (b.passed_count || 0), 0) }));
      const fx = [{ id: "demo-recent-1", keyword: "acrylic decoration", status: "succeeded", created_at: new Date(Date.now() - 5 * 36e5).toISOString(), passed_count: 7 },
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
    el.innerHTML = !L.length ? "" : `<div class="ss-recent"><span class="hint">最近 3 天：</span>${L.map((r) => `<a class="ss-rchip${V.run?.id === r.id ? " active" : ""}" href="#/selscreen/${esc(r.id)}" title="${esc(ST[r.status] || r.status)}"><b>${esc(r.keyword)}</b> <span class="hint">${t(r.created_at)} · 符合 ${(r.passed_count || 0).toLocaleString()} 条</span></a>`).join("")}</div>`;
  }
  // ---------- 数据 ----------
  function refresh() { if (DEMO && V.run) { const D = V.demoRuns[V.run.id]; if (D) { V.batches = D.batches; V.links = D.links; } } render(); }
  function demoRecentRun(id) { // 演示：最近列表里的固定条目
    if (V.demoRuns[id] || !id?.startsWith("demo-recent")) return;
    const kw = id === "demo-recent-1" ? "acrylic decoration" : "wedding sign", n = id === "demo-recent-1" ? 20 : 1400;
    const run = { id, keyword: kw, status: id === "demo-recent-1" ? "succeeded" : "batch_done", total_count: n, max_rows: n, batch_size: BATCH, current_batch: 1, remaining_count: n - Math.min(n, BATCH), created_at: new Date(Date.now() - (id === "demo-recent-1" ? 5 : 30) * 36e5).toISOString() };
    const pass = id === "demo-recent-1" ? 7 : 312, b = { id: `${id}-b1`, batch_no: 1, offset_start: 1, offset_end: Math.min(n, BATCH), status: "done", scraped_count: Math.min(n, BATCH), passed_count: pass, excluded_count: Math.min(n, BATCH) - pass, excluded_by_reason: { "定制": Math.min(n, BATCH) - pass }, demo: true };
    V.demoRuns[id] = { run, batches: [b], links: { [b.id]: demoLinks(run, 1, pass) } };
  }
  function schedule() { clearTimeout(V.timer); if (!DEMO && active(V.run) && location.hash.startsWith("#/selscreen")) V.timer = setTimeout(() => load(V.run.id), 3000); }
  async function load(runId) {
    if (DEMO) { V.fx ||= (await import("./demo-selection.js")).selectionFixtures(); demoRecentRun(runId); loadRecent(); V.run = V.demoRuns[runId]?.run || null; return refresh(); }
    const sb = getSb(); if (!sb) return;
    if (!V.recent || !active(V.run)) loadRecent();
    if (!runId) { V.run = null; return render(); }
    const r = await sb.from("sel_runs").select("*").eq("id", runId).maybeSingle();
    if (r.error) { $("ssBody").innerHTML = `<div class="card empty">数据表未就绪（需要迁移 024）：${esc(r.error.message)}</div>`; return; }
    V.run = r.data; const b = await sb.from("sel_batches").select("*").eq("run_id", runId).order("batch_no"); V.batches = b.data || [];
    const it = await sb.from("sel_items").select("batch_id,etsy_url,rank").eq("run_id", runId).order("rank"); V.links = {};
    (it.data || []).forEach((x) => (V.links[x.batch_id] ||= []).push(x.etsy_url));
    render(); schedule();
  }
  // ---------- 界面 ----------
  function countPanel(r) {
    const locked = `<div class="ss-form"><label class="hint">最多读取 <input class="input input-sm ss-thr" type="number" disabled placeholder="—"> 条</label><button class="btn btn-primary" disabled title="先查询数量">抓取 →</button><span class="hint">先查询数量，显示结果数后才能抓取</span></div>`;
    if (!r || r.total_count == null) return `<div class="ss-count">${r && active(r) ? `<span><span class="spin"></span> 正在读取 EHunt「${esc(r.keyword)}」的结果数…</span>` : ""}${locked}</div>`;
    const n = r.total_count, nb = Math.ceil(Math.min(n, MAX_ROWS) / BATCH);
    const label = DEMO ? (MEASURED[r.keyword.toLowerCase()] ? `<span class="pill pill-ok">EHunt 实测（10-16 ${esc(MEASURED[r.keyword.toLowerCase()][1])}）</span>` : '<span class="pill pill-bad">演示数字，非实测</span>') : "";
    const tip = n > BATCH ? `<span class="pill pill-info">超过 1000：分批读取，每批 1000，共 ${nb} 批${n > MAX_ROWS ? "（EHunt 最多显示前 5,000 条）" : ""}</span>` : "";
    const head = `<div class="ss-count-n"><b>${esc(r.keyword)}</b>：<span class="ss-n ss-n-info">${n.toLocaleString()}</span> 条 ${tip}${label}</div>
      <p class="hint">筛选：$8–40 · 总销量 ≥100 · 7 天销量 >3 · 按 7 天销量降序</p>`;
    if (r.status === "counted" && n === 0) return `<div class="ss-count">${head}<div class="ss-form"><button class="btn btn-primary" disabled>抓取 →</button><span class="warn">0 条：建议放宽关键词或换个词再查</span></div></div>`;
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
    if (b.status === "canceled") return `<section class="ss-batch ss-batch-stop"><div class="ss-res-head"><h3>第 ${b.batch_no} 批（${range}）· <span class="pill pill-bad">已停止</span> 已读 ${(b.scraped_count || 0).toLocaleString()} / ${tot.toLocaleString()} 条，通过 <span class="ss-n ss-n-ok">${links.length}</span> 条</h3>${demo}<span class="spacer"></span>
        ${V.run?.status === "canceled" && b === [...V.batches].reverse().find((x) => x.status === "canceled") ? '<button class="btn btn-primary btn-sm" id="ssResume">继续本批</button>' : ""}${btns}</div>
      ${links.length ? `<pre class="ss-links">${esc(links.join("\n"))}</pre>` : ""}${exl}</section>`;
    if (b.status !== "done") return `<div class="ss-batch ss-batch-run"><b>第 ${b.batch_no} 批（${range}）</b> <span class="hint">${b.status === "failed" ? "失败" : `读取中… 已读 ${(b.scraped_count || 0).toLocaleString()} 条，通过 ${b.passed_count || 0} 条`}</span></div>`;
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
      : r && ["failed", "canceled"].includes(r.status) ? `<div class="ss-prog-row"><span class="pill pill-bad">${esc(ST[r.status])}</span>${r.error ? `<span class="warn">${esc(r.error)}</span>` : ""}</div>` : "";
    $("ssStart").innerHTML = `<div id="ssRecent"></div><div class="card ss-start"><div class="ss-form"><input id="ssKw" class="input" placeholder="EHunt 关键词，例如 acrylic decoration" value="${esc($("ssKw")?.value || r?.keyword || "")}">
        <button class="btn btn-primary" id="ssCount"${active(r) ? " disabled" : ""}>查询数量</button></div>${countPanel(r)}${prog}</div>`; DEMO ? loadRecent() : renderRecent();
    const done = V.batches.filter((b) => b.status === "done" || b.status === "canceled");
    const all = [...new Set(done.flatMap((b) => V.links[b.id] || []))], exAll = done.reduce((a, b) => a + (b.excluded_count || 0), 0);
    const exR = {}; done.forEach((b) => Object.entries(b.excluded_by_reason || {}).forEach(([k, v]) => exR[k] = (exR[k] || 0) + v));
    const nextBar = r && r.status === "batch_done" && r.remaining_count > 0 && r.current_batch < MAX_BATCHES ? (() => {
      const s = r.current_batch * BATCH + 1, e = Math.min((r.current_batch + 1) * BATCH, r.max_rows), nb = Math.min(MAX_BATCHES - r.current_batch, Math.ceil(r.remaining_count / BATCH));
      return `<div class="card ss-next"><div><b>还剩 ${r.remaining_count.toLocaleString()} 条未读</b><p class="hint">每批完成后更新 · EHunt 最多显示前 5,000 条，最多 5 批</p></div><span class="spacer"></span>
        <button class="btn btn-primary" id="ssNext">读取下一批（${s.toLocaleString()}–${e.toLocaleString()}）</button>${nb > 1 ? `<button class="btn btn-ghost" id="ssAll">一键读取全部剩余（${r.remaining_count.toLocaleString()} 条，${nb} 批）</button>` : ""}</div>`; })() : "";
    const cum = done.length > 1 || (r && r.status !== "succeeded" && done.length) ? `<div class="card ss-cum"><b>累计符合要求 <span class="ss-n ss-n-ok">${all.length.toLocaleString()}</span> 条</b><span class="hint">（${done.length} 批 · 已读 ${done.reduce((a, b) => a + b.scraped_count, 0).toLocaleString()} 条）</span>${V.batches.some((b) => b.demo) ? '<span class="pill pill-bad">演示数字</span>' : ""}<span class="spacer"></span>
        <button class="btn btn-primary btn-sm" data-sscopy="all"${all.length ? "" : " disabled"}>复制全部批次</button><button class="btn btn-ghost btn-sm" data-ssdl="all"${all.length ? "" : " disabled"}>下载 TXT</button></div>` : "";
    body.innerHTML = !r ? '<div class="card empty"><div class="ico">◎</div><h3>输入关键词，先“查询数量”</h3><p class="muted">再填写最多读取条数点“抓取”</p></div>'
      : `${cum}${V.batches.map(batchBlock).join("")}${nextBar}
        ${done.length ? `<p class="hint ss-note">已排除合计 ${exAll.toLocaleString()}${exAll ? `：${esc(sumReasons(exR))}` : ""}（原因只记在 box 本地日志，3 天后删除）· 仅保留最近 3 天的筛选结果 · 网页不提供搜索，查历史请在对话里问序猿</p>` : ""}`;
  }
  document.addEventListener("click", (e) => {
    const t = e.target, id = t.id;
    if (id === "ssCount") return count(); if (id === "ssGo") return start(); if (id === "ssStop") return cancel();
    if (id === "ssResume") return resume();
    if (t.dataset?.ssps) return prescreen(t.dataset.ssps);
    if (id === "ssNext") return next(false); if (id === "ssAll") return next(true);
    if (t.dataset?.sscopy) return copy(t.dataset.sscopy);
    if (t.dataset?.ssdl) return download(t.dataset.ssdl);
    if (t.dataset?.ssopen) { V.open.add(t.dataset.ssopen); return render(); }
    if (t.dataset?.ssfold) { V.open.delete(t.dataset.ssfold); return render(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    if (e.target.id === "ssKw" && !$("ssCount")?.disabled) { e.preventDefault(); count(); }
    else if (e.target.id === "ssLimit") { e.preventDefault(); start(); }
  });
  document.addEventListener("input", (e) => { if (e.target.id === "ssLimit") { V.limErr = ""; e.target.classList.remove("input-bad"); const el = $("ssLimErr"); if (el) el.textContent = ""; } });
  return { show: (runId) => load(runId), _state: V };
}
