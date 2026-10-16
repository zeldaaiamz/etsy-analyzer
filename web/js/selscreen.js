// 链接筛选（#/selscreen[/<run>]）：输入关键词点“开始” → sel_start 建任务 → box 上的 selection worker 用自己已登录的浏览器逐条读 EHunt 列表页
// → 每条立即分类（convertible + 可选定制保留，视觉×3）→ 逐条出现在这里（3 秒轮询，带进度条）→ 人工复核，
// 只有“已通过”（机器保留 + 人工确认的待确认）才进入 #/select。样式/交互沿用 #/screen 的 sc-* 卡片与 ✕/撤销按钮，不改动 #/screen。
const GATE_NAME = { pending: "待确认", passed: "保留", rejected: "已排除" };
// 排除原因归类（规则 6：每一步排除都记原因和数量）
const RGROUP = (it) => it.gate_by === "user" ? "人工排除" : it.reason === "7天销量≤3" ? "7天销量≤3" : /定制/.test(it.reason || "") ? "定制" : it.reason === "IP" ? "IP" : /材质/.test(it.reason || "") ? "材质" : /数字|素材|空白/.test(it.reason || "") ? "素材/数字文件" : "其他";
export function createSelScreen({ $, esc, imgUrl, toast, getSb, DEMO }) {
  const V = { runs: [], run: null, items: [], tab: "pending", timer: null, fx: null, auditOpen: false };
  const active = (r) => r && ["count_queued", "counting", "queued", "running"].includes(r.status);
  // 演示用的 EHunt 结果数（标准筛选：$8–40、总销量 ≥100、按 7 天销量）
  // 演示数字（非实测，界面上会标“演示数字”）；实测值放 MEASURED（含 7 天销量 >3 筛选后的数）后会替换并标“EHunt 实测”
  const DEMO_COUNTS = { "acrylic decoration": 20, "sign": 1688 }; // 10-16 实测（全部筛选含 7 天销量 Min 4）；其他词为演示数字
  const MEASURED = { "acrylic decoration": "10-16 Find 20 products", "sign": "10-16 共 1,688 条 / 85 页" };
  const demoTotal = (kw) => DEMO_COUNTS[kw.toLowerCase()] ?? (40 + [...kw].reduce((a, c) => a + c.charCodeAt(0) * 7, 0) % 900);
  const advice = (n) => n <= 100 ? ["ok", `≤100 条：全部读取`] : n > 1000 ? ["bad", `超过 1000 条：建议先跳过，等人工决定（需求文档 §2，例如 sign、keychain）`] : ["info", `超过 100 条：默认只读前 100 条（按 7 天销量），可调整`];
  // 演示：模拟 worker 逐条抓取 + 分类（scraped → classified），不连 Supabase
  function demoCount(kw) {
    const fx = V.fx, id = `demo-${Date.now()}`;
    const run = { id, keyword: kw, status: "counting", total_count: null, target_count: null, scraped_count: 0, classified_count: 0, created_at: new Date().toISOString(), stats: { raw_rows: 0 } };
    fx.runs.unshift(run); fx.items[id] = []; location.hash = `#/selscreen/${id}`;
    setTimeout(() => { run.status = "counted"; run.total_count = demoTotal(kw); rerender(); }, Number(QSP.get("countms")) || 1200);
  }
  async function demoStart(run, limit) {
    const fx = V.fx, src = fx.items["demo-run"].map((x) => ({ ...x })), id = run.id;
    Object.assign(run, { status: "running", target_count: Math.min(limit, QSP.has("guarddemo") ? src.length : src.filter((x) => x.sales_7d >= FILTERS.min_sales_7d).length) }); render();
    // EHunt 页面已按 7 天销量 >3 筛选，只会返回这些行；?guarddemo=1 时播放全部回放行，演示兜底排除
    const raw = JSON.parse(JSON.stringify(src)).filter((x) => QSP.has("guarddemo") || x.sales_7d >= FILTERS.min_sales_7d).sort((a, b) => a.ranks[0] - b.ranks[0]); let i = 0;
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
  // 标准筛选（需求文档 §2），数量查询和抓取都带上；box worker 在 EHunt 界面上逐项设置并校验 Selected: 标签
  const FILTERS = { price_min: 8, price_max: 40, min_sales: 100, min_sales_7d: 4, sort: "↓7-day sales" };
  async function count() {
    const kw = $("ssKw").value.trim(); if (!kw) return toast("请输入关键词", "bad");
    if (DEMO) return demoCount(kw);
    const { data, error } = await getSb().rpc("sel_count", { p_keyword: kw, p_params: FILTERS });
    if (error) return toast(`查询失败：${error.message}`, "bad");
    location.hash = `#/selscreen/${data}`;
  }
  async function start() {
    const r = V.run, limit = Math.max(1, Math.min(Number($("ssLimit").value) || 100, r.total_count || 100));
    if (DEMO) return demoStart(r, limit);
    const { error } = await getSb().rpc("sel_start", { p_run: r.id, p_limit: limit });
    if (error) return toast(`开始失败：${error.message}`, "bad");
    toast(`开始读取前 ${limit} 条`, "ok"); load(r.id);
  }
  async function skip() {
    if (DEMO) { V.run.status = "skipped"; return render(); }
    const { error } = await getSb().rpc("sel_skip", { p_run: V.run.id }); if (error) return toast(error.message, "bad"); load(V.run.id);
  }
  async function cancel() {
    if (DEMO) { V.run.status = "canceled"; return render(); }
    const { error } = await getSb().rpc("sel_cancel", { p_run: V.run.id }); if (error) return toast(error.message, "bad"); load(V.run.id);
  }
  function schedule() { clearTimeout(V.timer); if (!DEMO && active(V.run) && location.hash.startsWith("#/selscreen")) V.timer = setTimeout(() => load(V.run.id), 3000); }
  async function load(runId) {
    if (DEMO) { V.fx ||= (await import("./demo-selection.js")).selectionFixtures(); const fx = V.fx; V.runs = fx.runs; V.run = V.runs.find((r) => r.id === runId) || V.runs[0]; V.items = fx.items[V.run.id] || [];
      if (QSP.has("stream") && !V.autoStarted) { V.autoStarted = true; render(); $("ssKw").value = QSP.get("stream") || "acrylic decoration"; return demoCount($("ssKw").value); }
      return render(); }
    const sb = getSb(); if (!sb) return;
    const rs = await sb.from("sel_runs").select("id,keyword,created_at,stats,status,total_count,target_count,scraped_count,classified_count,error,heartbeat_at").order("created_at", { ascending: false }).limit(50);
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
      : `${g === "pending" ? `<button class="sc-x ss-pass" data-ssgate="passed" data-ssid="${esc(it.id)}" title="保留，进入选品筛选" aria-label="保留">✓</button>` : ""}<button class="sc-x" data-ssgate="rejected" data-ssid="${esc(it.id)}" title="排除" aria-label="排除">✕</button>`;
    const tag = rj ? '<span class="sc-tag">已排除</span>' : ok ? '<span class="sc-tag sc-kept">保留</span>' : '<span class="sc-tag ss-tag-q">待确认</span>';
    const price = it.price_min == null ? "" : Number(it.price_min) === Number(it.price_max) ? `$${Number(it.price_min).toFixed(2)}` : `$${Number(it.price_min).toFixed(2)}–${Number(it.price_max).toFixed(2)}`;
    return `<div class="sc-card${rj ? " rej" : ""}" data-skey="${esc(it.id)}">
      <a class="sc-img" href="https://www.etsy.com/listing/${esc(it.etsy_listing_id)}" target="_blank" rel="noopener"><img class="on" loading="lazy" src="${imgUrl(it.image_url)}" alt="">${tag}</a>${act}
      <div class="sc-info"><div class="sc-title" title="${esc(it.title)}">${esc(it.title)}</div>
        <div class="sc-meta"><b>${esc(price)}</b><span class="hint">#${esc(it.etsy_listing_id)}</span></div>
        <div class="ss-reason"><span class="pill pill-${vCls(it.verdict)}">机器：${esc(it.verdict)}</span>${it.gate_by === "user" ? '<span class="pill pill-info">人工</span>' : ""}
          ${it.reason ? `<b>${esc(it.reason)}</b>` : ""}${it.note ? ` · ${esc(it.note)}` : ""}${it.acrylic_fit ? ` · 亚克力：${esc(it.acrylic_fit)}` : ""}</div></div></div>`;
  }
  function countPanel(r) {
    const locked = `<div class="ss-form"><label class="hint">读取前 <input class="input input-sm ss-thr" type="number" disabled placeholder="—"> 条</label><button class="btn btn-primary" disabled title="先查询数量">抓取 →</button><span class="hint">先查询数量，显示结果数后才能开始</span></div>`;
    if (!r || ["succeeded", "failed", "canceled"].includes(r.status) && r.total_count == null) return `<div class="ss-count">${locked}</div>`;
    if (r.status === "count_queued" || r.status === "counting") return `<div class="ss-count"><span><span class="spin"></span> 正在读取 EHunt「${esc(r.keyword)}」的结果数（$8–40 · 总销量 ≥100 · 7 天销量 >3 · 按 7 天销量降序）…</span>${locked}</div>`;
    if (r.total_count == null) return `<div class="ss-count">${locked}</div>`;
    const n = r.total_count, [k, t] = advice(n), def = Math.min(n, 100);
    const head = `<div class="ss-count-n"><b>${esc(r.keyword)}</b>：<span class="ss-n ss-n-${k}">${n.toLocaleString()}</span> 条 <span class="pill pill-${k === "ok" ? "ok" : k === "bad" ? "bad" : "info"}">${esc(t)}</span>${DEMO ? (MEASURED[r.keyword.toLowerCase()] ? `<span class="pill pill-ok">EHunt 实测（${esc(MEASURED[r.keyword.toLowerCase()])}）</span>` : '<span class="pill pill-bad ss-fake">演示数字，非实测</span>') : ""}</div>
      <p class="hint">筛选：$${FILTERS.price_min}–${FILTERS.price_max} · 总销量 ≥${FILTERS.min_sales} · 7 天销量 >${FILTERS.min_sales_7d - 1} · 按 7 天销量降序</p>`;
    if (r.status === "skipped") return `<div class="ss-count">${head}<p class="hint">已跳过，等人工决定。可以换个更细的子词重新查询。</p></div>`;
    if (r.status !== "counted") return `<div class="ss-count ss-count-done">${head}<p class="hint">本次读取前 ${esc(r.target_count ?? def)} 条（每页 20 条）</p></div>`;
    return `<div class="ss-count${k === "bad" ? " ss-count-warn" : ""}">${head}
      <div class="ss-form"><label>最多读取 <input id="ssLimit" class="input input-sm ss-thr" type="number" min="1" max="${Math.min(n, 1000)}" value="${def}"> 条</label>
        ${k === "bad" ? `<button class="btn btn-primary" id="ssSkip">跳过，等人工决定</button><button class="btn btn-ghost" id="ssGo">仍然读取</button>`
          : `<button class="btn btn-primary" id="ssGo">抓取 →</button><button class="btn btn-ghost" id="ssSkip">跳过</button>`}</div></div>`;
  }
  function render() {
    const body = $("ssBody"); if (!body) return;
    const done = (i) => i.item_state === "classified" || i.item_state === "error";
    const ex = V.items.filter((i) => done(i) && i.gate === "rejected"), inList = V.items.filter((i) => !(done(i) && i.gate === "rejected"));
    const n = (g) => inList.filter((i) => done(i) && i.gate === g).length, nRun = inList.filter((i) => !done(i)).length, r = V.run;
    $("ssSum").innerHTML = "";
    const ST = { count_queued: "排队查询数量", counting: "读取 EHunt 结果数中", counted: "已查询数量", skipped: "已跳过（等人工决定）", queued: "排队中（等待 box 抓取进程）", running: "读取 EHunt 列表页中", succeeded: "已完成", failed: "失败", canceled: "已停止" };
    const pct = r ? Math.round(100 * (r.scraped_count || 0) / (r.target_count || 100)) : 0;
    $("ssStart").innerHTML = `<div class="card ss-start"><div class="ss-form"><input id="ssKw" class="input" placeholder="EHunt 关键词，例如 acrylic decoration" value="${esc($("ssKw")?.value || "")}">
        <button class="btn btn-primary" id="ssCount"${active(r) || r?.status === "counted" ? " disabled" : ""}>查询数量</button></div>
      <p class="hint">价格 $8–40 · 总销量 ≥100 · 7 天销量 >3 · 按 7 天销量降序（需求文档 §2 + Hongyan 10-16）。box 上的抓取进程用自己已登录的浏览器逐条读 EHunt 列表页（不用 EHunt API），先查结果数，再决定读多少条；每条经规则 + 视觉判断，全部完成后输出符合要求的链接。</p>
      ${countPanel(r)}
      ${r && r.status && !["count_queued", "counting", "counted", "skipped"].includes(r.status) ? `<div class="ss-prog-row"><span class="pill ${active(r) ? "pill-info" : r.status === "succeeded" ? "pill-ok" : "pill-bad"}">${active(r) ? '<span class="spin"></span> ' : ""}${esc(ST[r.status] || r.status)}</span>
        <span class="sc-prog ss-prog"><span style="width:${pct}%"></span></span><span class="hint">已读取 <b>${esc(r.scraped_count || 0)}</b> / ${esc(r.target_count || 100)} 行 · 已分类 <b>${esc(r.classified_count || 0)}</b>${r.error ? ` · <span class="warn">${esc(r.error)}</span>` : ""}</span>
        ${active(r) ? '<button class="btn btn-ghost btn-sm" id="ssStop">■ 停止</button>' : ""}</div>` : ""}</div>`;
    // 输出（Hongyan 10-16）：全部处理完才显示“符合要求 N 条” + 纯 Etsy 链接清单（含待确认，拿不准就保留）；抓取中只显示进度条和停止
    const grp = {}; ex.forEach((i) => { const g = RGROUP(i); grp[g] = (grp[g] || 0) + 1; });
    const order = ["定制", "IP", "材质", "素材/数字文件", "7天销量≤3", "人工排除", "其他"].filter((g) => grp[g]);
    const finished = r && ["succeeded", "canceled", "failed"].includes(r.status) && !nRun;
    const ok = inList.filter(done).sort((a, b) => (a.ranks?.[0] ?? 0) - (b.ranks?.[0] ?? 0));
    V.links = ok.map((i) => `https://www.etsy.com/listing/${i.etsy_listing_id}`);
    $("ssTabs").innerHTML = V.runs.length > 1 ? `<select id="ssRun" class="input input-sm">${V.runs.map((x) => `<option value="${esc(x.id)}"${x.id === V.run?.id ? " selected" : ""}>${esc(x.keyword)}</option>`).join("")}</select>` : "";
    const audit = ex.length ? `<details class="ss-audit ss-audit-mini"><summary>排除记录（${ex.length}：${order.map((g) => `${esc(g)} ${grp[g]}`).join(" / ")}）</summary>
      <table class="ss-audit-t"><thead><tr><th>rank</th><th>listing</th><th>标题</th><th>类别</th><th>原因 / 依据</th><th>来源</th></tr></thead><tbody>
      ${ex.sort((a, b) => (a.ranks?.[0] ?? 0) - (b.ranks?.[0] ?? 0)).map((i) => `<tr><td>${esc((i.ranks || []).join("/"))}</td><td>${esc(i.etsy_listing_id)}</td><td class="ss-at">${esc(i.title)}</td><td>${esc(RGROUP(i))}</td>
        <td>${esc(i.reason || "")}${i.explain ? `<span class="hint"> · ${esc(i.explain)}</span>` : ""}</td><td>${i.gate_by === "user" ? "人工" : esc(i.source || "")}</td></tr>`).join("")}</tbody></table></details>` : "";
    body.innerHTML = !V.run ? '<div class="card empty"><div class="ico">◎</div><h3>还没有任务</h3><p class="muted">在上方输入关键词，先“查询数量”，再填写最多读取条数点“抓取”</p></div>'
      : !finished ? (["count_queued", "counting", "counted", "skipped"].includes(r.status) ? "" : '<p class="hint ss-wait">全部处理完成后在这里显示符合要求的 Etsy 链接。</p>')
      : `<section class="card ss-result"><div class="ss-res-head"><h3>符合要求 <span class="ss-n ss-n-ok">${ok.length}</span> 条</h3><span class="hint">保留 ${n("passed")} + 待确认 ${n("pending")}（拿不准就保留）${r.status !== "succeeded" ? ` · 任务${r.status === "canceled" ? "已停止" : "失败"}，以下为已处理部分` : ""}</span><span class="spacer"></span>
          <button class="btn btn-primary btn-sm" id="ssCopy"${ok.length ? "" : " disabled"}>复制全部</button><button class="btn btn-ghost btn-sm" data-ssdl="txt"${ok.length ? "" : " disabled"}>下载 TXT</button><button class="btn btn-ghost btn-sm" data-ssdl="csv"${ok.length ? "" : " disabled"}>下载 CSV</button></div>
        <pre class="ss-links" id="ssLinks">${esc(V.links.join("\n"))}</pre>${audit}</section>`;
    $("ssFoot").innerHTML = "";
  }
  function download(kind) {
    const kw = (V.run?.keyword || "links").replace(/[^\w-]+/g, "_"), txt = kind === "csv" ? "url\n" + V.links.join("\n") + "\n" : V.links.join("\n") + "\n";
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([kind === "csv" ? "\ufeff" + txt : txt], { type: kind === "csv" ? "text/csv" : "text/plain" }));
    a.download = `etsy_${kw}_${V.links.length}.${kind}`; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
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
    if (e.target.id === "ssCount") return count();
    if (e.target.id === "ssCopy") return navigator.clipboard.writeText(V.links.join("\n")).then(() => toast(`已复制 ${V.links.length} 条链接`, "ok"), () => toast("复制失败，请手动选择", "bad"));
    if (e.target.dataset?.ssdl) return download(e.target.dataset.ssdl);
    if (e.target.id === "ssSkip") return skip();
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
