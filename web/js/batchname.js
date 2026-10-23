// 送去预筛选批次命名：「<搜索关键词> 第N批」（N 按同一关键词的发送顺序，中文数字），列表按关键词分组。
// 纯前端：从 039 写入的批次名「获取链接 <kw> 第<sel批号>批 MM-DD HH:MI」解析关键词，不改库（无需迁移）。
export const cnNum = (n) => { const d = "零一二三四五六七八九"; n = Number(n) || 0; if (n < 10) return d[n]; if (n < 20) return "十" + (n % 10 ? d[n % 10] : ""); if (n < 100) return d[Math.floor(n / 10)] + "十" + (n % 10 ? d[n % 10] : ""); return String(n); };
const kwKey = (k) => String(k || "").trim().replace(/\s+/g, " ").toLowerCase();
export const batchLabel = (kw, n) => `${String(kw).trim()} 第${cnNum(n)}批`;
const RE = /^获取链接 (.+) 第(\d+)批(?: (\d{2}-\d{2} \d{2}:\d{2}))?$/;
export function parseScreenName(name) { const m = String(name || "").match(RE); return m ? { keyword: m[1], selNo: Number(m[2]), at: m[3] || "" } : null; }
// 预筛选批次列表：一次发送里同一获取链接批次被拆成多个（每个 ≤500 条）的算同一批，显示「（1/2）」
export function screenBatchGroups(batches) {
  const rows = (batches || []).map((b) => ({ b, p: parseScreenName(b.name) })).filter((x) => x.p)
    .sort((x, y) => String(x.b.created_at).localeCompare(String(y.b.created_at)) || String(x.b.id).localeCompare(String(y.b.id)));
  const G = new Map();
  for (const { b, p } of rows) {
    const k = kwKey(p.keyword); let g = G.get(k); if (!g) G.set(k, (g = { key: k, keyword: p.keyword.trim(), sends: [], last: "" }));
    const sk = `${p.selNo}|${p.at}`, prev = g.sends[g.sends.length - 1];
    if (prev && prev.sk === sk && p.at) prev.parts.push(b); else g.sends.push({ sk, n: g.sends.length + 1, parts: [b] });
    g.last = String(b.created_at);
  }
  return [...G.values()].sort((a, b) => b.last.localeCompare(a.last)).map((g) => ({ keyword: g.keyword,
    items: g.sends.flatMap((s) => s.parts.map((b, i) => ({ id: b.id, n: s.n, label: batchLabel(g.keyword, s.n) + (s.parts.length > 1 ? `（${i + 1}/${s.parts.length}）` : ""), created_at: b.created_at, demo: b.demo }))) }));
}
// 获取链接历史：每个已发送的 sel_batch 按同一关键词的发送时间编号 → Map(`${runId}:${batch_no}` → N)
export function historySendNos(runs) {
  const per = new Map(), out = new Map();
  for (const r of runs || []) for (const b of r.sel_batches || []) if (b.sent_to_screen_at) { const k = kwKey(r.keyword); if (!per.has(k)) per.set(k, []); per.get(k).push({ id: `${r.id}:${b.batch_no}`, at: Date.parse(b.sent_to_screen_at) || 0 }); }
  for (const L of per.values()) L.sort((a, b) => a.at - b.at).forEach((x, i) => out.set(x.id, i + 1));
  return out;
}
// batch_id → { keyword, n, label }（与批次列表同一套按关键词编号）
export function batchIndex(batches) { const M = new Map(); for (const g of screenBatchGroups(batches)) for (const it of g.items) M.set(it.id, { keyword: g.keyword, n: it.n, label: batchLabel(g.keyword, it.n) }); return M; }
// 条目框里的来源：第一批只显示关键词；第二批起「kw + kw 第N批」
export const itemSrc = (x) => (!x ? "" : x.n === 1 ? x.keyword : `${x.keyword} + ${x.label}`);
// 当前视图各批数量：「reason 第一批 12个，reason 第二批 8个」（按批次列表顺序；未知来源归「其他」）
export function batchCounts(items, batches) {
  const M = batchIndex(batches), order = [...M.keys()], c = new Map(); let other = 0;
  for (const it of items || []) { if (M.has(it.batch_id)) { const k = M.get(it.batch_id).label; c.set(k, (c.get(k) || 0) + 1); } else other++; }
  const seen = new Set(), out = [];
  for (const id of order) { const l = M.get(id).label; if (c.has(l) && !seen.has(l)) { seen.add(l); out.push(`${l} ${c.get(l)}个`); } }
  if (other && out.length) out.push(`其他 ${other}个`);
  return out.join("，");
}
// 同 batchCounts，但返回结构（含 batch_id 列表），用于「全选本批」
export function batchCountRows(items, batches) {
  const M = batchIndex(batches), R = new Map();
  for (const id of M.keys()) { const l = M.get(id).label; if (!R.has(l)) R.set(l, { label: l, ids: [], n: 0 }); R.get(l).ids.push(id); }
  for (const it of items || []) { const x = M.get(it.batch_id); if (x) R.get(x.label).n++; }
  return [...R.values()].filter((r) => r.n);
}
// 「删除所选」可选范围（与 040 screen_hide_ids 一致）：待确认、取数失败、已确认排除；取数中（queued/running）与已进入分析的不可选
export const deletable = (it) => !!it && it.screen_state !== "kept" && ["screening", "failed", "canceled"].includes(it.status);
// 「重新获取」可选范围（053 screen_refetch_got）：仅已获取成功、仍待确认（screening，非 kept）；失败走一键刷新；取数中/已进入分析不可选
export const refetchableGot = (it) => !!it && it.status === "screening" && it.screen_state !== "kept";
// 确认框内容：总数 + 各批数量（按批次列表顺序，未知来源归「其他」）
export function deletePlan(selIds, items, batches) {
  const S = new Set(selIds), L = (items || []).filter((it) => S.has(it.id) && deletable(it));
  return { ids: L.map((it) => it.id), n: L.length, rows: batchCountRows(L, batches).map((r) => ({ label: r.label, n: r.n })).concat((() => { const M = batchIndex(batches), k = L.filter((it) => !M.has(it.batch_id)).length; return k ? [{ label: "其他", n: k }] : []; })()) };
}
// 「重新获取」确认框：只计已获取成功（screening）的所选；失败/取数中/kept 不计
export function refetchPlan(selIds, items, batches) {
  const S = new Set(selIds), L = (items || []).filter((it) => S.has(it.id) && refetchableGot(it));
  return { ids: L.map((it) => it.id), n: L.length, rows: batchCountRows(L, batches).map((r) => ({ label: r.label, n: r.n })).concat((() => { const M = batchIndex(batches), k = L.filter((it) => !M.has(it.batch_id)).length; return k ? [{ label: "其他", n: k }] : []; })()) };
}
// 条目来源标签：只按该任务自己的 jobs.batch_id（建任务的批次，之后从不改写）查批次列表，不看标题/关键词文字/位置。
// 批次列表还没加载 → 空（不猜）；批次不是「获取链接」建的（任务页手动粘贴、重新分析）→「其他来源」
export function sourceLabel(item, idx) { if (!idx || !item?.batch_id) return ""; const x = idx.get(item.batch_id); return x ? itemSrc(x) : "其他来源"; }
// 预筛选页按实际发送批次分组（jobs.batch_id）：一个关键词的一次发送 = 一组（≤500 拆分的多个批次合为一组）。
// 组顺序 = 批次列表顺序（最近发送的关键词在前，组内按发送顺序）；非获取链接批次 / 未知批次 →「其他来源」放最后。
// sent = 发送时建的任务数（batches.jobs(count)，包括已进入分析 / 已隐藏的）；拿不到时为 null
export function groupItems(items, batches) {
  const out = [], byLabel = new Map(), bid2g = new Map(), cnt = new Map((batches || []).map((b) => [b.id, b.jobs_count]));
  for (const g of screenBatchGroups(batches)) for (const it of g.items) {
    const label = batchLabel(g.keyword, it.n); let G = byLabel.get(label);
    if (!G) byLabel.set(label, (G = { key: label, label, keyword: g.keyword, ids: [], items: [], sent: 0 })), out.push(G);
    G.ids.push(it.id); bid2g.set(it.id, G); const c = cnt.get(it.id); G.sent = G.sent == null || c == null ? null : G.sent + Number(c);
  }
  const other = { key: "其他来源", label: "其他来源", keyword: "", ids: [], items: [], sent: null };
  for (const it of items || []) (bid2g.get(it.batch_id) || other).items.push(it);
  for (const it of other.items) if (it.batch_id && !other.ids.includes(it.batch_id)) other.ids.push(it.batch_id);
  return out.filter((G) => G.items.length).concat(other.items.length ? [other] : []);
}
// 组标题数量：列表中 x 条；与发送数 y 不同时显示「x/y」（差额 = 已进入分析 / 已删除隐藏 / 去重等不在列表里的）
export function groupCount(G) { const x = G.items.length; return G.sent == null || G.sent === x ? `${x}个` : `${x}/${G.sent}个`; }
