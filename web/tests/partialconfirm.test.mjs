// node --test web/tests/ —— 预筛选：同批有排队/取数中的条目时，已取到的可先确认（批次 caa8d6b3 场景：143 已取到 + 30 排队）
import test from "node:test";
import assert from "node:assert/strict";
import { confirmableItems } from "../js/screen.js";
const mk = (n, status, extra = {}) => Array.from({ length: n }, (_, i) => ({ id: `${status}-${i}`, status, screen_state: status === "screening" ? "awaiting" : null, image_url: "x", ...extra }));
test("143 已取到 + 30 排队：可确认 143 条，排队的不在内", () => {
  const b = [...mk(143, "screening"), ...mk(30, "queued")];
  const c = confirmableItems(b, () => true);
  assert.equal(c.length, 143); assert.ok(c.every((it) => it.status === "screening"));
});
test("图片未加载的不算可确认", () => {
  const b = mk(5, "screening"); const ok = new Set(["screening-0", "screening-1"]);
  assert.equal(confirmableItems(b, (it) => ok.has(it.id)).length, 2);
});
test("确认后不重复：已确认(kept)的不再出现；剩余取到后再确认", () => {
  const b = [...mk(143, "screening"), ...mk(30, "queued")];
  confirmableItems(b, () => true).forEach((it) => { it.status = "queued"; it.screen_state = "kept"; });
  assert.equal(confirmableItems(b, () => true).length, 0);
  b.filter((it) => it.screen_state !== "kept").forEach((it) => { it.status = "screening"; it.screen_state = "awaiting"; });
  const c2 = confirmableItems(b, () => true); assert.equal(c2.length, 30); assert.ok(c2.every((it) => it.id.startsWith("queued-")));
});
test("running / failed / rejected 不可确认", () => {
  const b = [...mk(2, "running"), ...mk(2, "failed"), { id: "r", status: "canceled", screen_state: "rejected" }];
  assert.equal(confirmableItems(b, () => true).length, 0);
});
