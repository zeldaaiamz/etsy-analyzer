// node --test web/tests/ —— 预筛选批次命名「<关键词> 第N批」
import test from "node:test";
import assert from "node:assert/strict";
import { screenBatchGroups, historySendNos, parseScreenName } from "../js/batchname.js";
import { sentInfo } from "../js/selscreen.js";
test("解析 039 批次名", () => { assert.deepEqual(parseScreenName("获取链接 Alex kitchen 第2批 10-20 09:15"), { keyword: "Alex kitchen", selNo: 2, at: "10-20 09:15" }); assert.equal(parseScreenName("重新分析 #1"), null); });
test("预筛选列表：按关键词分组、按发送顺序编号、拆分的算同一批", () => {
  const B = [["reason", 1, "01", "10-19 09:00"], ["Alex kitchen", 1, "02", "10-19 10:00"], ["reason", 2, "03", "10-19 11:00"], ["Alex kitchen", 2, "04", "10-19 12:00"], ["Alex kitchen", 2, "05", "10-19 12:00"]]
    .map(([k, n, t, at], i) => ({ id: `b${i}`, name: `获取链接 ${k} 第${n}批 ${at}`, created_at: `2026-10-19T${t}:00Z` }));
  const G = screenBatchGroups([...B, { id: "x", name: "2026-10-19T00:00:00Z", created_at: "2026-10-19T00:00:00Z" }]);
  assert.deepEqual(G.map((g) => [g.keyword, g.items.map((x) => x.label)]), [["Alex kitchen", ["Alex kitchen 第一批", "Alex kitchen 第二批（1/2）", "Alex kitchen 第二批（2/2）"]], ["reason", ["reason 第一批", "reason 第二批"]]]);
});
test("历史标签：同一关键词跨查找按发送顺序编号", () => {
  const runs = [{ id: "r2", keyword: "Reason", sel_batches: [{ batch_no: 1, sent_to_screen_at: "2026-10-20T03:00:00Z" }] }, { id: "r1", keyword: "reason", sel_batches: [{ batch_no: 1, sent_to_screen_at: "2026-10-19T03:00:00Z" }, { batch_no: 2 }] }];
  const nos = historySendNos(runs);
  assert.equal(sentInfo(runs[0].sel_batches, "Reason", nos, "r2").text, "Reason 第二批");
  assert.equal(sentInfo(runs[1].sel_batches, "reason", nos, "r1").text, "reason 第一批");
  assert.equal(sentInfo([], "reason", nos, "r1").text, "未发送");
});
import { itemSrc, batchCounts } from "../js/batchname.js";
test("条目来源与各批数量", () => {
  const B = [["reason DEP", 1, "01"], ["reason DEP", 2, "02"]].map(([k, n, t], i) => ({ id: `b${i}`, name: `获取链接 ${k} 第${n}批 10-19 ${t}:00`, created_at: `2026-10-19T${t}:00:00Z` }));
  assert.equal(itemSrc({ keyword: "reason DEP", n: 1, label: "reason DEP 第一批" }), "reason DEP");
  assert.equal(itemSrc({ keyword: "reason DEP", n: 2, label: "reason DEP 第二批" }), "reason DEP + reason DEP 第二批");
  const items = [...Array(12).fill({ batch_id: "b0" }), ...Array(8).fill({ batch_id: "b1" })];
  assert.equal(batchCounts(items, B), "reason DEP 第一批 12个，reason DEP 第二批 8个");
});
import { batchCountRows } from "../js/batchname.js";
test("全选本批：按标签汇总 batch_id", () => {
  const B = [{ id: "a", name: "获取链接 reason 第1批 10-19 01:00", created_at: "2026-10-19T01:00:00Z" }, { id: "b", name: "获取链接 reason 第2批 10-19 02:00", created_at: "2026-10-19T02:00:00Z" }, { id: "c", name: "获取链接 reason 第2批 10-19 02:00", created_at: "2026-10-19T02:00:01Z" }];
  assert.deepEqual(batchCountRows([{ batch_id: "a" }, { batch_id: "b" }, { batch_id: "c" }], B), [{ label: "reason 第一批", ids: ["a"], n: 1 }, { label: "reason 第二批", ids: ["b", "c"], n: 2 }]);
});
import { deletable, deletePlan } from "../js/batchname.js";
test("删除所选：可选范围与确认框各批数量", () => {
  assert.ok(deletable({ status: "screening" }) && deletable({ status: "failed" }) && deletable({ status: "canceled", screen_state: "rejected" }));
  assert.ok(!deletable({ status: "queued" }) && !deletable({ status: "running" }) && !deletable({ status: "succeeded", screen_state: "kept" }));
  const B = [{ id: "a", name: "获取链接 reason 第1批 10-19 01:00", created_at: "2026-10-19T01:00:00Z" }, { id: "b", name: "获取链接 Alex kitchen 第1批 10-19 02:00", created_at: "2026-10-19T02:00:00Z" }];
  const I = [{ id: 1, batch_id: "a", status: "screening" }, { id: 2, batch_id: "a", status: "failed" }, { id: 3, batch_id: "b", status: "screening" }, { id: 4, batch_id: "b", status: "running" }, { id: 5, batch_id: "z", status: "screening" }];
  const p = deletePlan([1, 2, 3, 4, 5], I, B);
  assert.equal(p.n, 4); assert.deepEqual(p.ids, [1, 2, 3, 5]);
  assert.deepEqual(p.rows, [{ label: "Alex kitchen 第一批", n: 1 }, { label: "reason 第一批", n: 2 }, { label: "其他", n: 1 }]);
});
import { sourceLabel, batchIndex as bIdx } from "../js/batchname.js";
test("来源标签严格按 jobs.batch_id：与标题文字 / 位置无关", () => {
  const B = [["reason", 1, "01"], ["Alex kitchen", 1, "02"], ["reason", 2, "03"], ["Alex kitchen", 2, "04"], ["reason", 3, "05"]]
    .map(([k, n, t], i) => ({ id: `pb${i}`, name: `获取链接 ${k} 第${n}批 10-19 ${t}:00`, created_at: `2026-10-19T${t}:00:00Z` }));
  const expect = { pb0: "reason", pb1: "Alex kitchen", pb2: "reason + reason 第二批", pb3: "Alex kitchen + Alex kitchen 第二批", pb4: "reason + reason 第三批" };
  // 1–50 区域：50 条交错来自 5 个批次，标题故意写成别的关键词
  const ids = Object.keys(expect), items = Array.from({ length: 50 }, (_, i) => ({ id: `j${i}`, batch_id: ids[(i * 7) % 5], title: i % 2 ? "reason gift" : "Alex kitchen sign" }));
  const idx = bIdx(B); for (const it of items) assert.equal(sourceLabel(it, idx), expect[it.batch_id], `${it.id} → ${it.batch_id}`);
  // 同一 listing 两个任务（重复 / 重发）：各自按建任务的批次显示
  const dup = [{ id: "d1", batch_id: "pb0", listing_id: 1 }, { id: "d2", batch_id: "pb2", listing_id: 1 }];
  assert.deepEqual(dup.map((it) => sourceLabel(it, idx)), ["reason", "reason + reason 第二批"]);
  assert.equal(sourceLabel({ batch_id: "manual" }, idx), "其他来源");
  assert.equal(sourceLabel({ batch_id: "pb0" }, null), ""); // 批次列表未加载：不猜
});
import { groupItems, groupCount } from "../js/batchname.js";
test("按实际发送批次分组：Gift 第一批 恰好 32 条，组数量 = 该批任务", () => {
  const B = [{ id: "r1", name: "获取链接 reason 第1批 10-19 01:00", created_at: "2026-10-19T01:00:00Z", jobs_count: 10 },
    { id: "g1a", name: "获取链接 Gift 第1批 10-19 05:00", created_at: "2026-10-19T05:00:00Z", jobs_count: 20 }, { id: "g1b", name: "获取链接 Gift 第1批 10-19 05:00", created_at: "2026-10-19T05:00:01Z", jobs_count: 12 },
    { id: "r2", name: "获取链接 reason 第2批 10-19 03:00", created_at: "2026-10-19T03:00:00Z", jobs_count: 6 }, { id: "m", name: "2026-10-19T00:00:00Z", created_at: "2026-10-19T00:00:00Z" }];
  // 交错顺序 + 标题故意写别的关键词：分组只看 batch_id
  const mk = (bid, n) => Array.from({ length: n }, (_, i) => ({ id: `${bid}-${i}`, batch_id: bid, title: "reason gift" }));
  const all = [...mk("g1a", 20), ...mk("r1", 8), ...mk("g1b", 12), ...mk("r2", 6), ...mk("m", 2), { id: "x", batch_id: "unknown" }].sort(() => 0.5 - Math.random());
  const G = groupItems(all, B);
  assert.deepEqual(G.map((g) => [g.label, g.items.length, groupCount(g)]), [["Gift 第一批", 32, "32个"], ["reason 第一批", 8, "8/10个"], ["reason 第二批", 6, "6个"], ["其他来源", 3, "3个"]]);
  for (const g of G.slice(0, 3)) for (const it of g.items) assert.ok(g.ids.includes(it.batch_id));
  assert.equal(G.reduce((a, g) => a + g.items.length, 0), all.length); // 不多不少
  assert.equal(groupCount({ items: [1, 2], sent: null }), "2个"); // 拿不到发送数：只显示列表数
});
