// 获取链接 · 标题排除词表（后台表 sel_exclude_words；worker 每 5 分钟加载）。所有人可查看，sel_admins 里的人可增改停用删除。
const CAT = { custom: "定制", ip: "IP" };
const NEVER = ["wholesale", "bulk", "vintage"];
// 演示数据：与 classify.py 内置兜底 / 031 种子一致（节选 + 新增 address）
const DEMO_WORDS = [
  ...["personali[sz]ed?", "personalisation", "personalization", "custom(i[sz]ed)?", "your (name|text|photo|logo)", "with (name|photo|names)", "name", "names", "photo(?!\\s*(booths?|props?)\\b)", "monogram\\w*", "initials?", "engraved", "any text", "logo", "return address", "address"].map((w) => ["custom", w]),
  ...["disney", "marvel", "pokemon", "star wars", "harry potter", "hello kitty", "barbie", "nfl", "taylor swift", "bluey", "stitch", "grinch", "snoopy", "paw patrol"].map((w) => ["ip", w]),
].map(([category, word], i) => ({ id: i + 1, category, word, enabled: true, note: /address/.test(word) ? "v2 新增" : null }));
export function createExWords({ $, esc, toast, getSb, DEMO, confirmDialog }) {
  const W = { rows: null, admin: false, open: false, cat: "custom", err: "" };
  async function load() {
    if (DEMO) { W.rows ||= DEMO_WORDS.map((r) => ({ ...r })); W.admin = new URLSearchParams(location.search).has("wordadmin"); return render(); }
    const sb = getSb(); if (!sb) return;
    const [{ data, error }, adm] = await Promise.all([sb.from("sel_exclude_words").select("*").order("category").order("id"), sb.rpc("sel_is_admin")]);
    W.rows = error ? [] : data || []; W.admin = !!adm.data; W.err = error ? `排除词表未就绪（需要迁移 031）：${error.message}` : ""; render();
  }
  async function save(id, patch) {
    const r = W.rows.find((x) => x.id === id) || {}, next = { ...r, ...patch };
    const word = (next.word || "").trim();
    if (!word) return toast("排除词不能为空", "bad");
    if (NEVER.includes(word.toLowerCase())) return toast("wholesale / bulk / vintage 不排除", "bad");
    try { new RegExp(word); } catch { return toast("格式不对（正则片段无效）", "bad"); }
    if (DEMO) { if (id) Object.assign(r, next, { word }); else W.rows.push({ ...next, word, id: Date.now() }); return render(); }
    const { error } = await getSb().rpc("sel_upsert_exclude_word", { p_id: id || null, p_category: next.category, p_word: word, p_enabled: next.enabled ?? true, p_note: next.note || null });
    if (error) return toast(error.message, "bad"); toast("已保存，worker 5 分钟内生效", "ok"); load();
  }
  async function del(id) {
    const r = W.rows.find((x) => x.id === id);
    if (!await confirmDialog({ title: "删除排除词？", ok: "删除", body: `<p>删除「${esc(r?.word)}」后，标题含这个词的商品不再被规则直接排除（仍会走豆包判断）。只想暂时不用可以点“停用”。</p>` })) return;
    if (DEMO) { W.rows = W.rows.filter((x) => x.id !== id); return render(); }
    const { error } = await getSb().rpc("sel_delete_exclude_word", { p_id: id }); if (error) return toast(error.message, "bad"); load();
  }
  function render() {
    const el = $("ssWords"); if (!el || !W.rows) return;
    const rows = W.rows.filter((r) => r.category === W.cat), n = (c) => W.rows.filter((r) => r.category === c && r.enabled).length;
    el.innerHTML = `<details class="card ex-words"${W.open ? " open" : ""}><summary><b>标题排除词表</b> <span class="hint">定制 ${n("custom")} · IP ${n("ip")} · 标题整词命中即排除，不调豆包；每条排除在 box 日志里记下命中的词</span></summary>
      ${W.err ? `<p class="warn">${esc(W.err)}</p>` : ""}
      <p class="hint">不排除：wholesale / bulk / vintage，以及素材/数字文件类（svg、printable、blank 等，照常判断）。通用文字（Happy Birthday、Mr &amp; Mrs、photo booth/prop 等）和标题写 optional personalization 的不算定制。${W.admin ? "" : "只有管理员可以修改。"}</p>
      <div class="fchips">${Object.entries(CAT).map(([k, v]) => `<button class="fchip${W.cat === k ? " active" : ""}" data-exwcat="${k}">${v} ${W.rows.filter((r) => r.category === k).length}</button>`).join("")}</div>
      <div class="ex-list">${rows.map((r) => `<span class="ex-word${r.enabled ? "" : " off"}" title="${esc(r.note || "")}"><code>${esc(r.word)}</code>${r.note ? ` <span class="pill pill-info">${esc(r.note)}</span>` : ""}${W.admin ? ` <button class="btn btn-link btn-sm" data-exwtog="${r.id}">${r.enabled ? "停用" : "启用"}</button><button class="btn btn-link btn-sm" data-exwdel="${r.id}">删除</button>` : ""}</span>`).join("")}</div>
      ${W.admin ? `<div class="ss-form ex-add"><input id="exwNew" class="input input-sm" placeholder="新增${CAT[W.cat]}词，如 return address"><button class="btn btn-primary btn-sm" id="exwAdd">添加</button></div>` : ""}</details>`;
  }
  document.addEventListener("click", (e) => {
    const t = e.target, d = t.dataset || {};
    if (t.closest?.(".ex-words summary")) { W.open = !t.closest(".ex-words").open; return; }
    if (d.exwcat) { W.cat = d.exwcat; W.open = true; return render(); }
    if (d.exwtog) { const r = W.rows.find((x) => String(x.id) === d.exwtog); return save(r.id, { enabled: !r.enabled }); }
    if (d.exwdel) return del(W.rows.find((x) => String(x.id) === d.exwdel)?.id);
    if (t.id === "exwAdd") return save(null, { category: W.cat, word: $("exwNew").value, enabled: true });
  });
  return { show: load };
}
