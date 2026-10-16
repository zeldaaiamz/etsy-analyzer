// 选品预筛（#/selscreen[/<run>]）：输入关键词点“开始” → sel_start 建任务 → ECS selection worker 用已登录浏览器逐条读 EHunt 列表页
// → 每条立即分类（convertible + 可选定制保留，视觉×3）→ 逐条出现在这里（3 秒轮询，带进度条）→ 人工复核，
// 只有“已通过”（机器保留 + 人工确认的待确认）才进入 #/select。样式/交互沿用 #/screen 的 sc-* 卡片与 ✕/撤销按钮，不改动 #/screen。
const GATE_NAME = { pending: "待确认", passed: "已通过", rejected: "已排除" };
export function createSelScreen({ $, esc, imgUrl, toast, getSb, DEMO }) {
  const V = { runs: [], run: null, items: [], tab: "all", timer: null, fx: null };
  const active = (r) => r && (r.status === "queued" || r.status === "running");
  // 演示：模拟 worker 逐条抓取 + 分类（scraped → classified），不连 Supabase
  async function demoStart(kw, limit) {
    const fx = V.fx, src = fx.items["demo-run"].map((x) => ({ ...x })), id = `demo-${Date.now()}`;
    const run = { id, keyword: kw, status: "running", target_count: Math.min(limit, src.length), scraped_count: 0, classified_count: 0, created_at: new Date().toISOString(), stats: { raw_rows: 0 } };
    fx.runs.unshift(run); fx.items[id] = []; location.hash = `#/selscreen/${id}`;
    const raw = JSON.parse(JSON.stringify(src)).sort((a, b) => a.ranks[0] - b.ranks[0]); let i = 0;
    const step = () => {
      if (run.status === "canceled") return;
      if (i >= raw.length || run.scraped_count >= run.target_count) { run.status = "succeeded"; run.stats.raw_rows = run.scraped_count; run.stats.items = fx.items[id].length; return rerender(); }
      const it = { ...raw[i++], id: `${id}-${raw[i - 1].etsy_listing_id}`, item_state: "scraped", gate: "pending", verdict: "待确认", reason: "", note: "", gate_by: "auto" };
      const full = { ...raw[i - 1] }; fx.items[id].push(it); run.scraped_count++; rerender();
      setTimeout(() => { Object.assign(it, full, { id: it.id, item_state: "classified" }); run.classified_count++; rerender(); }, 600);
      setTimeout(step, Number(QSP.get("speed")) || 220);
    };
    setTimeout(step, 500);
  }
  const QSP = new URLSearchParams(location.search);
  const rerender = () => { if (location.hash.startsWith("#/selscreen")) { V.items = V.fx.items[V.run?.id] || V.items; render(); } };
  async function start() {
    const kw = $("ssKw").value.trim(), limit = Number($("ssLimit").value) || 100;
    if (!kw) return toast("请输入关键词", "bad");
    if (DEMO) return demoStart(kw, limit);
    const { data, error } = await getSb().rpc("sel_start", { p_keyword: kw, p_params: { limit, price_min: 8, price_max: 40, min_sales: 100, sort: "sales_7d" } });
    if (error) return toast(`开始失败：${error.message}`, "bad");
    toast("已创建任务，等待服务器开始读取 EHunt", "ok"); location.hash = `#/selscreen/${data}`;
  }
  async function cancel() {
    if (DEMO) { V.run.status = "canceled"; return render(); }
    const { error } = await getSb().rpc("sel_cancel", { p_run: V.run.id }); if (error) return toast(error.message, "bad"); load(V.run.id);
  }
  function schedule() { clearTimeout(V.timer); if (!DEMO && active(V.run) && location.hash.startsWith("#/selscreen")) V.timer = setTimeout(() => load(V.run.id), 3000); }
  async function load(runId) {
    if (DEMO) { V.fx ||= (await import("./demo-selection.js")).selectionFixtures(); const fx = V.fx; V.runs = fx.runs; V.run = V.runs.find((r) => r.id === runId) || V.runs[0]; V.items = fx.items[V.run.id] || [];
      if (QSP.has("stream") && !V.autoStarted) { V.autoStarted = true; render(); $("ssKw").value = QSP.get("stream") || "acrylic decoration"; return demoStart($("ssKw").value, 100); }
      return render(); }
    const sb = getSb(); if (!sb) return;
    const rs = await sb.from("sel_runs").select("id,keyword,created_at,stats,status,target_count,scraped_count,classified_count,error,heartbeat_at").order("created_at", { ascending: false }).limit(50);
    if (rs.error) { $("ssBody").innerHTML = `<div class="card empty">选品数据表未就绪（需要迁移 024）：${esc(rs.error.message)}</div>`; return; }
    V.runs = rs.data || []; V.run = V.runs.find((r) => r.id === runId) || V.runs[0] || null;
    if (!V.run) { V.items = []; return render(); }
    const { data, error } = await sb.rpc("sel_list", { p_run: V.run.id, p_stage: "screen" });
    if (error) return toast(`加载失败：${error.message}`, "bad");
    V.items = data || []; render(); schedule();
  }
  const vCls = (v) => v === "保留" ? "ok" : v === "待确认" ? "warn" : "bad";
  function card(it) {
    if (it.item_state !== "classified" && it.item_state !== "error") return `<div class="sc-card wait ss-new" data-skey="${esc(it.id)}"><span class="sc-img"><img class="on" src="${imgUrl(it.image_url)}" alt=""><span class="sc-tag ss-tag-run"><span class="spin"></span> 分类中</span></span>
      <div class="sc-info"><div class="sc-title">${esc(it.title)}</div><div class="sc-meta"><span class="hint">#${esc(it.etsy_listing_id)}</span></div><div class="ss-reason hint">规则 + 视觉判断中（3 次）…</div></div></div>`;
    const g = it.gate, rj = g === "rejected", ok = g === "passed";
    const act = rj ? `<button class="sc-x sc-undo" data-ssgate="pending" data-ssid="${esc(it.id)}" title="恢复为待确认">恢复</button>`
      : `${g === "pending" ? `<button class="sc-x ss-pass" data-ssgate="passed" data-ssid="${esc(it.id)}" title="确认通过，进入选品筛选" aria-label="通过">✓</button>` : ""}<button class="sc-x" data-ssgate="rejected" data-ssid="${esc(it.id)}" title="排除" aria-label="排除">✕</button>`;
    const tag = rj ? '<span class="sc-tag">已排除</span>' : ok ? '<span class="sc-tag sc-kept">已通过</span>' : '<span class="sc-tag ss-tag-q">待确认</span>';
    const price = it.price_min == null ? "" : Number(it.price_min) === Number(it.price_max) ? `$${Number(it.price_min).toFixed(2)}` : `$${Number(it.price_min).toFixed(2)}–${Number(it.price_max).toFixed(2)}`;
    return `<div class="sc-card${rj ? " rej" : ""}" data-skey="${esc(it.id)}">
      <a class="sc-img" href="https://www.etsy.com/listing/${esc(it.etsy_listing_id)}" target="_blank" rel="noopener"><img class="on" loading="lazy" src="${imgUrl(it.image_url)}" alt="">${tag}</a>${act}
      <div class="sc-info"><div class="sc-title" title="${esc(it.title)}">${esc(it.title)}</div>
        <div class="sc-meta"><b>${esc(price)}</b><span class="hint">#${esc(it.etsy_listing_id)}</span></div>
        <div class="ss-reason"><span class="pill pill-${vCls(it.verdict)}">机器：${esc(it.verdict)}</span>${it.gate_by === "user" ? '<span class="pill pill-info">人工</span>' : ""}
          ${it.reason ? `<b>${esc(it.reason)}</b>` : ""}${it.note ? ` · ${esc(it.note)}` : ""}${it.acrylic_fit ? ` · 亚克力：${esc(it.acrylic_fit)}` : ""}</div></div></div>`;
  }
  function render() {
    const body = $("ssBody"); if (!body) return;
    const done = (i) => i.item_state === "classified" || i.item_state === "error";
    const n = (g) => V.items.filter((i) => done(i) && i.gate === g).length, nRun = V.items.filter((i) => !done(i)).length, r = V.run;
    $("ssSum").innerHTML = r ? `共 <b>${V.items.length}</b> 个 listing · 待确认 <b>${n("pending")}</b> · 已通过 ${n("passed")} · 已排除 ${n("rejected")}${nRun ? ` · 分类中 ${nRun}` : ""}` : "";
    const ST = { queued: "排队中（等待服务器浏览器）", running: "读取 EHunt 列表页中", succeeded: "已完成", failed: "失败", canceled: "已停止" };
    const pct = r ? Math.round(100 * (r.scraped_count || 0) / (r.target_count || 100)) : 0;
    $("ssStart").innerHTML = `<div class="card ss-start"><div class="ss-form"><input id="ssKw" class="input" placeholder="EHunt 关键词，例如 acrylic decoration" value="${esc($("ssKw")?.value || "")}">
        <label class="hint">最多 <input id="ssLimit" class="input input-sm ss-thr" type="number" min="1" max="300" value="${esc($("ssLimit")?.value || 100)}"> 条</label>
        <button class="btn btn-primary" id="ssGo"${active(r) ? " disabled" : ""}>开始抓取并预筛 →</button></div>
      <p class="hint">价格 $8–40 · 总销量 ≥100 · 按 7 天销量降序（需求文档 §2）。服务器用已登录的浏览器逐条读 EHunt 列表页（不用 EHunt API），每读到一条立即分类并出现在下方。</p>
      ${r && r.status ? `<div class="ss-prog-row"><span class="pill ${active(r) ? "pill-info" : r.status === "succeeded" ? "pill-ok" : "pill-bad"}">${active(r) ? '<span class="spin"></span> ' : ""}${esc(ST[r.status] || r.status)}</span>
        <span class="sc-prog ss-prog"><span style="width:${pct}%"></span></span><span class="hint">已读取 <b>${esc(r.scraped_count || 0)}</b> / ${esc(r.target_count || 100)} 行 · 已分类 <b>${esc(r.classified_count || 0)}</b>${r.error ? ` · <span class="warn">${esc(r.error)}</span>` : ""}</span>
        ${active(r) ? '<button class="btn btn-ghost btn-sm" id="ssStop">■ 停止</button>' : ""}</div>` : ""}</div>`;
    $("ssTabs").innerHTML = (V.runs.length > 1 ? `<select id="ssRun" class="input input-sm">${V.runs.map((r) => `<option value="${esc(r.id)}"${r.id === V.run?.id ? " selected" : ""}>${esc(r.keyword)}</option>`).join("")}</select>` : "")
      + [["all", "全部", V.items.length], ["pending", "待确认", n("pending")], ["passed", "已通过", n("passed")], ["rejected", "已排除", n("rejected")]]
        .map(([k, t, c]) => `<button class="fchip${V.tab === k ? " active" : ""}" data-sstab="${k}">${t}<span class="fchip-n">${c}</span></button>`).join("");
    const show = V.items.filter((i) => V.tab === "all" || (done(i) && i.gate === V.tab))
      .sort((a, b) => V.tab === "all" ? (b.ranks?.[0] ?? 0) - (a.ranks?.[0] ?? 0) : (a.ranks?.[0] ?? 0) - (b.ranks?.[0] ?? 0)); // 全部：最新读到的在前
    body.innerHTML = !V.run ? '<div class="card empty"><div class="ico">◎</div><h3>还没有选品任务</h3><p class="muted">在上方输入关键词，点“开始抓取并预筛”</p></div>'
      : `<section class="sc-batch"><div class="sc-bh"><h3>${esc(V.run.keyword)} <span class="hint">${esc(GATE_NAME[V.tab] || "全部")} ${show.length}</span></h3><span class="spacer"></span>
          ${V.tab === "pending" && n("pending") ? `<button class="btn btn-ghost btn-sm" data-ssbulk="passed">全部待确认 → 通过 (${n("pending")})</button>` : ""}</div>
        ${show.length ? `<div class="sc-grid">${show.map(card).join("")}</div>` : '<p class="hint sc-none">此分类下没有条目</p>'}</section>`;
    $("ssFoot").innerHTML = V.run ? `<div class="sc-foot"><div><b>进入选品筛选</b><p class="hint">只有“已通过”的 ${n("passed")} 条进入选品筛选（短名单、勾选条件、加入备选）；${n("pending")} 条待确认需先点 ✓ 确认，已排除的不进入。</p></div>
      <a class="btn btn-primary" href="#/select/${esc(V.run.id)}"${n("passed") ? "" : ' aria-disabled="true"'}>进入选品筛选（${n("passed")} 条）→</a></div>` : "";
  }
  async function setGate(ids, gate) {
    const items = ids.map((id) => V.items.find((x) => x.id === id)).filter(Boolean); if (!items.length) return;
    if (!DEMO) { const { error } = await getSb().rpc("sel_set_gate", { p_items: ids, p_gate: gate }); if (error) return toast(`操作失败：${error.message}`, "bad"); }
    const prev = items.map((it) => [it, it.gate, it.gate_by]);
    items.forEach((it) => { it.gate = gate; it.gate_by = "user"; });
    toast(`${items.length} 条 → ${GATE_NAME[gate]}`, "ok", [{ label: "撤销", run: () => undo(prev) }], 4000); render();
  }
  async function undo(prev) {
    for (const [it, g, by] of prev) { if (!DEMO) await getSb().rpc("sel_set_gate", { p_items: [it.id], p_gate: g }); it.gate = g; it.gate_by = by; }
    render();
  }
  document.addEventListener("click", (e) => {
    if (e.target.id === "ssGo") return start();
    if (e.target.id === "ssStop") return cancel();
    const t = e.target.closest("[data-sstab],[data-ssgate],[data-ssbulk]"); if (!t) return;
    if (t.dataset.sstab) { V.tab = t.dataset.sstab; render(); }
    else if (t.dataset.ssgate) { e.preventDefault(); setGate([t.dataset.ssid], t.dataset.ssgate); }
    else if (t.dataset.ssbulk) setGate(V.items.filter((i) => i.gate === "pending").map((i) => i.id), t.dataset.ssbulk);
  });
  document.addEventListener("change", (e) => { if (e.target.id === "ssRun") location.hash = `#/selscreen/${e.target.value}`; });
  return { show: (runId) => load(runId), _state: V };
}
