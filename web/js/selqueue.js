// 获取链接「抓取队列」（037，Hongyan 2026-10-20）：下载管理器式竖向列表。
// box 只有 1 个抓取进程 / 1 个浏览器 → 一次只抓一个，其余按顺序等待（不并行，不拖慢抓取）。
// 正在抓取（进度条）→ 等待中（可暂停/继续/取消/上移下移）→ 有问题（失败/卡住/box 离线，原因 + 刷新/重新抓取：原地从断点续读）→ 已结束（可删除）
export const STALL_S = 600;       // 与 037 sel_resume / sel_claim_run 的 10 分钟一致：超过才允许「重新抓取」卡住的任务
export const WARN_S = 180;        // 3 分钟没更新进度先提示
export const WAIT_STATES = ["queued", "paused"], RUN_STATES = ["running"], END_STATES = ["succeeded", "batch_done", "failed", "canceled", "counted"];
const sec = (iso, now) => (iso ? Math.max(0, (now - Date.parse(iso)) / 1000) : Infinity);
const qkey = (r) => Date.parse(r.queue_at || r.created_at || 0);
// ---- 纯函数（web/tests/selscreen.test.mjs）----
// 每条 run → 队列里的一行：分组、状态文字、原因、可用按钮
export function queueItem(r, batch, now = Date.now()) {
  const b = batch || null, P = r.progress || {}, beat = sec(P.at || r.heartbeat_at, now);
  const base = { id: r.id, keyword: r.keyword, status: r.status, batch_no: b?.batch_no ?? r.current_batch ?? null, range: b ? [b.offset_start, b.offset_end] : null, acts: [], reason: "" };
  if (r.status === "running") {
    if (beat > STALL_S) return { ...base, group: "problem", label: "卡住", reason: `box 抓取进程 ${Math.round(beat / 60)} 分钟没有更新进度（可能离线或浏览器卡死）`, acts: ["retry", "cancel"] };
    const target = P.target || (b ? b.offset_end - b.offset_start + 1 : 0), judged = Math.max(P.judged || 0, b?.scraped_count || 0), read = Math.max(judged, P.read || 0);
    return { ...base, group: "running", label: P.stage === "vision" ? "豆包判断中" : P.retry > 0 ? (P.retry_why && P.retry_why !== "超时" ? "页面重试中" : "单页超时重试中") : P.stage === "read_setup" ? "打开 EHunt" : "抓取中", warn: beat > WARN_S,
      pctR: target ? Math.min(100, (100 * read) / target) : 0, pctJ: target ? Math.min(100, (100 * judged) / target) : 0, read, judged, target, passed: Math.max(P.passed || 0, b?.passed_count || 0), page: P.page, acts: ["pause", "cancel"] };
  }
  if (r.status === "counting" || r.status === "count_queued") return { ...base, group: "running", label: r.status === "counting" ? "查询数量中" : "排队查询数量", counting: true, acts: ["cancel"] };
  if (r.status === "queued") return { ...base, group: "wait", label: "等待", resumeAt: b?.scraped_count ? b.offset_start + b.scraped_count : null, acts: ["pause", "cancel", "up", "down"] };
  if (r.status === "paused") return { ...base, group: "wait", label: "暂停", resumeAt: b?.scraped_count ? b.offset_start + b.scraped_count : null, acts: ["resume", "cancel", "up", "down"] };
  if (r.status === "failed" || (r.status === "canceled" && r.error)) {
    const can = !!b; // 查询数量失败（没有批次）只能重新查询
    return { ...base, group: "problem", label: r.status === "failed" ? "失败" : "中断", reason: r.error || "", acts: can ? ["retry", "delete"] : ["delete"], done: b?.scraped_count || 0 };
  }
  return { ...base, group: "end", label: { succeeded: "完成", batch_done: "本批完成", canceled: "已停止", counted: (r.total_count > 0 ? "自动入队中" : "0 条") }[r.status] || r.status, passed: r.passed_count || 0, acts: r.status === "canceled" && b ? ["retry", "delete"] : ["delete"] };
}
// 整个队列：正在抓 → 等待（按 queue_at，即点「抓取」的先后 / 上移下移后的顺序）→ 有问题 → 已结束（最新在前）
export function buildQueue(runs, batchOf = () => null, now = Date.now()) {
  const items = runs.map((r) => ({ ...queueItem(r, batchOf(r.id), now), _k: qkey(r), _c: Date.parse(r.created_at || 0) }));
  const g = (k) => items.filter((x) => x.group === k);
  const wait = g("wait").sort((a, b) => a._k - b._k); wait.forEach((x, i) => { x.pos = i + 1; if (i === 0) x.acts = x.acts.filter((a) => a !== "up"); if (i === wait.length - 1) x.acts = x.acts.filter((a) => a !== "down"); });
  const running = g("running").sort((a, b) => (a.counting ? 1 : 0) - (b.counting ? 1 : 0));
  // box 离线：有等待的任务、却没有在抓的（且最早的等待超过 3 分钟）
  const offline = !running.some((x) => !x.counting) && wait.some((x) => x.status === "queued") && runs.some((r) => r.status === "queued" && sec(r.queue_at || r.created_at, now) > WARN_S);
  return { running, wait, problem: g("problem").sort((a, b) => b._c - a._c), end: g("end").sort((a, b) => b._c - a._c), offline };
}
// 本地乐观更新（演示 / 点了按钮等 3 秒轮询前）：和 037 RPC 的状态转换一致
export function applyAction(r, act, now = Date.now()) {
  const iso = new Date(now).toISOString();
  if (act === "pause" && ["queued", "running"].includes(r.status)) return { ...r, status: "paused" };
  if ((act === "resume" || act === "retry") && (["paused", "failed", "canceled"].includes(r.status) || (r.status === "running" && sec(r.progress?.at || r.heartbeat_at, now) > STALL_S)))
    return { ...r, status: "queued", error: null, finished_at: null, queue_at: r.status === "paused" ? r.queue_at || r.created_at : iso };
  if (act === "cancel" && ["count_queued", "counting", "counted", "queued", "running", "paused"].includes(r.status)) return { ...r, status: "canceled", finished_at: iso };
  return r;
}
export const canDelete = (r) => !["count_queued", "counting", "queued", "running", "paused"].includes(r.status);

export function createSelQueue({ $, esc, toast, getSb, DEMO, confirmDialog, friendlyErr, onChange }) {
  const Q = { runs: [], batches: {}, timer: null, busy: false, demo: null };
  const QSP = new URLSearchParams(location.search);
  const RPC = { pause: "sel_pause", resume: "sel_resume", retry: "sel_resume", cancel: "sel_cancel" };
  const n0 = (v) => Number(v || 0).toLocaleString();
  function demoFixtures(kind) { // ?demo=1&squeue=running|paused|failed|empty —— 全部是演示数字
    const now = Date.now(), iso = (s) => new Date(now - s * 1000).toISOString(), B = (rid, no, s, e, st, sc = 0, pc = 0) => ({ id: `${rid}-b${no}`, run_id: rid, batch_no: no, offset_start: s, offset_end: e, status: st, scraped_count: sc, passed_count: pc });
    const runs = [], bat = {}; const add = (r, b) => { runs.push({ created_at: iso(3600), ...r }); if (b) bat[r.id] = b; };
    if (kind !== "empty") {
      add({ id: "dq-run", keyword: "acrylic sign", status: "running", max_rows: 3000, read_all: true, current_batch: 2, queue_at: iso(1800), heartbeat_at: iso(2), progress: { stage: "reading", batch_no: 2, target: 1000, read: 468, judged: 431, passed: 149, page: 74, at: iso(2) } }, B("dq-run", 2, 1001, 2000, "running", 431, 149));
      add({ id: "dq-w1", keyword: "wedding sign", status: kind === "paused" ? "paused" : "queued", max_rows: 1400, current_batch: 1, queue_at: iso(1500) }, B("dq-w1", 1, 1, 1000, kind === "paused" ? "canceled" : "queued", kind === "paused" ? 320 : 0));
      add({ id: "dq-w2", keyword: "cake topper", status: "queued", max_rows: 500, current_batch: 1, queue_at: iso(1200) }, B("dq-w2", 1, 1, 500, "queued"));
      add({ id: "dq-w3", keyword: "christmas ornament", status: "queued", max_rows: 2000, read_all: true, current_batch: 1, queue_at: iso(900) }, B("dq-w3", 1, 1, 1000, "queued"));
      if (kind === "failed") add({ id: "dq-f1", keyword: "name plate", status: "failed", error: "Remote end closed connection without response", current_batch: 1, created_at: iso(2400), queue_at: iso(2400) }, B("dq-f1", 1, 1, 1000, "failed", 286, 97));
      if (kind === "failed") add({ id: "dq-f2", keyword: "door hanger", status: "running", current_batch: 1, created_at: iso(5000), queue_at: iso(5000), heartbeat_at: iso(1500), progress: { stage: "reading", at: iso(1500) } }, B("dq-f2", 1, 1, 600, "running", 120, 40));
      add({ id: "dq-e1", keyword: "gift", status: "succeeded", passed_count: 6, created_at: iso(6 * 3600) }, B("dq-e1", 1, 1, 200, "done", 200, 6));
      add({ id: "dq-e2", keyword: "keychain", status: "canceled", passed_count: 41, created_at: iso(9 * 3600) }, B("dq-e2", 1, 1, 1000, "canceled", 130, 41));
    }
    return { runs, bat };
  }
  async function load() {
    if (DEMO) { if (!Q.demo) { const f = demoFixtures(QSP.get("squeue") || "running"); Q.demo = f; } Q.runs = Q.demo.runs; Q.batches = Q.demo.bat; return render(); }
    const sb = getSb(); if (!sb) return;
    const since = new Date(Date.now() - 3 * 864e5).toISOString();
    const r = await sb.from("sel_runs").select("id,keyword,status,error,created_at,queue_at,heartbeat_at,progress,current_batch,max_rows,read_all,passed_count,total_count")
      .or(`status.in.(count_queued,counting,queued,running,paused),created_at.gte.${since}`).order("created_at", { ascending: false }).limit(40);
    if (r.error) { if (!/queue_at/.test(r.error.message)) toast(`读取抓取队列失败：${r.error.message}`, "bad"); Q.runs = []; return render(); } // 未执行 037：不显示队列
    Q.runs = r.data || [];
    const ids = Q.runs.filter((x) => x.status !== "counted" && x.total_count != null).map((x) => x.id), bat = {};
    if (ids.length) { const b = await sb.from("sel_batches").select("id,run_id,batch_no,offset_start,offset_end,status,scraped_count,passed_count").in("run_id", ids).order("batch_no", { ascending: false });
      (b.data || []).forEach((x) => { bat[x.run_id] ||= x; }); }
    Q.batches = bat; render(); schedule();
  }
  function schedule() { clearTimeout(Q.timer); if (!DEMO && location.hash.startsWith("#/selscreen") && Q.runs.some((x) => ["count_queued", "counting", "queued", "running"].includes(x.status))) Q.timer = setTimeout(load, 4000); }
  const BTN = { pause: ["暂停", "btn-ghost"], resume: ["▶ 继续", "btn-primary"], cancel: ["取消", "btn-ghost"], retry: ["⟳ 重新抓取", "btn-primary"], up: ["↑", "btn-ghost"], down: ["↓", "btn-ghost"], delete: ["删除", "btn-ghost ss-q-del"] };
  const TIP = { pause: "暂停：不再抓这个任务；正在抓的会停在断点，已读部分保留", resume: "继续：回到原来的排队位置，从断点接着读", cancel: "取消：停止并移出队列（已通过的链接保留）", retry: "刷新：原地重新排队，从断点接着读（不删除、不新建任务）", up: "上移", down: "下移", delete: "删除这条任务和它的链接" };
  function row(x) {
    const btns = x.acts.map((a) => `<button class="btn ${BTN[a][1]} btn-sm" data-sq="${a}" data-sqid="${esc(x.id)}" title="${esc(TIP[a])}">${BTN[a][0]}</button>`).join("");
    const rng = x.range ? `第 ${x.batch_no} 批（${n0(x.range[0])}–${n0(x.range[1])}）` : "";
    const pill = { running: "pill-info", wait: x.status === "paused" ? "pill-warn" : "pill-mute", problem: "pill-bad", end: ["succeeded", "batch_done"].includes(x.status) ? "pill-ok" : "pill-mute" }[x.group];
    let sub = "";
    if (x.group === "running" && !x.counting) sub = `<div class="ss-pg-bar" role="progressbar" aria-valuenow="${Math.round(x.pctJ)}"><i class="rd" style="width:${x.pctR}%"></i><i class="jd" style="width:${x.pctJ}%"></i></div>
      <div class="hint">已读 <b>${n0(x.read)}</b> / ${n0(x.target)} 条 · <b>${Math.floor(x.pctJ)}%</b>${x.page ? ` · 第 ${x.page} 页` : ""} · 通过 <b class="ss-n-ok">${n0(x.passed)}</b>${x.warn ? ' · <span class="warn">⚠ 3 分钟以上没有更新进度</span>' : ""}</div>`;
    else if (x.group === "wait") sub = `<div class="hint">${x.status === "paused" ? "已暂停，不会被抓取；点「继续」回到第 " + x.pos + " 位" : `排第 ${x.pos} 位，等前面的抓完自动开始`}${x.resumeAt ? ` · 从第 ${n0(x.resumeAt)} 条接着读` : ""}</div>`;
    else if (x.group === "problem") sub = `<div class="warn" title="${esc(x.reason)}">原因：${esc(friendlyErr(x.reason) || x.reason || "未知")}${x.done ? `<span class="hint"> · 已读 ${n0(x.done)} 条已保留，重新抓取从断点接着读</span>` : ""}</div>`;
    else sub = x.status === "counted" ? `<div class="hint">已查数量，正在自动加入抓取队列（0 条不抓）</div>` : `<div class="hint">符合 ${n0(x.passed)} 条</div>`;
    return `<li class="ss-q-row ss-q-${x.group}${x.status === "paused" ? " ss-q-paused" : ""}"><div class="ss-q-main"><span class="ss-q-pos">${x.group === "running" ? (x.counting ? "…" : '<span class="spin"></span>') : x.group === "wait" ? x.pos : x.group === "problem" ? "!" : "✓"}</span>
      <a class="ss-q-kw" href="#/selscreen/${esc(x.id)}">${esc(x.keyword)}</a><span class="hint">${rng}</span><span class="pill ${pill}">${esc(x.label)}</span><span class="spacer"></span><span class="ss-q-acts">${btns}</span></div>${sub}</li>`;
  }
  function render() {
    const el = $("ssQueue"); if (!el) return;
    if (!DEMO && !Q.runs.length) { el.innerHTML = ""; return; }
    const q = buildQueue(Q.runs, (id) => Q.batches[id], Date.now()), act = q.running.length + q.wait.length;
    const sec = (t, L, empty = "") => (L.length ? `<div class="ss-q-sec">${t}</div><ol class="ss-q-list">${L.map(row).join("")}</ol>` : empty);
    el.innerHTML = `<div class="card ss-queue"><div class="ss-q-head"><b>抓取队列</b><span class="hint">box 一次抓 1 个，其余按顺序等待 · 进行中 ${act} 个${q.problem.length ? ` · <span class="warn">有问题 ${q.problem.length} 个</span>` : ""}</span>${DEMO ? '<span class="pill pill-bad">演示数字</span>' : ""}</div>
      ${q.offline ? '<p class="warn ss-q-off">⚠ 有任务在等待，但 box 抓取进程没有在抓（box 可能离线或暂停）。恢复后会按顺序自动开始，不需要重新点。</p>' : ""}
      ${act ? "" : '<p class="hint ss-q-empty">队列为空：查询数量成功后会自动加入（也可手动改条数）；多个关键词按顺序排队。</p>'}
      ${sec("正在抓取", q.running)}${sec(`等待中（${q.wait.length}）`, q.wait)}${sec("有问题", q.problem)}${q.end.length ? `<details class="ss-q-endd"${QSP.has("qend") ? " open" : ""}><summary class="ss-q-sec">已结束（${q.end.length}，最近 3 天）</summary><ol class="ss-q-list">${q.end.map(row).join("")}</ol></details>` : ""}</div>`;
  }
  async function act(a, id) {
    const r = Q.runs.find((x) => x.id === id); if (!r || Q.busy) return;
    if (a === "cancel" && !await confirmDialog({ title: `取消「${r.keyword}」？`, ok: "取消任务", body: "<p>停止并移出队列，已读到的部分和已通过的链接会保留。</p>" })) return;
    if (a === "delete") {
      if (!canDelete(r)) return toast("任务进行中，请先取消再删除", "bad");
      if (!await confirmDialog({ title: `删除「${r.keyword}」？`, ok: "删除", danger: true, body: `<p>将删除这条任务、它的全部批次和 <b>${n0(r.passed_count)}</b> 条符合要求的链接，不能恢复。</p>` })) return;
    }
    Q.busy = true;
    try {
      if (DEMO) {
        if (a === "delete") Q.demo.runs = Q.runs = Q.runs.filter((x) => x.id !== id);
        else if (a === "up" || a === "down") swap(r, a);
        else { const i = Q.runs.indexOf(r); Q.runs[i] = applyAction(r, a); const b = Q.batches[id]; if (b && Q.runs[i].status === "queued") b.status = "queued"; }
        toast({ pause: "已暂停", resume: "已继续，回到原排队位置", retry: "已重新加入队列，将从断点接着读", cancel: "已取消", delete: "已删除", up: "已上移", down: "已下移" }[a], "ok"); return render();
      }
      const sb = getSb();
      const { data, error } = a === "up" || a === "down" ? await sb.rpc("sel_queue_move", { p_run: id, p_dir: a === "up" ? -1 : 1 }) : a === "delete" ? await sb.rpc("sel_delete_run", { p_run: id }) : await sb.rpc(RPC[a], { p_run: id });
      if (error) toast(/Could not find the function|does not exist/.test(error.message) ? "需要后台先执行 037 迁移" : error.message, "bad");
      else if (data === false) toast("任务状态已变化，已刷新", "bad");
      else toast({ pause: "已暂停（正在抓的会在几秒内停在断点）", resume: "已继续", retry: "已重新加入队列，将从断点接着读", cancel: "已取消", delete: "已删除" }[a] || "已调整顺序", "ok");
      await load(); onChange?.(id, a);
    } finally { Q.busy = false; }
  }
  function swap(r, a) { const q = buildQueue(Q.runs, (id) => Q.batches[id]).wait, i = q.findIndex((x) => x.id === r.id), o = q[i + (a === "up" ? -1 : 1)]; if (!o) return;
    const R = (id) => Q.runs.find((x) => x.id === id), ka = r.queue_at, ro = R(o.id); r.queue_at = ro.queue_at; ro.queue_at = ka; }
  document.addEventListener("click", (e) => { const t = e.target.closest?.("[data-sq]"); if (t) act(t.dataset.sq, t.dataset.sqid); });
  // 演示（?demoqueue=1）：新任务进入抓取队列末尾（演示数字）
  function demoAdd(run, batch) { if (!DEMO) return; if (!Q.demo) Q.demo = demoFixtures(QSP.get("squeue") || "running"); Q.demo.runs.push({ created_at: new Date().toISOString(), ...run }); if (batch) Q.demo.bat[run.id] = batch; Q.runs = Q.demo.runs; Q.batches = Q.demo.bat; render(); }
  return { load, render, demoAdd, _state: Q };
}
