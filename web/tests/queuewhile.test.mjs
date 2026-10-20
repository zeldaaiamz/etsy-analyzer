// node --test web/tests/ —— 进行中也能查询新词（048）
import test from "node:test";
import assert from "node:assert/strict";
import { countDisabled } from "../js/selscreen.js";
test("查询数量：任务进行中不禁用，只在提交中禁用", () => {
  for (const status of ["count_queued", "counting", "queued", "running", "paused", "succeeded"]) assert.equal(countDisabled({ status }, false), false, status);
  assert.equal(countDisabled(null, false), false); assert.equal(countDisabled({ status: "running" }, true), true);
});
