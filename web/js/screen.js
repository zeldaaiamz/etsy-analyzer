// 预筛选视图（#/screen[/<batch>]）：粘贴 → worker 只取 EHunt 数据 → 每 50 条一批看大图，✕ 排除 / 撤销
// → 每批“分析剩余数据”或底部“全部已加载”一键确认 → 只有保留的进入 DeepSeek / Keepamore / 截图（017 screen_confirm）
export const BATCH = 50;
const POLL_MS = 3000;

export function createScreen({ $, esc, imgUrl, toast, getSb, DEMO, QS, confirmDialog }) {
  const V = { loadErr: null, items: [], batch: null, tab: "all", rej: new Set(), imgOk: new Set(), busy: new Set(), loaded: false, timer: null, demo: null };
  const thumb = (u) => (/^https:\/\/i\.etsystatic\.com\//.test(u || "") ? u.replace(/\/il_[0-9a-zA-Z]+x[0-9a-zA-Z]+\./, "/il_570xN.") : u);
  const hasData = (it) => it.status === "screening" || it.screen_state === "rejected" || it.screen_state === "kept";
  const fetchingIt = (it) => (it.status === "queued" || it.status === "running") && it.screen_state !== "kept";
  const waiting = (it) => fetchingIt(it) && it.status === "queued" && /额度/.test(it.error || "");
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
    const { data, error } = await sb.rpc("screen_list", V.batch ? { p_batch: V.batch } : {});
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
    const head = it.title ? esc(it.title) : waiting(it) ? "等待 EHunt 额度（明日自动继续）" : it.status === "failed" ? (left > 0 ? "取数失败" : `取数失败（已重试 ${it.attempts ?? 3} 次，已停止）`) : "读取 Etsy 数据中…";
    const img = hasData(it) && imgUrl(it.image_url)
      ? `<img src="${imgUrl(thumb(it.image_url))}" alt="" data-sid="${esc(it.id)}" class="${V.imgOk.has(it.id) ? "on" : ""}">` : "";
    const ph = `<div class="sc-ph">${waiting(it) ? "⏳" : it.status === "failed" ? "!" : hasData(it) && !it.image_url ? "无图" : '<span class="spin"></span>'}</div>`;
    const act = failed ? (left > 0 ? `<button class="sc-x sc-undo" data-srefetch="${esc(it.id)}" title="重新取数（剩余 ${left} 次）">${V.busy.has(it.id) ? '<span class="spin"></span>' : "刷新"}</button>` : "")
      : done ? `<button class="sc-x sc-undo" data-srestore="${esc(it.id)}" title="恢复为待确认">恢复</button>`
      : it.status === "screening" ? (rj ? `<button class="sc-x sc-undo" data-sundo="${esc(it.id)}" title="撤销排除">撤销</button>`
        : `<button class="sc-x" data-srej="${esc(it.id)}" title="排除（不再分析）" aria-label="排除">✕</button>`) : "";
    const price = it.price != null ? `$${Number(it.price).toFixed(2)}` : "";
    return `<div class="sc-card${rj ? " rej" : ""}${hasData(it) ? "" : " wait"}" data-skey="${esc(it.id)}">
      <a class="sc-img" href="${esc(it.etsy_url)}" target="_blank" rel="noopener">${ph}${img}${rj ? '<span class="sc-tag">已排除</span>' : it.screen_state === "kept" ? '<span class="sc-tag sc-kept">已进入分析</span>' : ""}</a>${act}
      <div class="sc-info"><div class="sc-title" title="${esc(it.title || "")}">${head}</div>
      <div class="sc-meta"><b>${esc(price)}</b><span class="hint">#${esc(it.listing_id || "")}</span></div></div></div>`;
  }

  function render() {
    const box = $("screenBody"); if (!box) return;
    const bs = batches(), tot = V.items.length;
    const nRej = V.items.filter((it) => V.rej.has(it.id) || it.screen_state === "rejected").length;
    const nPend = V.items.filter((it) => it.status === "screening").length;
    const nFetch = V.items.filter(fetchingIt).length;
    const nWait = V.items.filter(waiting).length;
    $("screenSum").innerHTML = tot ? `共 <b>${tot}</b> 条 · ${bs.length} 批 · 待确认 <b>${nPend}</b> · 已排除 ${nRej}${nFetch ? ` · 取数中 ${nFetch}` : ""}${nWait ? ` · <span class="warn">${nWait} 条等待明日 EHunt 额度</span>` : ""}` : "";
    $("screenTabs").innerHTML = [["all", "全部"], ["pending", "待确认"], ["rejected", "已排除"]].map(([k, n]) =>
      `<button class="fchip${V.tab === k ? " active" : ""}" data-stab="${k}">${n}<span class="fchip-n">${k === "all" ? tot : k === "pending" ? nPend - V.items.filter((it) => it.status === "screening" && V.rej.has(it.id)).length : nRej}</span></button>`).join("")
    const nDone = V.items.filter((it) => it.screen_state === "rejected").length; // 只算已确认排除的（未确认的 ✕ 可直接撤销）
    $("screenTabs").innerHTML += `<span class="spacer"></span>${nDone ? `<button class="btn btn-danger-ghost btn-sm" data-shide="1" title="永久删除全部已排除条目（不可恢复，不消耗 EHunt 额度）">🗑 删除所有已排除 (${nDone})</button>` : ""}`;
    if (V.loadErr && !tot) { box.innerHTML = `<div class="card empty"><div class="ico">!</div><h3>加载失败</h3><p class="muted">${esc(V.loadErr)}</p><button class="btn btn-primary btn-sm" data-sreload="1">刷新</button></div>`; $("screenFoot").innerHTML = ""; return; }
    if (!tot) { box.innerHTML = `<div class="card empty"><div class="ico">◎</div><h3>${!DEMO && !V.loaded ? "加载中…" : "没有待筛选的链接"}</h3><p class="muted">在“任务”页粘贴链接并勾选“先预筛选”，取到 EHunt 数据后会出现在这里</p></div>`; $("screenFoot").innerHTML = ""; return; }
    box.innerHTML = bs.map((b, i) => {
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
  async function refetch(id) {
    const it = V.items.find((x) => x.id === id); if (!it || V.busy.has(id)) return;
    if ((it.attempts ?? 0) >= (it.max_attempts ?? 3)) return toast("已达最多 3 次重试，不再取数", "bad");
    V.busy.add(id); render();
    let n = 1;
    if (!DEMO) { const { data, error } = await getSb().rpc("screen_refetch", { p_ids: [id] }); V.busy.delete(id); if (error) { render(); return toast(`刷新失败：${error.message}`, "bad"); } n = data || 0; }
    else V.busy.delete(id);
    if (!n) { toast("无法刷新（已达重试上限或状态已变化）", "bad"); return DEMO ? render() : load(); }
    it.status = "queued"; it.error = null; toast("已重新排队取数", "ok"); render(); if (!DEMO) load();
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
    const t = ev.target.closest("[data-srej],[data-sundo],[data-srestore],[data-sconfirm],[data-stab],[data-shide],[data-srefetch],[data-sreload]"); if (!t) return;
    ev.preventDefault();
    if (t.dataset.srej) { V.rej.add(t.dataset.srej); toast("已排除，不会进入分析", "", [{ label: "撤销", run: () => { V.rej.delete(t.dataset.srej); render(); } }], 4000); render(); }
    else if (t.dataset.sundo) { V.rej.delete(t.dataset.sundo); render(); }
    else if (t.dataset.srestore) restore(t.dataset.srestore);
    else if (t.dataset.sconfirm) confirm(t.dataset.sconfirm);
    else if (t.dataset.stab) { V.tab = t.dataset.stab; render(); }
    else if (t.dataset.shide) hideRejected();
    else if (t.dataset.srefetch) refetch(t.dataset.srefetch);
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
  };
}
