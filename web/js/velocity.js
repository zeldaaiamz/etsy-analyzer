// 评论速度（只在用户点“按评论速度排序”时调用）。输入 = 每个 listing 的评论日期列表。
// 规格（数据 agent / Hongyan 确认）：近 6 个月评论，按新到旧：
// 1 连续性：有评论的月份数 0-6（主排序）  2 6 个月新增 + 月均（次排序）
// 3 趋势：近 3 月 vs 前 3 月  4 新鲜度：距最近评论天数，>30 天标记可能停滞。仅用于同类产品对比。
const DAY = 864e5;
export function computeVelocity(dates, now = Date.now()) {
  if (!dates) return null; // 无数据（≠ 0 条评论）
  const t = dates.map((d) => new Date(d).getTime()).filter((x) => !isNaN(x) && x <= now).sort((a, b) => b - a);
  const win = t.filter((x) => now - x < 180 * DAY);
  const months = new Set(win.map((x) => Math.min(5, Math.floor((now - x) / (30 * DAY)))));
  const recent = win.filter((x) => now - x < 90 * DAY).length, prior = win.length - recent;
  const days = t.length ? Math.floor((now - t[0]) / DAY) : null;
  const trend = recent > prior ? "up" : recent < prior ? "down" : "flat";
  return { months: months.size, n6: win.length, avg: +(win.length / 6).toFixed(1), recent, prior, trend, days, stale: days == null || days > 30 };
}
export const cmpVelocity = (a, b) => (!a) - (!b) || (a && b ? b.months - a.months || b.n6 - a.n6 : 0);
const TR = { up: "↑ 上升", down: "↓ 降温", flat: "→ 持平" };
export function velocityBadge(v, esc) {
  if (!v) return `<span class="pb pb-vel none" title="该商品还没有评论日期（抓取失败且未手动填写，或尚未抓取）；排在最后">无评论日期数据</span>`;
  const tip = `连续性：近 6 月中 ${v.months} 个月有评论（主排序）\n6 月新增：${v.n6} 条，月均 ${v.avg}（次排序）\n趋势：近 3 月 ${v.recent} vs 前 3 月 ${v.prior}（${TR[v.trend]}）\n新鲜度：${v.days == null ? "无评论" : `最近评论 ${v.days} 天前`}${v.stale ? "，可能停滞" : ""}\n仅用于同类产品对比`;
  const M = v.shot ? '<span class="pb pb-man" title="来自评论截图 OCR（已核对）">截图</span>' : v.manual ? '<span class="pb pb-man" title="手动填写的评论速度">手动</span>' : "";
  return M + `<span class="pb pb-vel${v.stale ? " stale" : ""}" title="${esc(tip)}">速度 ${v.months}/6 · ${v.n6}条 ${TR[v.trend][0]}${v.stale ? " · 停滞?" : ""}</span>`;
}
// 049 自动抓取状态徽标（无速度数据时显示）：pending/fetching → 抓取中；failed → 重试 + 手动录入；unavailable → 产品不可售；其它 → ""
export const SCRAPE_BUSY = new Set(["pending", "fetching"]);
const escA = (x) => String(x ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export function scrapeChip(status, id, why) {
  if (SCRAPE_BUSY.has(status)) return `<span class="pb pb-scr" title="新加入产品库，后台正在自动抓取 Etsy 评论日期（评论抓取优先，可与获取链接/预筛选并行）">评论抓取中</span>`;
  if (status === "failed") return `<button type="button" class="pb pb-scr bad" data-rretry="${Number(id)}" title="自动抓取 Etsy 评论日期失败；点击重新排队抓取（也可上传截图或手动录入）">抓取失败 重试</button>`
    + `<button type="button" class="pb pb-ment" data-mentry="${Number(id)}" title="商品仍在售但一直抓不到：手动录入近 6 个月每 30 天的评论数，按同一公式算评论速度">手动录入</button>`;
  // 056：Etsy 已下架 / 售罄 / 店铺暂停 → 不再自动重试；↻ 仅在商品恢复在售时手动点
  if (status === "unavailable") return `<span class="pb pb-scr bad unav" title="${escA(`Etsy 页面显示该商品不可购买${why ? `：${why}` : ""}。不再自动重试，也不计入一键「手动抓取评论速度」`)}">抓取失败：产品不可售</span>`
    + `<button type="button" class="pb pb-rechk" data-rretry="${Number(id)}" title="如果商品已恢复在售，点此重新抓取一次">↻</button>`;
  return "";
}
// 056：手动录入是否生效——有手动值时优先；只有「之后」显式重抓成功（status=ok 且 updated_at 晚于手动保存时间）才让位给抓取数据
export function manualActive(man, status, statusAt) {
  if (!man) return false;
  if (status !== "ok") return true;
  const a = new Date(statusAt || 0).getTime(), m = new Date(man.updated_at || 0).getTime();
  return !(a > m);
}
// 056：手动录入的 6 段（按「距今天数」每 30 天一段，与 computeVelocity 的分段一致）
export const MANUAL_BUCKETS = ["近 30 天", "31–60 天", "61–90 天", "91–120 天", "121–150 天", "151–180 天"];
const ymd = (t) => { const d = new Date(t); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }; // 本地日期
export function manualBucketRanges(now = Date.now()) { // 每段对应的日期区间（含两端），提示用户按评论日期数
  const f = (t) => ymd(t).slice(5);
  return MANUAL_BUCKETS.map((_, i) => `${f(now - (30 * i + 29) * DAY)} ~ ${f(now - 30 * i * DAY)}`);
}
// 056：校验手动录入；raw = 6 个输入字符串（空 = 0），latest = "YYYY-MM-DD" 或 ""。返回 { ok, err } 或 { ok, counts, latest }
export function validateManualEntry(raw, latest, now = Date.now()) {
  const counts = [];
  for (let i = 0; i < 6; i++) {
    const v = String(raw?.[i] ?? "").trim();
    if (v === "") { counts.push(0); continue; }
    if (!/^\d+$/.test(v) || Number(v) > 10000) return { ok: false, err: `「${MANUAL_BUCKETS[i]}」需为 0–10000 的整数` };
    counts.push(Number(v));
  }
  const n = counts.reduce((a, b) => a + b, 0);
  latest = String(latest || "").trim();
  if (!latest) return n ? { ok: false, err: "有评论时请填写「最近一条评论日期」" } : { ok: true, counts, latest: null };
  if (!/^\d{4}-\d{2}-\d{2}$/.test(latest) || isNaN(new Date(latest))) return { ok: false, err: "最近一条评论日期格式不对" };
  const today = ymd(now);
  if (latest > today) return { ok: false, err: "最近一条评论日期不能晚于今天" };
  const age = Math.max(0, Math.floor((now - new Date(latest).getTime()) / DAY));
  if (age > 3650) return { ok: false, err: "最近一条评论日期太早" };
  if (age < 180) {
    const b = Math.min(5, Math.floor(age / 30));
    if (counts.slice(0, b).some((x) => x > 0)) return { ok: false, err: `最近一条评论在 ${age} 天前，比它更近的时段应为 0` };
    if (counts[b] < 1) return { ok: false, err: `最近一条评论在 ${age} 天前，「${MANUAL_BUCKETS[b]}」至少应有 1 条` };
  } else if (n) return { ok: false, err: "最近一条评论在 180 天前，6 段都应为 0" };
  return { ok: true, counts, latest };
}
// 速度模式下的卡片徽标：有速度 → 速度徽标；没有 → 抓取状态；都没有 → 「无评论日期数据」
export function cardVelocityBadge(v, status, id, esc) { return v ? velocityBadge(v, esc) : scrapeChip(status, id) || velocityBadge(null, esc); }
// 数据适配器：返回 Map<listing_id, 日期数组>；没有数据的 listing 不在 Map 中
export async function loadReviewDates(sb, ids) {
  const out = new Map();
  for (let i = 0; i < ids.length; i += 200) {
    const since = new Date(Date.now() - 200 * DAY).toISOString().slice(0, 10);
    const { data: f, error: e1 } = await sb.from("listing_reviews").select("listing_id").in("listing_id", ids.slice(i, i + 200)).limit(10000);
    if (e1) throw e1; (f || []).forEach((r) => out.set(Number(r.listing_id), []));
    const { data, error } = await sb.from("listing_reviews").select("listing_id,review_date").in("listing_id", ids.slice(i, i + 200)).gte("review_date", since).order("review_date", { ascending: false }).limit(10000);
    if (error) throw error;
    (data || []).forEach((r) => out.get(Number(r.listing_id))?.push(r.review_date));
  }
  return out;
}
// 演示：确定性的示例评论日期
export function demoReviewDates(ids, now) {
  const P = [[1, 1, 1, 1, 1, 1, 2, 3], [0.5, 0.5, 2, 4], [0.4, 1.2, 2, 2.5, 3.1, 4, 5.5], [2, 3.5, 4.8, 5.2], [1.5, 2.6, 3.2, 3.9, 4.4, 5.1, 5.6, 5.9], null, [0.1, 0.3, 0.6, 0.9, 1.1, 1.6, 2.2, 2.8]];
  const m = new Map();
  ids.forEach((id, i) => { const p = P[i % P.length]; if (p) m.set(id, p.flatMap((mo, k) => [now - (mo * 30 + (k % 3)) * DAY]).map((x) => new Date(x).toISOString())); });
  return m;
}
// 手动填写：6 个月每月评论数（[0]=最近 30 天）+ 最近评论日期 → 与 computeVelocity 同结构
export function computeManualVelocity(m, now = Date.now()) {
  if (!m) return null;
  const c = (m.month_counts || []).slice(0, 6).map((x) => Math.max(0, Number(x) || 0)); while (c.length < 6) c.push(0);
  const n6 = c.reduce((a, b) => a + b, 0), recent = c[0] + c[1] + c[2], prior = n6 - recent;
  const lt = m.latest_review_date ? new Date(m.latest_review_date).getTime() : NaN, days = isNaN(lt) ? null : Math.max(0, Math.floor((now - lt) / DAY));
  return { months: c.filter((x) => x > 0).length, n6, avg: +(n6 / 6).toFixed(1), recent, prior,
    trend: recent > prior ? "up" : recent < prior ? "down" : "flat", days, stale: days == null || days > 30, manual: true };
}
// 抓取状态 + 手动值（加载产品库时取，体量小；不做评分）
export async function loadScrapeInfo(sb, ids) {
  const st = new Map(), man = new Map(), at = new Map(), why = new Map();
  if (!ids.length) return { st, man, ocr: new Map(), at, why };
  const [a, b, c] = await Promise.all([sb.from("listing_scrape_status").select("listing_id,status,error,updated_at").in("listing_id", ids),
    sb.from("listing_review_manual").select("listing_id,month_counts,latest_review_date,updated_at").in("listing_id", ids),
    sb.from("review_ocr_jobs").select("id,listing_id,status,extracted_dates,total_reviews,ocr_error,confirmed_dates,created_at").in("listing_id", ids).order("created_at", { ascending: false })]);
  const ocr = new Map(); (c.data || []).forEach((r) => { if (!ocr.has(Number(r.listing_id))) ocr.set(Number(r.listing_id), r); });
  (a.data || []).forEach((r) => { const id = Number(r.listing_id); st.set(id, r.status); at.set(id, r.updated_at); if (r.error) why.set(id, r.error); });
  (b.data || []).forEach((r) => man.set(Number(r.listing_id), r));
  return { st, man, ocr, at, why };
}

// 产品库评论抓取进度：聚合 listing_scrape_status（pending/fetching/ok/failed）；仅有排队/进行中时显示
export function scrapeProgress(statusMap) {
  let ok = 0, pending = 0, fetching = 0, failed = 0, unavailable = 0;
  if (statusMap && typeof statusMap.values === "function") for (const v of statusMap.values()) {
    if (v === "ok") ok++;
    else if (v === "pending") pending++;
    else if (v === "fetching") fetching++;
    else if (v === "failed") failed++;
    else if (v === "unavailable") unavailable++;
  }
  const busy = pending + fetching, total = ok + pending + fetching + failed;   // 不可售不计入合计（不会再抓）
  return { ok, pending, fetching, failed, unavailable, busy, total, show: busy > 0 && total > 0 };
}
// 会话冷却提示：worker_heartbeat.info.cool_until / cool_remaining_s → 「防拦截冷却中，约 N 分钟后继续」
export function coolHint(cool, now = Date.now()) {
  if (!cool) return "";
  let rem = null;
  if (cool.cool_remaining_s != null && Number.isFinite(Number(cool.cool_remaining_s))) rem = Math.max(0, Number(cool.cool_remaining_s));
  else if (cool.cool_until) {
    const t = new Date(cool.cool_until).getTime();
    if (!isNaN(t)) rem = Math.max(0, Math.round((t - now) / 1000));
  }
  if (rem == null || rem <= 0) return "";
  const mins = Math.max(1, Math.ceil(rem / 60));
  return `防拦截冷却中，约 ${mins} 分钟后继续`;
}
// 「评论抓取 已完成 A / 合计 B（排队 C · 进行中 D · 失败 E）」[+ 冷却提示] + A/B 进度条；无 busy 时返回 ""
export function scrapeProgressBar(p, cool) {
  if (!p?.show || !(p.total > 0)) return "";
  const pct = Math.min(100, Math.round((p.ok / p.total) * 100));
  const hint = coolHint(cool);
  const lab = `评论抓取 已完成 ${p.ok} / 合计 ${p.total}（排队 ${p.pending} · 进行中 ${p.fetching} · 失败 ${p.failed}）` + (hint ? ` · ${hint}` : "");
  const tip = "本人产品库评论抓取进度（listing_scrape_status）；有排队/进行中时约每 30 秒刷新" + (hint ? `；${hint}` : "");
  return `<div class="lrev-prog" title="${tip}">${lab}<div class="lrev-prog-bar" role="progressbar" aria-label="评论抓取进度" aria-valuenow="${p.ok}" aria-valuemin="0" aria-valuemax="${p.total}"><i style="width:${pct}%"></i></div></div>`;
}
