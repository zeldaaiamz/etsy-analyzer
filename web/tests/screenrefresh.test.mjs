// node --test web/tests/ —— 044 送去预筛选后看不到
import test from "node:test";
import assert from "node:assert/strict";
import { sendOutcome } from "../js/selscreen.js";
test("生产场景（044 前）：sent=0、21 条复用但被删除 → 明确提示未恢复，不再静默", () => {
  const o = sendOutcome({ sent: 0, dup: 2, reused: 21 }); assert.match(o.text, /21 条已获取过但在预筛选里删除过，未恢复/); assert.match(o.text, /2 条已在预筛选列表里/);
});
test("044 后：恢复 21 条 → 跳到预筛选", () => {
  const o = sendOutcome({ sent: 0, dup: 2, reused: 21, restored: 21 }); assert.equal(o.go, "#/screen"); assert.equal(o.kind, "ok"); assert.match(o.text, /21 条之前在预筛选里删除过，已恢复显示/); assert.doesNotMatch(o.text, /未恢复/);
});
test("有新建 → 跳到新批次；都已发送 → 不跳转", () => {
  assert.equal(sendOutcome({ sent: 5, first_screen_batch: "abc", screen_batches: 1 }).go, "#/screen/abc");
  const n = sendOutcome({ sent: 0 }); assert.equal(n.go, null); assert.equal(n.kind, "bad");
  assert.match(sendOutcome({ sent: 3, limited: true, screen_batches: 1 }, 7).text, /还有 7 条/);
});
