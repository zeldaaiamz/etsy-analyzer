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
