// 051 预筛选对账：每个预筛选批次「确认 N · 已进任务 M · 产品库 L」，N = M + L 为绿，否则红「缺 K」；展开看每条与原因，「去任务」跳到我的任务对应行；
// 已删除（隐藏）未确认的条目单独一栏，可「撤销删除」（screen_unhide）。数据：screen_reconcile()（只读）。
export const REASON = { task: "已进任务", library: "在产品库", failed: "已进任务 · 分析失败", missing: "缺失" };
export function reconTotals(list) {
  const t = { batches: 0, confirmed: 0, in_tasks: 0, in_library: 0, failed: 0, missing: 0, deleted: 0 };
  for (const b of list || []) { t.batches++; for (const k of ["confirmed", "in_tasks", "in_library", "failed", "missing", "deleted"]) t[k] += Number(b[k] || 0); }
  return t;
}
// 缺 K 以 N − M − L 计（与服务端 missing 双重核对：两者取大，任何不一致都显示红）
export function batchGap(b) { return Math.max(Number(b.missing || 0), Number(b.confirmed || 0) - Number(b.in_tasks || 0) - Number(b.in_library || 0)); }
export function reasonText(it) {
  if (it.reason === "task" || it.reason === "failed") return `${REASON[it.reason]}（第 ${it.task_row} 行）`;
  return REASON[it.reason] || "未知";
}
export const sortAt = (j) => j.sort_at || j.screened_at || j.created_at || "";
export function taskTotalLine({ total, shown, libCount, rec, demo = false }) {
  const parts = [];
  if (total != null) parts.push(`共 <b>${total}</b> 个任务 · 已显示 ${shown}${total > shown ? "（点底部「加载更多」看其余）" : ""}`);
  if (libCount) parts.push(`已加入产品库的 ${libCount} 个在「产品库」查看，不在此列表`);
  let html = parts.length ? `<div class="task-total hint">${parts.join(" · ")}</div>` : "";
  if (rec && rec.confirmed) {
    const gap = rec.confirmed - rec.in_tasks - rec.in_library, bad = gap > 0 || rec.missing > 0;
    html += `<div class="recon-line ${bad ? "recon-bad" : "recon-ok"}" data-recon-line="1">预筛选已确认 <b>${rec.confirmed}</b> · 任务 <b>${rec.in_tasks}</b> · 产品库 <b>${rec.in_library}</b> · ${bad ? `<b>缺 ${Math.max(gap, rec.missing)}</b>` : "缺 0 ✓"} <a href="#/screen" class="recon-link">查看对账</a>${demo ? ' <span class="pill pill-bad">演示数字</span>' : ""}</div>`;
  }
  return html;
}
export function reconPanelHtml(list, esc, open = new Set(), tab = new Map(), demo = false) {
  if (!list || !list.length) return "";
  const t = reconTotals(list), gapAll = list.reduce((s, b) => s + batchGap(b), 0);
  const rows = list.map((b) => {
    const gap = batchGap(b), key = String(b.batch_id), isOpen = open.has(key), tb = tab.get(key) || "items";
    const head = `<button type="button" class="recon-head ${gap ? "recon-bad" : "recon-ok"}" data-recon-toggle="${esc(key)}" aria-expanded="${isOpen}">
      <span class="recon-name">${esc(b.name || "（未命名批次）")}</span>
      <span class="recon-nums">确认 <b>${b.confirmed}</b> · 已进任务 <b>${b.in_tasks}</b> · 产品库 <b>${b.in_library}</b>${b.failed ? ` · 分析失败 ${b.failed}` : ""}${b.deleted ? ` · 已删除 ${b.deleted}` : ""}</span>
      <span class="recon-badge">${gap ? `缺 ${gap}` : b.confirmed ? "✓ 一致" : "—"}</span></button>`;
    if (!isOpen) return `<div class="recon-row">${head}</div>`;
    const tabs = `<div class="recon-tabs"><button type="button" class="fchip ${tb === "items" ? "active" : ""}" data-recon-tab="${esc(key)}:items">已确认 ${b.confirmed}</button><button type="button" class="fchip ${tb === "deleted" ? "active" : ""}" data-recon-tab="${esc(key)}:deleted">已删除 ${b.deleted}</button>${tb === "deleted" && b.deleted ? `<span class="spacer"></span><button type="button" class="btn btn-ghost btn-xs" data-recon-unhide="${esc((b.deleted_items || []).map((d) => d.id).join(","))}">全部撤销删除（${b.deleted}）</button>` : ""}</div>`;
    const items = tb === "items" ? (b.items || []).map((it) => `<li class="recon-it r-${esc(it.reason || "")}"><span class="mono">#${esc(it.listing_id)}</span> <span class="recon-t">${esc(it.title || "")}</span> <span class="pill ${it.reason === "missing" ? "pill-bad" : it.reason === "failed" ? "pill-warn" : "pill-info"}">${esc(reasonText(it))}</span>${it.error ? ` <span class="hint">${esc(it.error)}</span>` : ""}${it.reason === "task" || it.reason === "failed" ? ` <a href="#/" data-recon-go="${esc(it.id)}" data-row="${esc(it.task_row)}">去任务</a>` : it.reason === "library" ? ' <a href="#/library">去产品库</a>' : ""}</li>`)
      : (b.deleted_items || []).map((d) => `<li class="recon-it"><span class="mono">#${esc(d.listing_id)}</span> <span class="recon-t">${esc(d.title || "")}</span> <button type="button" class="btn btn-ghost btn-xs" data-recon-unhide="${esc(d.id)}">撤销删除</button></li>`);
    return `<div class="recon-row open">${head}${tabs}<ul class="recon-list">${items.join("") || '<li class="hint">无</li>'}</ul></div>`;
  }).join("");
  return `<details class="card recon" ${gapAll ? "open" : ""}><summary><b>对账</b>${demo ? ' <span class="pill pill-bad">演示数字</span>' : ""} <span class="${gapAll ? "recon-bad" : "recon-ok"}">已确认 ${t.confirmed} · 已进任务 ${t.in_tasks} · 产品库 ${t.in_library} · ${gapAll ? `缺 ${gapAll}` : "缺 0 ✓"}</span><span class="hint"> · ${t.batches} 个批次 · 已删除 ${t.deleted}</span></summary><div class="recon-rows">${rows}</div></details>`;
}
export function createReconcile({ $, esc, getSb, DEMO, toast, demoData, onGoTask, onChanged }) {
  const R = { list: null, open: new Set(), tab: new Map(), at: 0, busy: false };
  async function load(force = false) {
    if (DEMO) { R.list = R.list || demoData(); return R.list; }
    if (!force && Date.now() - R.at < 15000 && R.list) return R.list;
    const sb = getSb(); if (!sb) return R.list;
    const { data, error } = await sb.rpc("screen_reconcile");
    if (error) { if (!/screen_reconcile|PGRST202|function/i.test(error.message || "")) console.warn("screen_reconcile", error); R.list = R.list || []; return R.list; }
    R.list = data || []; R.at = Date.now(); return R.list;
  }
  const totals = () => { if (DEMO && !R.list) R.list = demoData(); return R.list ? reconTotals(R.list) : null; };
  function render() { const el = $("screenRecon"); if (el) el.innerHTML = reconPanelHtml(R.list || [], esc, R.open, R.tab, DEMO); }
  async function refresh(force = true) { await load(force); render(); onChanged && onChanged(); }
  async function unhide(ids) {
    if (R.busy || !ids.length) return; R.busy = true;
    try {
      if (!DEMO) { const { data, error } = await getSb().rpc("screen_unhide", { p_ids: ids }); if (error) return toast(`撤销失败：${error.message}`, "bad"); toast(`已撤销删除 ${data ?? ids.length} 条，回到预筛选待确认`, "ok"); }
      else { const gone = new Set(ids); R.list.forEach((b) => { const n = (b.deleted_items || []).filter((d) => gone.has(d.id)).length; b.deleted_items = (b.deleted_items || []).filter((d) => !gone.has(d.id)); b.deleted -= n; }); toast(`已撤销删除 ${ids.length} 条（演示）`, "ok"); }
      await refresh(true);
    } finally { R.busy = false; }
  }
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest?.("[data-recon-toggle],[data-recon-tab],[data-recon-unhide],[data-recon-go]"); if (!t) return;
    if (t.dataset.reconToggle) { const k = t.dataset.reconToggle; R.open.has(k) ? R.open.delete(k) : R.open.add(k); return render(); }
    if (t.dataset.reconTab) { const [k, v] = t.dataset.reconTab.split(":"); R.tab.set(k, v); return render(); }
    if (t.dataset.reconUnhide) { ev.preventDefault(); return unhide(t.dataset.reconUnhide.split(",").filter(Boolean)); }
    if (t.dataset.reconGo) { ev.preventDefault(); onGoTask && onGoTask(t.dataset.reconGo, Number(t.dataset.row) || 0); }
  });
  return { load, render, refresh, totals, state: R };
}
