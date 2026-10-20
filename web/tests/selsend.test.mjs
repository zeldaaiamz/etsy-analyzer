// node --test web/tests/ —— 送去预筛选修复（039）
import test from "node:test";
import assert from "node:assert/strict";
import { sendState, sentInfo, sendPlan } from "../js/selscreen.js";
const AT = "2026-10-20T01:00:00Z";
test("本批发送状态：未发送 / 全部 / 部分 / 038 旧标记", () => {
  assert.deepEqual(sendState({ passed_count: 10 }), { sent: 0, total: 10, full: false, partial: false, left: 10, at: null });
  const p = sendState({ sent_to_screen_at: AT, sent_count: 4 }, 10); assert.ok(p.partial && !p.full); assert.equal(p.left, 6);
  assert.ok(sendState({ sent_to_screen_at: AT, sent_count: 10 }, 10).full);
  assert.ok(sendState({ sent_to_screen_at: AT, sent_count: null }, 10).full); // 038 时代：整批已发送
});
test("历史列表：部分发送显示「第N批已发送（部分 x/y）」", () => {
  assert.equal(sentInfo([{ batch_no: 2, sent_to_screen_at: AT, sent_count: 200, passed_count: 352 }]).text, "第二批已发送（部分 200/352）");
  assert.equal(sentInfo([{ batch_no: 2, sent_to_screen_at: AT, sent_count: 352, passed_count: 352 }]).text, "第二批已发送");
});
test("确认计划：单批已发送 → 明确重发；部分 → 发剩余；发送全部跳过已发送", () => {
  const L = { a: Array(352).fill(0).map((_, i) => `u${i}`), b: Array(341).fill(0).map((_, i) => `v${i}`), c: ["w"] };
  const A = { id: "a", batch_no: 1, sent_to_screen_at: AT }, B = { id: "b", batch_no: 2 }, C = { id: "c", batch_no: 3, sent_to_screen_at: AT, sent_count: 0 };
  const lo = (id) => L[id];
  assert.deepEqual((({ ids, resend, n }) => ({ ids, resend, n }))(sendPlan([A], lo, true)), { ids: ["a"], resend: true, n: 352 });
  assert.deepEqual((({ ids, resend, n }) => ({ ids, resend, n }))(sendPlan([{ ...A, sent_count: 200 }], lo, true)), { ids: ["a"], resend: false, n: 152 });
  const all = sendPlan([A, B, C], lo, false); assert.deepEqual(all.ids, ["b", "c"]); assert.equal(all.n, 342); assert.deepEqual(all.skipped.map((x) => x.b.id), ["a"]); assert.equal(all.resend, false);
  assert.equal(sendPlan([], lo, true), null);
});
import { linkSentFlags } from "../js/selscreen.js";
test("每条链接已发送标记：只有实际发送（含去重算已发送）的前 sent_count 条", () => {
  const AT2 = "2026-10-20T01:00:00Z";
  assert.deepEqual(linkSentFlags({}, 3), [false, false, false]);
  assert.deepEqual(linkSentFlags({ sent_to_screen_at: AT2, sent_count: 2 }, 5), [true, true, false, false, false]); // 部分发送
  assert.deepEqual(linkSentFlags({ sent_to_screen_at: AT2, sent_count: null }, 2), [true, true]); // 038 旧标记 = 整批
  assert.deepEqual(linkSentFlags({ sent_to_screen_at: AT2, sent_count: 9 }, 2), [true, true]);
});
import { sentInfo as sentInfo2, historySendNos as hsn, linkSentFlags as lsf } from "../js/selscreen.js";
test("044 自动发送：历史标签「已自动发送」；部分自动发送后的勾选仍按 sent_count", () => {
  const runs = [{ id: "r", keyword: "Gift", sel_batches: [{ batch_no: 1, sent_to_screen_at: "2026-10-20T01:00:00Z", sent_count: 32, passed_count: 32, auto_sent_at: "2026-10-20T01:00:00Z" }] }];
  assert.equal(sentInfo2(runs[0].sel_batches, "Gift", hsn(runs), "r").text, "Gift 第一批 · 已自动发送");
  const b = { sent_to_screen_at: "2026-10-20T01:00:00Z", sent_count: 120, auto_sent_at: "2026-10-20T01:00:00Z", auto_pending: true };
  const f = lsf(b, 341); assert.equal(f.filter(Boolean).length, 120); assert.ok(f[119] && !f[120]);
  assert.equal(lsf({ ...b, sent_count: 341, auto_pending: false }, 341).every(Boolean), true); // 补发完成后全部勾选
});
