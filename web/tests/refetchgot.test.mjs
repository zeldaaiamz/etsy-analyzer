// node --test web/tests/ —— 053 预筛选选中后「重新获取」已获取成功项
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { refetchableGot, refetchPlan, deletable } from "../js/batchname.js";

const mk = (id, status, extra = {}) => ({ id, batch_id: "b1", status, screen_state: status === "screening" ? "awaiting" : null, listing_id: 1, ...extra });

test("refetchableGot：仅 screening 且非 kept", () => {
  assert.equal(refetchableGot(mk("a", "screening")), true);
  assert.equal(refetchableGot(mk("b", "screening", { screen_state: "awaiting" })), true);
  assert.equal(refetchableGot(mk("c", "screening", { screen_state: "kept" })), false);
  assert.equal(refetchableGot(mk("d", "failed")), false);
  assert.equal(refetchableGot(mk("e", "queued")), false);
  assert.equal(refetchableGot(mk("f", "running")), false);
  assert.equal(refetchableGot(mk("g", "canceled", { screen_state: "rejected" })), false);
  assert.equal(refetchableGot(null), false);
});

test("refetchPlan：只计已获取成功的所选；按批汇总", () => {
  const items = [
    mk("1", "screening"),
    mk("2", "screening"),
    mk("3", "failed"),
    mk("4", "queued"),
    mk("5", "screening", { screen_state: "kept", batch_id: "b2" }),
    { id: "6", batch_id: "b2", status: "screening", screen_state: "awaiting" },
  ];
  const batches = [
    { id: "b1", name: "获取链接 Gift 第1批 10-23 11:00" },
    { id: "b2", name: "获取链接 Gift 第1批 10-23 11:00" },
  ];
  const plan = refetchPlan(["1", "2", "3", "4", "5", "6"], items, batches);
  assert.equal(plan.n, 3);
  assert.deepEqual(plan.ids.sort(), ["1", "2", "6"]);
  assert.ok(plan.rows.some((r) => r.n === 2) || plan.rows.some((r) => r.n === 3));
});

test("deletable 仍含 failed；与 refetchableGot 不混用", () => {
  assert.equal(deletable(mk("f", "failed")), true);
  assert.equal(refetchableGot(mk("f", "failed")), false);
  assert.equal(deletable(mk("s", "screening")), true);
  assert.equal(refetchableGot(mk("s", "screening")), true);
});

test("screen.js：选中栏有重新获取；RPC screen_refetch_got；无一键全量重取已获取", () => {
  const src = readFileSync(new URL("../js/screen.js", import.meta.url), "utf8");
  assert.match(src, /data-sselact="refetch"/);
  assert.match(src, /重新获取/);
  assert.match(src, /screen_refetch_got/);
  assert.match(src, /async function refetchGotSelected/);
  assert.match(src, /refetchPlan/);
  // 一键刷新仍只针对失败
  assert.match(src, /一键刷新（\$\{nRetry\} 条失败）/);
  assert.match(src, /screen_refetch_all/);
  // 不提供静默/一键全部重取已获取
  assert.doesNotMatch(src, /一键重新获取全部/);
  assert.doesNotMatch(src, /screen_refetch_got_all/);
  // 提示改为需选中
  assert.match(src, /已获取的需选中后「重新获取」/);
});
