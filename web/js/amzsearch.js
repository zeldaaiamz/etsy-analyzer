// PM-001 一键亚马逊搜索：纯函数 + 渲染（无全局依赖，便于 node 测试）
export const MAX_KW = 5;
export const SRC_NAME = { title: "标题", image: "图片", review: "评论", "original-title": "原标题", manual: "手动" };
export const DIR_NAME = { 1: "精准词", 2: "同场景相似品", 3: "产品文案/卖点词", 4: "人群礼物词", 5: "细分场景词" };
const dirTag = (d, esc) => DIR_NAME[d] ? `<b class="akw-d d${d}" title="方向 D${d}：${esc(DIR_NAME[d])}">D${d} ${esc(DIR_NAME[d])}</b>` : "";
const dirSel = (d) => `<select class="akw-dir" data-akwdir title="方向"><option value="">方向</option>${Object.entries(DIR_NAME).map(([k, v]) => `<option value="${k}"${Number(d) === Number(k) ? " selected" : ""}>D${k} ${v}</option>`).join("")}</select>`;
export const SHOW_STAGES = new Set(["candidate", "active"]); // 备选 / 进行中；不考虑(rejected) 不显示
export const canShowAmz = (stage) => SHOW_STAGES.has(stage || "candidate");
export const amzSearchUrl = (kw) => `https://www.amazon.com/s?k=${encodeURIComponent(kw)}`;
export const amzAsinUrl = (asin) => `https://www.amazon.com/dp/${encodeURIComponent(asin)}`;
export const normKw = (s) => String(s ?? "").trim().replace(/\s+/g, " ").toLowerCase();
export const kwList = (r) => (Array.isArray(r?.amazon_keywords) ? r.amazon_keywords : []).filter((k) => k && normKw(k.keyword));
export function amzUrls(r, withAsin = false) {
  const urls = kwList(r).map((k) => amzSearchUrl(k.keyword));
  if (withAsin && /^[A-Z0-9]{10}$/.test(r?.competitor_asin || "")) urls.push(amzAsinUrl(r.competitor_asin));
  return urls;
}
// 必须在 click 处理函数里同步调用（之前不能有 await），否则浏览器会拦截。返回被拦截的数量。
// 不传 "noopener"：带它时规范要求 window.open 返回 null，就无法检测拦截 → 打开后手动断开 opener
export function openAll(urls, win = window) {
  let blocked = 0;
  for (const u of urls) { const w = win.open(u, "_blank"); if (!w) blocked++; else try { w.opener = null; } catch { /* 忽略 */ } }
  return blocked;
}
// 编辑：返回规范化后的列表；新增的一律 source=manual；最多 5 个；去重
// rows：字符串，或 {keyword, direction}（编辑界面可改方向）
export function applyEdit(old, rows) {
  const byKw = new Map((old || []).map((k) => [normKw(k.keyword), k])), seen = new Set(), out = [];
  for (const raw of rows) { const kw = normKw(typeof raw === "string" ? raw : raw?.keyword); if (!kw || seen.has(kw)) continue; seen.add(kw);
    const d = Number(typeof raw === "object" ? raw?.direction : NaN), o = byKw.get(kw);
    const item = o ? { ...o, keyword: kw } : { keyword: kw, source: "manual", basis: "人工添加" };
    if (d >= 1 && d <= 5) item.direction = d;
    out.push(item); }
  if (out.length > MAX_KW) throw new Error(`最多 ${MAX_KW} 个关键词`);
  return out;
}
export function amzBlock(r, { esc, editing = false, asinOn = false, open = false }) {
  const kws = kwList(r), id = esc(r.id);
  const chips = kws.length ? kws.map((k) => `<span class="akw" title="${esc(k.basis || "")}">${dirTag(k.direction, esc)}<span class="akw-k">${esc(k.keyword)}</span><em class="akw-s s-${esc(k.source)}">${esc(SRC_NAME[k.source] || k.source)}</em></span>`).join("")
    : '<span class="hint">关键词未生成</span>';
  if (editing) return `<div class="amz amz-edit" data-amzbox="${id}"><div class="amz-h">编辑亚马逊搜索词（最多 ${MAX_KW} 个，新增记为“手动”，可选方向 D1–D5）</div>
    ${kws.map((k) => `<div class="akw-row">${dirSel(k.direction)}<input class="input akw-in" maxlength="80" value="${esc(k.keyword)}" data-akwin><em class="akw-s s-${esc(k.source)}">${esc(SRC_NAME[k.source] || k.source)}</em><button class="btn btn-link btn-sm" data-akwdel title="删除">✕</button></div>`).join("")}
    <div class="row"><button class="btn btn-ghost btn-sm" data-akwadd="${id}"${kws.length >= MAX_KW ? " disabled" : ""}>＋ 添加</button><span class="spacer"></span>
    <button class="btn btn-ghost btn-sm" data-akwcancel="${id}">取消</button><button class="btn btn-primary btn-sm" data-akwsave="${id}">保存</button></div></div>`;
  const show = canShowAmz(r.stage), asin = /^[A-Z0-9]{10}$/.test(r.competitor_asin || "") ? r.competitor_asin : null;
  const d1 = kws.find((k) => Number(k.direction) === 1) || kws[0];
  const edit = `<button class="btn btn-link btn-sm akw-ed" data-akwedit="${id}" title="编辑搜索词">✎</button>`;
  const head = kws.length ? `<div class="akw-head"><button class="akw-tog" data-akwtog="${id}" aria-expanded="${open}">关键词 (${kws.length}) ${open ? "▴" : "▾"}</button>${edit}</div>`
    : `<div class="akw-head"><span class="hint">关键词未生成</span>${show ? edit : ""}</div>`;
  const body = !kws.length ? "" : open ? `<div class="akws">${chips}</div>`
    : `<div class="akw-sum" title="${esc(d1.keyword)}">${dirTag(d1.direction, esc)}<span class="akw-k">${esc(d1.keyword)}</span></div>`;
  return `<div class="amz">${head}${body}
    ${show ? `<div class="amz-acts"><span class="amz-wrap" title="${kws.length ? `在亚马逊美国站打开 ${kws.length + (asin && asinOn ? 1 : 0)} 个新标签页` : "关键词未生成"}">
      <button class="btn btn-amz btn-sm" data-amz="${id}"${kws.length ? "" : " disabled"}>🔍 一键亚马逊搜索${kws.length ? ` (${kws.length})` : ""}</button></span>
      ${asin ? `<label class="amz-asin" title="同时打开竞品商品页 amazon.com/dp/${esc(asin)}"><input type="checkbox" data-amzasin="${id}"${asinOn ? " checked" : ""}> 竞品 ${esc(asin)}</label>` : ""}</div>` : ""}</div>`;
}
export const DIR_SELECT_HTML = dirSel("");
