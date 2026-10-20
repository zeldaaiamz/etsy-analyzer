// 预筛选视图（#/screen[/<batch>]）：粘贴 → worker 只取 EHunt 数据 → 每 50 条一批看大图，✕ 排除 / 撤销
// → 每批“分析剩余数据”或底部“全部已加载”一键确认 → 只有保留的进入 DeepSeek / Keepamore / 截图（017 screen_confirm）
export const BATCH = 50;
const POLL_MS = 3000;
import { screenBatchGroups, batchIndex, itemSrc, batchCounts, batchCountRows } from "./batchname.js";
// EHunt 账号/服务异常（与商品本身无关）：新 worker 写 'EHunt 服务异常（原因）'；旧 worker 写 'EHunt 错误 500: 查询失败: …'
export const EHUNT_ANOMALY_RE = /EHunt 服务异常|EHunt 错误 5\d\d|查询失败|daily_limit/;
export const ANOMALY_PREFIX = "EHunt 服务异常";
export function anomalyReason(err) {
  const s = String(err || ""), m = s.match(/EHunt 服务异常（(.*?)）(?:，|$)/) || s.match(/EHunt 错误 \d{3}:\s*(.*)$/);
  return (m ? m[1] : s).trim().slice(0, 120);
}

export function createScreen({ $, esc, imgUrl, toast, getSb, DEMO, QS, confirmDialog, getStatus = () => null }) {
  const V = { loadErr: null, items: [], batch: null, tab: "all", rej: new Set(), imgOk: new Set(), busy: new Set(), loaded: false, timer: null, demo: null, sbatches: null, sel: new Set() };
  const thumb = (u) => (/^https:\/\/i\.etsystatic\.com\//.test(u || "") ? u.replace(/\/il_[0-9a-zA-Z]+x[0-9a-zA-Z]+\./, "/il_570xN.") : u);
  const hasData = (it) => it.status === "screening" || it.screen_state === "rejected" || it.screen_state === "kept";
  const fetchingIt = (it) => (it.status === "queued" || it.status === "running") && it.screen_state !== "kept";
  const anomaly = (it) => (it.status === "failed" || it.status === "queued") && it.screen_state !== "kept" && EHUNT_ANOMALY_RE.test(it.error || "");
  const svcPaused = () => { const st = getStatus() || {}; return String(st.reason || "").startsWith(ANOMALY_PREFIX) && Date.parse(st.ehunt_paused_until || "") > Date.now(); };
  // 受 EHunt 服务异常影响：已写上异常原因的，加上熔断期间所有仍在取数（排队/运行）的条目（如一键刷新后 error 已清空的）
  const blocked = (it) => anomaly(it) || (fetchingIt(it) && !it.title && svcPaused());
  const waiting = (it) => fetchingIt(it) && it.status === "queued" && /额度/.test(it.error || "") && !anomaly(it);
  const ready = (it) => !hasData(it) || !it.image_url || V.imgOk.has(it.id); // 无图或失败也算加载完成
  const batches = () => { const out = []; for (let i = 0; i < V.items.length; i += BATCH) out.push(V.items.slice(i, i + BATCH)); return out; };
  const pendingOf = (b) => b.filter((it) => it.status === "screening");
  const batchState = (b) => {
    const pend = pendingOf(b), fetching = b.filter(fetchingIt);
    if (!pend.length && !fetching.length) return "done";
    if (fetching.length || pend.some((it) => !ready(it))) return "loading";
    return "ready";
  };

  async function load() {
    if (DEMO) return;
    const sb = getSb(); if (!sb) return;
    // 批次列表与 screen_list 并行请求（只在首次 / 切换时取，轮询不重复取），不增加等待时间
    const bq = V.sbatches ? null : sb.from("batches").select("id,name,created_at").like("name", "获取链接 %").order("created_at", { ascending: true }).limit(1000);
    const [{ data, error }, br] = await Promise.all([sb.rpc("screen_list", V.batch ? { p_batch: V.batch } : {}), bq]);
    if (br && !br.error) V.sbatches = br.data || [];
    if (error) { V.loadErr = error.message; toast(`加载筛选列表失败：${error.message}`, "bad"); render(); return; }
    V.items = data || []; V.loaded = true; V.loadErr = null;
    render(); schedule();
  }
  function schedule() {
    clearTimeout(V.timer);
    if (!DEMO && visible() && V.items.some(fetchingIt)) V.timer = setTimeout(load, POLL_MS);
  }
  const visible = () => location.hash.startsWith("#/screen");

  function card(it, bstate) {
    const rj = V.rej.has(it.id) || it.screen_state === "rejected", done = it.screen_state === "rejected";
    const failed = it.status === "failed" && it.screen_state !== "kept";
    const left = (it.max_attempts ?? 3) - (it.attempts ?? 0);
    // EHunt 网页回退（ETSY_PROVIDER=ehunt_web）只取主图：没有标题/价格 → 显示 listing id，不当作“读取中”
    const head = it.title ? esc(it.title) : hasData(it) ? `Etsy #${esc(it.listing_id || "")} <span class="hint">· 仅主图</span>` : blocked(it) ? `<span class="sc-anom" title="${esc(it.error || "")}">EHunt 服务异常（${esc(anomalyReason(anomaly(it) ? it.error : getStatus()?.reason))}）<span class="hint">${it.status === "failed" ? " · 可一键刷新" : " · 已暂停，恢复后自动重试"}</span></span>` : waiting(it) ? "等待 EHunt 额度（明日自动继续）" : it.status === "failed" ? (left > 0 ? "取数失败" : `取数失败（已重试 ${it.attempts ?? 3} 次，已停止）`) : "读取 Etsy 数据中…";
    const img = hasData(it) && imgUrl(it.image_url)
      ? `<img src="${imgUrl(thumb(it.image_url))}" alt="" data-sid="${esc(it.id)}" class="${V.imgOk.has(it.id) ? "on" : ""}">` : "";
    const ph = `<div class="sc-ph">${blocked(it) ? "⚠" : waiting(it) ? "⏳" : it.status === "failed" ? "!" : hasData(it) && !it.image_url ? "无图" : '<span class="spin"></span>'}</div>`;
    const act = failed ? ""
      : done ? `<button class="sc-x sc-undo" data-srestore="${esc(it.id)}" title="恢复为待确认">恢复</button>`
      : it.status === "screening" ? (rj ? `<button class="sc-x sc-undo" data-sundo="${esc(it.id)}" title="撤销排除">撤销</button>`
        : `<button class="sc-x" data-srej="${esc(it.id)}" title="排除（不再分析）" aria-label="排除">✕</button>`) : "";
    const price = it.price != null ? `$${Number(it.price).toFixed(2)}` : "";
    const selBox = it.status === "screening" ? `<label class="sc-sel" title="选择"><input type="checkbox" data-ssel="${esc(it.id)}"${V.sel.has(it.id) ? " checked" : ""}></label>` : "";
    return `<div class="sc-card${rj ? " rej" : ""}${V.sel.has(it.id) ? " sel" : ""}${hasData(it) ? "" : " wait"}" data-skey="${esc(it.id)}">
      <a class="sc-img" href="${esc(it.etsy_url)}" target="_blank" rel="noopener">${ph}${img}${rj ? '<span class="sc-tag">已排除</span>' : it.screen_state === "kept" ? '<span class="sc-tag sc-kept">已进入分析</span>' : ""}</a>${selBox}${act}
      <div class="sc-info"><div class="sc-title" title="${esc(it.title || "")}">${head}</div>
      ${(() => { const src = itemSrc(V.bidx?.get(it.batch_id)); const stt = rj ? "已排除" : it.screen_state === "kept" ? "已进入分析" : failed ? "取数失败" : fetchingIt(it) ? "取数中" : it.status === "screening" ? (bstate === "ready" ? "可确认" : "待确认") : ""; return src || stt ? `<div class="sc-src">${src ? `<span class="sc-kw" title="${esc(src)}">${esc(src)}</span>` : ""}${stt ? `<span class="pill pill-info sc-st">${stt}</span>` : ""}</div>` : ""; })()}
      <div class="sc-meta"><b>${esc(price)}</b>${it.title ? `<span class="hint">#${esc(it.listing_id || "")}</span>` : ""}</div></div></div>`;
  }

  // 预筛选批次列表：「<关键词> 第N批」，按关键词分组（最近发送的关键词在前），组内按发送顺序
  function renderBatches() {
    const el = $("screenBatches"); if (!el) return; const G = screenBatchGroups(V.sbatches || []);
    if (!G.length) { el.innerHTML = ""; return; }
    const demo = (V.sbatches || []).some((b) => b.demo) ? ' <span class="pill pill-bad">演示数字</span>' : "";
    el.innerHTML = `<div class="sc-blist"><div class="sc-bgrp"><span class="sc-bkw">获取链接批次${demo}</span><a class="fchip sc-bchip${V.batch ? "" : " active"}" href="#/screen">全部</a></div>${G.map((g) =>
      `<div class="sc-bgrp"><span class="sc-bkw">${esc(g.keyword)}</span>${g.items.map((it) => `<a class="fchip sc-bchip${V.batch === it.id ? " active" : ""}" href="#/screen/${esc(it.id)}">${esc(it.label)}</a>`).join("")}</div>`).join("")}</div>`;
  }
  function render() {
    V.bidx = batchIndex(V.sbatches || []); renderBatches();
    { const el = $("screenCounts"); if (el) { const rows = batchCountRows(V.items, V.sbatches || []), selOk = (it) => it.status === "screening";
      const nSel = [...V.sel].filter((id) => V.items.some((it) => it.id === id)).length, demoTag = (V.sbatches || []).some((b) => b.demo) ? ' <span class="pill pill-bad">演示数字</span>' : "";
      el.innerHTML = rows.length ? `<div class="sc-counts"><b>本页各批数量：</b>${rows.map((r, i) => `<span class="sc-cnt">${esc(r.label)} ${r.n}个 <button type="button" class="btn btn-ghost btn-xs" data-sselb="${esc(r.ids.join(","))}">全选本批</button></span>${i < rows.length - 1 ? "，" : ""}`).join("")}${demoTag}</div>
        ${nSel ? `<div class="sc-selbar"><b>已选 ${nSel} 条</b><button type="button" class="btn btn-danger-ghost btn-sm" data-sselact="rej">✕ 排除所选</button><button type="button" class="btn btn-ghost btn-sm" data-sselact="undo">撤销排除</button><button type="button" class="btn btn-ghost btn-sm" data-sselact="clear">清除选择</button></div>` : ""}` : ""; } }
    const box = $("screenBody"); if (!box) return;
    const bs = batches(), tot = V.items.length;
    const nRej = V.items.filter((it) => V.rej.has(it.id) || it.screen_state === "rejected").length;
    const nPend = V.items.filter((it) => it.status === "screening").length;
    const nFetch = V.items.filter(fetchingIt).length;
    const nWait = V.items.filter(waiting).length;
    const anom = V.items.filter(blocked), st = getStatus() || {}, svcAnom = svcPaused();
    const banner = anom.length || svcAnom ? `<div class="banner banner-bad sc-banner" data-sbanner="1"><b>EHunt 服务异常</b>（${esc(anomalyReason(svcAnom ? st.reason : anom[0].error))}）：Etsy 取数已暂停${svcAnom ? `，约 北京时间 ${esc(new Date(st.ehunt_paused_until).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false }))} 自动重试` : ""}<span class="banner-sub">${anom.length ? `${anom.length} 条受影响（排队 ${anom.filter(fetchingIt).length} · 失败 ${anom.filter((it) => it.status === "failed").length}）。` : ""}这与商品本身无关，不消耗重试次数；EHunt 恢复后，失败项可点“一键刷新”重新取数（已达 3 次上限的也可以）。</span></div>` : "";
    $("screenSum").innerHTML = tot ? `共 <b>${tot}</b> 条 · ${bs.length} 批 · 待确认 <b>${nPend}</b> · 已排除 ${nRej}${nFetch ? ` · 取数中 ${nFetch}` : ""}${nWait ? ` · <span class="warn">${nWait} 条等待明日 EHunt 额度</span>` : ""}` : "";
    $("screenTabs").innerHTML = [["all", "全部"], ["pending", "待确认"], ["rejected", "已排除"]].map(([k, n]) =>
      `<button class="fchip${V.tab === k ? " active" : ""}" data-stab="${k}">${n}<span class="fchip-n">${k === "all" ? tot : k === "pending" ? nPend - V.items.filter((it) => it.status === "screening" && V.rej.has(it.id)).length : nRej}</span></button>`).join("")
    const nDone = V.items.filter((it) => it.screen_state === "rejected").length; // 只算已确认排除的（未确认的 ✕ 可直接撤销）
    const nRetry = V.items.filter(canRefetch).length;
    const nDead = V.items.filter(deadFailed).length;
    $("screenTabs").innerHTML += `<span class="spacer"></span>${nRetry ? `<button class="btn btn-primary btn-sm" data-srefall="1" ${V.busy.has("refall") ? "disabled" : ""} title="重新取数所有失败且未达 3 次上限的条目（EHunt 服务异常导致的失败不受上限限制）">${V.busy.has("refall") ? '<span class="spin"></span>' : ""}一键刷新（${nRetry} 条失败）</button>` : ""}${nDead ? `<button class="btn btn-danger-ghost btn-sm" data-sdelfail="1" title="永久删除已重试 3 次仍失败的条目（不可恢复）">🗑 一键删除失败（${nDead} 条）</button>` : ""}${nDone ? `<button class="btn btn-danger-ghost btn-sm" data-shide="1" title="永久删除全部已排除条目（不可恢复，不消耗 EHunt 额度）">🗑 删除所有已排除 (${nDone})</button>` : ""}`;
    if (V.loadErr && !tot) { box.innerHTML = `<div class="card empty"><div class="ico">!</div><h3>加载失败</h3><p class="muted">${esc(V.loadErr)}</p><button class="btn btn-primary btn-sm" data-sreload="1">刷新</button></div>`; $("screenFoot").innerHTML = ""; return; }
    if (!tot) { box.innerHTML = `<div class="card empty"><div class="ico">◎</div><h3>${!DEMO && !V.loaded ? "加载中…" : "没有待筛选的链接"}</h3><p class="muted">在“任务”页粘贴链接并勾选“先预筛选”，取到 EHunt 数据后会出现在这里</p></div>`; $("screenFoot").innerHTML = ""; return; }
    box.innerHTML = banner + bs.map((b, i) => {
      const st = batchState(b), pend = pendingOf(b), keep = pend.filter((it) => !V.rej.has(it.id)).length, rj = pend.length - keep;
      const nReady = b.filter((it) => hasData(it) && ready(it)).length;
      const show = b.filter((it) => V.tab === "all" || (V.tab === "pending" ? it.status === "screening" && !V.rej.has(it.id) : V.rej.has(it.id) || it.screen_state === "rejected"));
      const busy = V.busy.has(i);
      const btn = st === "done" ? `<span class="pill pill-ok">✓ 已确认${b.some((it) => it.screen_state === "kept") ? " · 已进入分析" : ""}</span>`
        : `<button class="btn btn-primary btn-sm" data-sconfirm="${i}" ${st !== "ready" || busy || !pend.length ? "disabled" : ""} title="${st === "loading" ? "本批图片加载完成后可确认" : ""}">${busy ? '<span class="spin"></span>' : ""}分析剩余数据（${keep} 条${rj ? `，排除 ${rj}` : ""}）</button>`;
      const allWait = st === "loading" && b.filter(fetchingIt).length && b.filter(fetchingIt).every(waiting) && !nReady;
      const prog = allWait ? `<span class="pill pill-warn">等待明日 EHunt 额度（${b.filter(waiting).length} 条）</span>` : st === "loading" ? `<span class="sc-prog"><span style="width:${Math.round((nReady / b.length) * 100)}%"></span></span><span class="hint">加载中 ${nReady}/${b.length}</span>`
        : st === "ready" ? `<span class="pill pill-info">可确认</span>` : "";
      return `<section class="sc-batch sc-${st}" data-sbatch="${i}"><div class="sc-bh"><h3>第 ${i + 1} 批 <span class="hint">#${i * BATCH + 1}–${i * BATCH + b.length}</span></h3>${prog}<span class="spacer"></span>${btn}</div>
        ${show.length ? `<div class="sc-grid">${show.map((it) => card(it, st)).join("")}</div>` : `<p class="hint sc-none">本批没有符合筛选的条目</p>`}</section>`;
    }).join("");
    const readyAll = bs.filter((b) => batchState(b) === "ready").flatMap(pendingOf), keepAll = readyAll.filter((it) => !V.rej.has(it.id)).length;
    $("screenFoot").innerHTML = `<div class="sc-foot"><div><b>分析剩余数据</b><p class="hint">只有未排除的条目会进入 DeepSeek 识别、Keepamore 匹配和截图；排除的不再产生任何调用${nFetch ? `。还有 ${nFetch} 条在取数，加载完成的批次可先确认` : ""}</p></div>
      <button class="btn btn-primary" data-sconfirm="all" ${readyAll.length && !V.busy.has("all") ? "" : "disabled"}>分析剩余数据（全部已加载 · ${keepAll} 条${readyAll.length - keepAll ? `，排除 ${readyAll.length - keepAll}` : ""}）</button></div>`;
    box.querySelectorAll("img[data-sid]").forEach((im) => { if (im.complete && im.naturalWidth) markImg(im); });
  }
  let rT = null; const rerender = () => { if (!rT) rT = requestAnimationFrame(() => { rT = null; render(); }); };
  function markImg(im, failed = false) {
    const id = im.dataset.sid; if (!V.imgOk.has(id)) { V.imgOk.add(id); im.classList.add("on"); if (failed) im.closest(".sc-img")?.classList.add("broken"); rerender(); }
  }

  async function confirm(which) {
    const bs = batches(), idx = which === "all" ? bs.map((_, i) => i).filter((i) => batchState(bs[i]) === "ready") : [Number(which)];
    const items = idx.flatMap((i) => pendingOf(bs[i])), keep = items.filter((it) => !V.rej.has(it.id)).map((it) => it.id), rej = items.filter((it) => V.rej.has(it.id)).map((it) => it.id);
    if (!items.length) return;
    V.busy.add(which); render();
    let res = { kept: keep.length, rejected: rej.length }, err = null;
    if (!DEMO) { const r = await getSb().rpc("screen_confirm", { p_keep: keep, p_reject: rej }); err = r.error; if (!err) res = r.data; }
    if (DEMO) await new Promise((r) => setTimeout(r, QS.has("slow") ? 1e9 : 400));
    V.busy.delete(which);
    if (err) { toast(`确认失败：${err.message}`, "bad"); return render(); }
    items.forEach((it) => { if (V.rej.has(it.id)) { it.status = "canceled"; it.screen_state = "rejected"; V.rej.delete(it.id); } else { it.status = "queued"; it.screen_state = "kept"; } });
    toast(`✓ ${res.kept} 条进入分析${res.rejected ? `，${res.rejected} 条已排除` : ""}`, "ok", [{ label: "查看任务", href: "#/" }], 4000);
    render(); if (!DEMO) load();
  }
  // EHunt 服务异常导致的失败：即使已达上限也可刷新（032 会把 attempts 归零），且不算“已停止”
  const canRefetch = (it) => it.status === "failed" && it.screen_state !== "kept" && ((it.attempts ?? 0) < (it.max_attempts ?? 3) || anomaly(it));
  const deadFailed = (it) => it.status === "failed" && it.screen_state !== "kept" && (it.attempts ?? 0) >= (it.max_attempts ?? 3) && !anomaly(it);
  async function deleteFailed() {
    const ids = V.items.filter(deadFailed).map((it) => it.id), n = ids.length; if (!n) return;
    let res = { deleted: n, ids };
    if (!DEMO) { const r = await getSb().rpc("screen_delete_failed", V.batch ? { p_batch: V.batch } : {}); if (r.error) return toast(`删除失败：${r.error.message}`, "bad"); res = r.data || res; }
    const gone = new Set(res.ids || ids); V.items = V.items.filter((it) => !gone.has(it.id));
    toast(`已永久删除 ${res.deleted ?? n} 条`, "ok"); render();
  }
  async function refetchAll() {
    const list = V.items.filter(canRefetch); if (!list.length || V.busy.has("refall")) return;
    V.busy.add("refall"); render();
    let n = list.length;
    if (!DEMO) { const { data, error } = await getSb().rpc("screen_refetch_all", V.batch ? { p_batch: V.batch } : {}); V.busy.delete("refall"); if (error) { render(); return toast(`刷新失败：${error.message}`, "bad"); } n = data || 0; }
    else { V.busy.delete("refall"); list.forEach((it) => { if (anomaly(it)) it.attempts = 0; it.status = "queued"; it.error = null; }); }
    if (!n) { toast("无法刷新（已达重试上限或状态已变化）", "bad"); return DEMO ? render() : load(); }
    toast(`已重新排队取数 ${n} 条`, "ok"); render(); if (!DEMO) load();
  }
  async function restore(id) {
    if (!DEMO) { const { error } = await getSb().rpc("screen_restore", { p_ids: [id] }); if (error) return toast(`恢复失败：${error.message}`, "bad"); }
    const it = V.items.find((x) => x.id === id); if (it) { it.status = "screening"; it.screen_state = "awaiting"; }
    toast("已恢复为待确认", "ok"); render();
  }

  async function hideRejected() {
    const ids = V.items.filter((it) => it.screen_state === "rejected").map((it) => it.id), n = ids.length; if (!n) return;
    const ok = await confirmDialog({ title: `删除所有已排除（${n} 条）`, danger: true, ok: `删除 ${n} 条`,
      body: `<p>将<b>永久删除</b> <b>${n}</b> 条已排除的产品（任务及其 EHunt 数据一并删除）。确定删除？</p>
        <p class="hint">删除后不可恢复；不消耗 EHunt 额度。</p>
        <label class="delrej-l"><span>请输入数量 <b>${n}</b> 以确认：</span><input id="scHideN" class="input" inputmode="numeric" autocomplete="off" placeholder="${n}"></label><p id="scHideErr" class="rj-err"></p>`,
      validate: () => { const v = $("scHideN").value.trim() === String(n); $("scHideErr").textContent = v ? "" : `请输入 ${n}`; return v; } });
    if (!ok) return;
    let res = { hidden: n, ids };
    if (!DEMO) { const r = await getSb().rpc("screen_hide_rejected", V.batch ? { p_batch: V.batch } : {}); if (r.error) return toast(`删除失败：${r.error.message}`, "bad"); res = r.data || res; }
    const gone = new Set(res.ids || ids); V.items = V.items.filter((it) => !gone.has(it.id));
    toast(`已永久删除 ${res.deleted ?? res.hidden ?? n} 条`, "ok"); render();
  }

  document.addEventListener("load", (ev) => { const t = ev.target; if (t.tagName === "IMG" && t.dataset?.sid) markImg(t); }, true);
  document.addEventListener("error", (ev) => { const t = ev.target; if (t.tagName === "IMG" && t.dataset?.sid) markImg(t, true); }, true);
  document.addEventListener("click", (ev) => {
    if (!visible()) return;
    const cb = ev.target.closest("[data-ssel]"); if (cb) { ev.stopPropagation(); cb.checked ? V.sel.add(cb.dataset.ssel) : V.sel.delete(cb.dataset.ssel); render(); return; }
    const sb2 = ev.target.closest("[data-sselb],[data-sselact]");
    if (sb2) { ev.preventDefault();
      if (sb2.dataset.sselb) { const ids = new Set(sb2.dataset.sselb.split(",")), L = V.items.filter((it) => ids.has(it.batch_id) && it.status === "screening"); L.forEach((it) => V.sel.add(it.id)); toast(`已选中本批 ${L.length} 条（可批量排除 / 撤销）`, "ok"); }
      else { const a = sb2.dataset.sselact; if (a === "rej") V.sel.forEach((id) => V.rej.add(id)); if (a === "undo") V.sel.forEach((id) => V.rej.delete(id)); if (a !== "clear") toast(a === "rej" ? `已排除 ${V.sel.size} 条（确认前可撤销）` : `已撤销 ${V.sel.size} 条`, "ok"); V.sel.clear(); }
      render(); return; }
    const t = ev.target.closest("[data-srej],[data-sundo],[data-srestore],[data-sconfirm],[data-stab],[data-shide],[data-srefall],[data-sdelfail],[data-sreload]"); if (!t) return;
    ev.preventDefault();
    if (t.dataset.srej) { V.rej.add(t.dataset.srej); toast("已排除，不会进入分析", "", [{ label: "撤销", run: () => { V.rej.delete(t.dataset.srej); render(); } }], 4000); render(); }
    else if (t.dataset.sundo) { V.rej.delete(t.dataset.sundo); render(); }
    else if (t.dataset.srestore) restore(t.dataset.srestore);
    else if (t.dataset.sconfirm) confirm(t.dataset.sconfirm);
    else if (t.dataset.stab) { V.tab = t.dataset.stab; render(); }
    else if (t.dataset.shide) hideRejected();
    else if (t.dataset.srefall) refetchAll();
    else if (t.dataset.sdelfail) deleteFailed();
    else if (t.dataset.sreload) load();
  });

  // ---------- 演示：?demo=1&screen=300#/screen（&sshot=loading|rejected|confirm|bottom） ----------
  function demo(n) {
    const pal = ["#c2410c", "#0e7490", "#7c3aed", "#be185d", "#15803d", "#a16207", "#1d4ed8", "#9f1239"];
    const names = ["Boho Macrame Wall Hanging", "Personalized Pet Portrait", "Minimalist Line Art Print", "Custom Name Necklace", "Ceramic Bud Vase", "Halloween Boo Basket Tag", "Teacher Tote Bag", "Linen Table Runner", "Pressed Flower Frame", "Crochet Bucket Hat"];
    const svg = (i) => "data:image/svg+xml;utf8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="570" height="456"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${pal[i % 8]}"/><stop offset="1" stop-color="${pal[(i + 3) % 8]}"/></linearGradient></defs><rect width="570" height="456" fill="url(#g)"/><circle cx="${150 + (i * 37) % 280}" cy="${140 + (i * 53) % 180}" r="${70 + (i % 5) * 18}" fill="rgba(255,255,255,.22)"/><text x="30" y="420" font-family="sans-serif" font-size="34" fill="rgba(255,255,255,.85)">#${i + 1}</text></svg>`);
    V.items = Array.from({ length: n }, (_, i) => ({ id: `demo-s-${i}`, batch_id: "demo", etsy_url: `https://www.etsy.com/listing/${1900000000 + i}/x`, status: "queued", screen_state: null,
      listing_id: 1900000000 + i, title: null, image_url: null, price: null, _t: `${names[i % names.length]}${i >= names.length ? ` · ${["Gift", "Set of 2", "Vintage Style", "Handmade"][i % 4]}` : ""}`, _img: svg(i), _p: 8 + ((i * 7.3) % 60) }));
    const shot = QS.get("sshot") || "";
    { const t0 = Date.now() - 6 * 864e5, at = (h) => new Date(t0 + h * 36e5).toISOString(), nm = (kw, no, h) => `获取链接 ${kw} 第${no}批 ${new Date(t0 + h * 36e5 + 8 * 36e5).toISOString().slice(5, 16).replace("T", " ")}`; // 演示数字
      V.sbatches = [["reason", 1, 1], ["Alex kitchen", 1, 2], ["reason", 2, 3], ["Alex kitchen", 2, 4], ["Alex kitchen", 2, 4], ["reason", 1, 5]].map(([k, no, h], i) => ({ id: `demo-pb-${i}`, name: nm(k, no, h), created_at: at(h) + "", demo: true })); }
    { const ids = ["demo-pb-0", "demo-pb-2", "demo-pb-0", "demo-pb-1", "demo-pb-3", "demo-pb-5", "demo-pb-0", "demo-pb-4"]; V.items.forEach((it, i) => { it.batch_id = ids[i % ids.length]; }); } // 演示数字
    const quotaFrom = n > 200 ? 200 : n; // 200 条以后等待明日额度（演示额度队列）
    const fill = (it, i) => { if (i >= quotaFrom) { it.error = "EHunt 今日额度已用完，明日自动继续"; return; } it.status = "screening"; it.screen_state = "awaiting"; it.title = it._t; it.image_url = it._img; it.price = it._p; };
    if (shot === "loading") { // 第 1 批已加载完成可确认；第 2、3 批部分加载；其余仍在取数
      V.items.forEach((it, i) => { if (i < 50 || (i < 100 && i % 3) || (i >= 100 && i < 150 && i % 4 === 0)) fill(it, i); else if (i < 150) it.status = "running"; });
      render();
      if (QS.has("sscroll")) setTimeout(() => { const el = document.querySelector('[data-sbatch="1"]'); if (el) window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - (innerWidth < 640 ? 260 : 330)); }, 300);
      return;
    }
    V.items.forEach(fill);
    if (shot === "failed") [2, 5, 8].forEach((k, j) => { const it = V.items[k]; if (it) { it.status = "failed"; it.screen_state = null; it.title = it.image_url = null; it.error = "EHunt 取数失败"; it.attempts = j === 2 ? 3 : j + 1; it.max_attempts = 3; } });
    if (shot === "ehuntq") { // 一键刷新后：28 条重新排队（error 已清空），其中 2 条已被 worker 探测并写上异常原因
      for (let k = 0; k < 28; k++) { const it = V.items[k * 2]; if (it) Object.assign(it, { status: "queued", screen_state: null, title: null, image_url: null, attempts: 0, max_attempts: 3,
        error: k < 2 ? "EHunt 服务异常（查询失败: 'daily_limit'），已暂停取数，自动重试" : null }); }
    }
    if (shot === "ehunt") { // EHunt 服务异常：前 6 条为旧 worker 已达上限的失败项，第 2 批 4 条为新 worker 熔断中排队
      [0, 3, 6, 9, 12, 15].forEach((k) => { const it = V.items[k]; if (it) Object.assign(it, { status: "failed", screen_state: null, title: null, image_url: null, error: "EHunt 错误 500: 查询失败: 'daily_limit'", attempts: 3, max_attempts: 3 }); });
      [50, 51, 52, 53].forEach((k) => { const it = V.items[k]; if (it) Object.assign(it, { status: "queued", screen_state: null, title: null, image_url: null, error: "EHunt 服务异常（查询失败: 'daily_limit'），已暂停取数，自动重试", attempts: 0, max_attempts: 3 }); });
      const it = V.items[20]; if (it) Object.assign(it, { status: "failed", screen_state: null, title: null, image_url: null, error: "商品不存在或已下架（EHunt 未返回 listing）", attempts: 3, max_attempts: 3 });
    }
    if (shot === "ehuntweb") { // EHunt 网页回退：只有主图（无标题/价格）；前 5 条为 2026-10-19 box 实测取到的真实主图
      const real = { 603690215: "10483440/r/il/510f63/1464094458/il_570xN.1464094458_rses.jpg", 1084985134: "12905279/r/il/ec7387/3390356664/il_570xN.3390356664_cj3w.jpg",
        761418342: "8121651/r/il/819371/8233320967/il_570xN.8233320967_47vk.jpg", 4527292345: "61497210/r/il/7d7f0a/8221478505/il_570xN.8221478505_enx8.jpg",
        1766735966: "43514590/r/il/e0f23e/6256738411/il_570xN.6256738411_frjz.jpg" };
      const ids = Object.keys(real);
      V.items.forEach((it, i) => { it.title = null; it.price = null; if (i < ids.length) { it.listing_id = Number(ids[i]); it.etsy_url = `https://www.etsy.com/listing/${ids[i]}`; it.image_url = `https://i.etsystatic.com/${real[ids[i]]}`; } });
      [6, 7, 8].forEach((k) => { const it = V.items[k]; if (it) Object.assign(it, { status: "queued", screen_state: null, image_url: null, error: "EHunt 网页取数排队中（box 处理中），稍后自动继续" }); });
      const nf = V.items[9]; if (nf) Object.assign(nf, { status: "failed", screen_state: null, image_url: null, error: "商品不存在或已下架（EHunt 网页搜不到 listing 1900000009）", attempts: 1, max_attempts: 3 });
    }
    if (shot === "ehuntlogin") V.items.forEach((it, i) => { if (i % 2) Object.assign(it, { status: "queued", screen_state: null, title: null, image_url: null, price: null,
      error: "EHunt 服务异常（EHunt 未登录：需要在 box 上运行 deploy/box-selection-worker.sh login 重新登录），已暂停取数，自动重试" }); else { it.title = null; it.price = null; } });
    if (shot === "excluded") [1, 4, 7, 9, 12, 53, 58, 61].forEach((k) => { const it = V.items[k]; if (it) { it.status = "canceled"; it.screen_state = "rejected"; } });
    if (shot === "rejected" || shot === "confirm" || shot === "bottom") [1, 4, 7, 9, 12, 53, 58].forEach((k) => V.rej.add(V.items[k].id));
    render();
    if (shot === "confirm") setTimeout(() => confirm("0"), 600);
    if (shot === "bottom") setTimeout(() => window.scrollTo(0, document.body.scrollHeight), 600);
    if (!shot) { // 交互演示：逐批“取数完成”
      let k = 0; V.items.forEach((it) => { it.status = "running"; it.title = it.image_url = null; it.screen_state = null; }); render();
      const t = setInterval(() => { for (let j = 0; j < 12 && k < V.items.length; j++, k++) fill(V.items[k], k); render(); if (k >= V.items.length) clearInterval(t); }, 250);
    }
  }

  return {
    show(batch) { V.batch = batch || null; if (DEMO) { if (!V.demo) { V.demo = true; demo(Number(QS.get("screen")) || 300); } else render(); return; } render(); load(); },
    hide() { clearTimeout(V.timer); },
    reload: load,
    rerender: () => { if (visible()) render(); }, // service_status 变化（EHunt 熔断/恢复）时刷新横幅
  };
}
