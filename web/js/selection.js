// 选品筛选视图（#/select[/<run>]）：EHunt 列表页抓取 → classify.py（规则 + 视觉×3，非一致→待确认）→ 导入 024 sel_* 表 → 这里只读展示
// 短名单 = 结论“保留” 且 每个条件都打勾；价格阈值可调（默认 $15，本地记忆）；一键加入产品库“备选”。
const THR_KEY = "selPriceThr";
const TABS = [["short", "短名单"], ["保留", "保留"], ["待确认", "待确认"], ["排除", "排除"], ["all", "全部"]];
export function createSelection({ $, esc, imgUrl, toast, getSb, DEMO }) {
  const V = { runs: [], run: null, items: [], tab: "short", thr: Number(localStorage.getItem(THR_KEY)) || 15, sortBy: "rank", demo: null, busy: false };
  const fmt$ = (n) => `$${Number(n).toFixed(2)}`;
  const price = (it) => it.price_min == null ? "" : Number(it.price_min) === Number(it.price_max) ? fmt$(it.price_min) : `${fmt$(it.price_min)}–${Number(it.price_max).toFixed(2)}`;
  // 每个条件一个勾：ok=true ✓ / false ✗ / null ?（待确认）
  function conds(it) {
    const r = it.reason || "", fit = it.acrylic_fit || "";
    return [
      ["非定制", /定制/.test(r) ? (it.verdict === "排除" ? false : null) : it.custom_type === "待确认" ? null : true, it.custom_type === "可选定制" ? "可选定制（不加名字也能卖）" : it.custom_type || ""],
      ["非 IP", r === "IP" ? false : true, ""],
      ["材质/平面亚克力", fit === "是" ? true : fit === "可改造" ? null : fit === "不适合" ? false : it.source === "规则" ? null : null, fit ? `acrylic_fit：${fit}` : "规则阶段已排除，未做视觉"],
      ["非素材/数字文件", /数字文件/.test(r) ? false : true, ""],
      [`价格 ≥ $${V.thr}`, Number(it.price_max) >= V.thr, price(it)],
      ["去重（每个 listing 一行）", true, it.dup_note || "无重复"],
    ];
  }
  const shortlisted = (it) => it.verdict === "保留" && conds(it).every((c) => c[1] === true);
  const tick = (v) => v === true ? '<span class="ss-ok">✓</span>' : v === false ? '<span class="ss-no">✗</span>' : '<span class="ss-q">?</span>';
  const fitPill = (f) => f ? `<span class="pill ss-fit ss-fit-${f === "是" ? "y" : f === "可改造" ? "c" : "n"}">平面亚克力：${esc(f)}</span>` : "";
  const vPill = (v) => `<span class="pill ss-v ss-v-${v === "保留" ? "k" : v === "待确认" ? "q" : "x"}">${esc(v)}</span>`;

  async function load(runId) {
    if (DEMO) {
      if (!V.demo) V.demo = (await import("./demo-selection.js")).selectionFixtures();
      V.runs = V.demo.runs; V.run = V.runs.find((r) => r.id === runId) || V.runs[0]; V.items = V.demo.items[V.run.id] || []; return render();
    }
    const sb = getSb(); if (!sb) return;
    const rs = await sb.from("sel_runs").select("id,keyword,created_at,params,stats").order("created_at", { ascending: false }).limit(50);
    if (rs.error) { $("selBody").innerHTML = `<div class="empty">选品筛选数据表未就绪（需要迁移 024）：${esc(rs.error.message)}</div>`; return; }
    V.runs = rs.data || []; V.run = V.runs.find((r) => r.id === runId) || V.runs[0] || null;
    if (!V.run) { V.items = []; return render(); }
    const { data, error } = await sb.rpc("sel_list", { p_run: V.run.id });
    if (error) return toast(`加载失败：${error.message}`, "bad");
    V.items = data || []; render();
  }
  function card(it) {
    const cs = conds(it), sl = shortlisted(it);
    const ratio = it.sales_ratio != null ? `${(it.sales_ratio * 100).toFixed(1)}%` : "—";
    return `<div class="ss-card${sl ? " ss-short" : ""}">
      <a class="ss-img" href="https://www.etsy.com/listing/${esc(it.etsy_listing_id)}" target="_blank" rel="noopener"><img loading="lazy" src="${imgUrl(it.image_url)}" alt=""></a>
      <div class="ss-main">
        <div class="ss-top">${vPill(it.verdict)}${sl ? '<span class="pill ss-sl">★ 短名单</span>' : ""}${fitPill(it.acrylic_fit)}${it.note ? `<span class="pill pill-warn">${esc(it.note)}</span>` : ""}
          <span class="spacer"></span><span class="hint">#${esc(it.etsy_listing_id)} · rank ${esc((it.ranks || []).join("/"))}</span></div>
        <div class="ss-title" title="${esc(it.title)}">${esc(it.title)}</div>
        <div class="ss-nums"><b>${esc(price(it))}</b><span>总销量 <b>${esc(it.total_sales)}</b></span><span>7 天 <b>${esc(it.sales_7d)}</b></span>
          <span title="7 天销量 / 总销量">7天/总 <b>${ratio}</b></span><span title="${esc((it.cross_kw_list || []).join("、"))}">跨关键词 <b>${esc(it.cross_kw || 1)}</b></span></div>
        <ul class="ss-conds">${cs.map(([n, v, h]) => `<li title="${esc(h)}">${tick(v)} ${esc(n)}${h && v !== true ? `<span class="hint"> ${esc(h)}</span>` : ""}</li>`).join("")}</ul>
        ${it.selling_points?.length ? `<div class="ss-tags"><span class="hint">卖点</span>${it.selling_points.map((s) => `<span class="tag">${esc(s)}</span>`).join("")}</div>` : ""}
        ${it.occasions?.length ? `<div class="ss-tags"><span class="hint">场合</span>${it.occasions.map((s) => `<span class="tag tag-o">${esc(s)}</span>`).join("")}</div>` : ""}
        <div class="ss-why">${it.verdict !== "保留" ? `<b>${esc(it.reason || "")}</b> · ` : ""}${esc(it.explain || "")}${it.votes ? `<span class="hint"> · 三次：${esc(it.votes)}</span>` : ""}</div>
      </div>
      <div class="ss-act">${it.in_library ? '<span class="pill">已在产品库</span>' : `<button class="btn btn-sm ${sl ? "btn-primary" : "btn-ghost"}" data-seladd="${esc(it.id)}">＋ 加入备选</button>`}</div>
    </div>`;
  }
  function render() {
    const body = $("selBody"); if (!body) return;
    const cnt = { short: V.items.filter(shortlisted).length, all: V.items.length };
    ["保留", "待确认", "排除"].forEach((k) => cnt[k] = V.items.filter((i) => i.verdict === k).length);
    const st = V.run?.stats || {};
    $("selHead").innerHTML = V.run ? `<select id="selRun" class="input input-sm">${V.runs.map((r) => `<option value="${esc(r.id)}"${r.id === V.run.id ? " selected" : ""}>${esc(r.keyword)} · ${esc((r.created_at || "").slice(0, 10))}</option>`).join("")}</select>
      <span class="hint">原始 ${esc(st.raw_rows ?? "?")} 行 → 去重 ${esc(st.items ?? V.items.length)} 个 listing · 视觉 ${esc(st.vision_calls ?? 0)} 次 · ¥${esc(st.cost_yuan ?? 0)} · 三次不一致 ${esc(st.disagree_runs ?? 0)}</span>
      <span class="spacer"></span><label class="hint">价格阈值 $<input id="selThr" class="input input-sm ss-thr" type="number" min="0" step="1" value="${V.thr}"></label>
      <button class="btn btn-sm btn-primary" id="selAddAll"${cnt.short ? "" : " disabled"}>短名单全部加入备选 (${cnt.short})</button>` : "";
    $("selTabs").innerHTML = TABS.map(([k, n]) => `<button class="fchip${V.tab === k ? " active" : ""}" data-seltab="${k}">${n}<span class="fchip-n">${cnt[k]}</span></button>`).join("");
    const list = V.items.filter((it) => V.tab === "all" ? true : V.tab === "short" ? shortlisted(it) : it.verdict === V.tab);
    body.innerHTML = !V.run ? '<div class="empty">还没有导入的筛选结果。用 tools/etsy_selection/classify.py 处理 EHunt 列表 JSON，再用 import_run.py 导入。</div>'
      : list.length ? list.map(card).join("") : '<div class="empty">此分类下没有条目</div>';
  }
  async function add(ids) {
    ids = ids.filter((id) => { const it = V.items.find((x) => x.id === id); return it && !it.in_library; });
    if (!ids.length) return;
    if (!DEMO) { const { data, error } = await getSb().rpc("sel_add_to_library", { p_items: ids }); if (error) return toast(`加入失败：${error.message}`, "bad"); toast(`已加入产品库“备选” ${data} 个`, "ok"); }
    else toast(`（演示）已加入产品库“备选” ${ids.length} 个`, "ok");
    ids.forEach((id) => { const it = V.items.find((x) => x.id === id); if (it) it.in_library = true; }); render();
  }
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-seltab],[data-seladd],#selAddAll"); if (!t) return;
    if (t.dataset.seltab) { V.tab = t.dataset.seltab; render(); }
    else if (t.dataset.seladd) add([t.dataset.seladd]);
    else if (t.id === "selAddAll") add(V.items.filter(shortlisted).map((i) => i.id));
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "selThr") { V.thr = Math.max(0, Number(e.target.value) || 0); localStorage.setItem(THR_KEY, V.thr); render(); }
    if (e.target.id === "selRun") location.hash = `#/select/${e.target.value}`;
  });
  return { show: (runId) => load(runId), hide: () => {}, _state: V };
}
