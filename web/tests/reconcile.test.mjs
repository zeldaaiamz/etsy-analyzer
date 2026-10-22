// node --test web/tests/ —— 051 对账：绿/红判定、原因文案、全账号一行、排序键
import test from "node:test";
import assert from "node:assert/strict";
import { reconTotals, batchGap, reasonText, taskTotalLine, reconPanelHtml, sortAt } from "../js/reconcile.js";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const book = { batch_id: "b", name: "获取链接 book", confirmed: 44, in_tasks: 44, in_library: 0, failed: 0, missing: 0, deleted: 136, items: [{ id: "j1", listing_id: 1, reason: "task", task_row: 31 }], deleted_items: [{ id: "d1", listing_id: 9 }] };
test("N = M + L → 绿；服务端 missing 或差额 → 红缺 K", () => {
  assert.equal(batchGap(book), 0);
  assert.equal(batchGap({ confirmed: 4, in_tasks: 1, in_library: 3 }), 0);
  assert.equal(batchGap({ confirmed: 3, in_tasks: 2, in_library: 0, missing: 1 }), 1);
  assert.equal(batchGap({ confirmed: 5, in_tasks: 2, in_library: 1, missing: 0 }), 2);   // 服务端漏算也会红
});
test("原因文案带行号；产品库/缺失", () => {
  assert.equal(reasonText({ reason: "task", task_row: 31 }), "已进任务（第 31 行）");
  assert.equal(reasonText({ reason: "failed", task_row: 50 }), "已进任务 · 分析失败（第 50 行）");
  assert.equal(reasonText({ reason: "library" }), "在产品库"); assert.equal(reasonText({ reason: "missing" }), "缺失");
});
test("生产真实数字：104 = 67 + 37 → 缺 0 绿", () => {
  const h = taskTotalLine({ total: 79, shown: 30, libCount: 51, rec: { confirmed: 104, in_tasks: 67, in_library: 37, missing: 0 } });
  assert.match(h, /共 <b>79<\/b> 个任务 · 已显示 30/); assert.match(h, /加载更多/); assert.match(h, /产品库/); assert.match(h, /recon-ok/); assert.match(h, /缺 0/);
  assert.match(taskTotalLine({ total: 5, shown: 5, rec: { confirmed: 3, in_tasks: 1, in_library: 1, missing: 0 } }), /recon-bad[\s\S]*缺 1/);
});
test("面板：展开列出原因与「去任务」；已删除页签可撤销", () => {
  const closed = reconPanelHtml([book], esc); assert.match(closed, /确认 <b>44<\/b> · 已进任务 <b>44<\/b> · 产品库 <b>0<\/b>/); assert.match(closed, /✓ 一致/); assert.doesNotMatch(closed, /去任务/);
  const open = reconPanelHtml([book], esc, new Set(["b"])); assert.match(open, /第 31 行/); assert.match(open, /data-recon-go="j1"/);
  const del = reconPanelHtml([book], esc, new Set(["b"]), new Map([["b", "deleted"]])); assert.match(del, /data-recon-unhide="d1"/);
  assert.equal(reconTotals([book, book]).confirmed, 88);
});
test("排序键：确认时间优先", () => {
  assert.equal(sortAt({ created_at: "2026-10-21", screened_at: "2026-10-22" }), "2026-10-22");
  assert.equal(sortAt({ created_at: "2026-10-21" }), "2026-10-21");
});
