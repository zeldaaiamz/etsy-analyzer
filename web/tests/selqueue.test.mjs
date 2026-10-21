// node --test web/tests/ —— 037 抓取队列
import test from "node:test";
import assert from "node:assert/strict";
import { queueItem, buildQueue, applyAction, canDelete, STALL_S } from "../js/selqueue.js";
const NOW = Date.parse("2026-10-20T08:00:00Z"), ago = (s) => new Date(NOW - s * 1000).toISOString();
const B = (o) => ({ batch_no: 1, offset_start: 1, offset_end: 1000, scraped_count: 0, passed_count: 0, ...o });

test("正在抓取：用进度数据算进度条，可暂停/取消", () => {
  const x = queueItem({ id: "a", status: "running", progress: { target: 1000, read: 468, judged: 431, passed: 149, at: ago(2) } }, B({ status: "running" }), NOW);
  assert.equal(x.group, "running"); assert.equal(Math.floor(x.pctJ), 43); assert.equal(x.read, 468); assert.deepEqual(x.acts, ["pause", "cancel"]);
});
test("卡住（10 分钟没心跳）→ 有问题 + 重新抓取", () => {
  const x = queueItem({ id: "a", status: "running", heartbeat_at: ago(STALL_S + 60) }, B({}), NOW);
  assert.equal(x.group, "problem"); assert.equal(x.label, "卡住"); assert.ok(x.acts.includes("retry")); assert.match(x.reason, /分钟没有更新/);
});
test("等待中按 queue_at 排序，首位不能上移、末位不能下移，暂停的保留位置", () => {
  const runs = [{ id: "c", status: "queued", queue_at: ago(10) }, { id: "a", status: "running", progress: { at: ago(1) } }, { id: "b", status: "paused", queue_at: ago(50) }, { id: "d", status: "queued", queue_at: ago(30) }];
  const q = buildQueue(runs, () => B({}), NOW);
  assert.deepEqual(q.wait.map((x) => x.id), ["b", "d", "c"]); assert.deepEqual(q.wait.map((x) => x.pos), [1, 2, 3]);
  assert.ok(!q.wait[0].acts.includes("up") && q.wait[0].acts.includes("resume")); assert.ok(!q.wait[2].acts.includes("down")); assert.equal(q.running[0].id, "a"); assert.equal(q.offline, false);
});
test("有任务等待但没有在抓的 → box 离线提示", () => {
  assert.equal(buildQueue([{ id: "x", status: "queued", queue_at: ago(400) }], () => B({}), NOW).offline, true);
  assert.equal(buildQueue([{ id: "x", status: "queued", queue_at: ago(30) }], () => B({}), NOW).offline, false);
});
test("失败 → 原因 + 刷新/重新抓取（原地续读）；点了之后排到队尾、等待", () => {
  const r = { id: "f", status: "failed", error: "Remote end closed connection without response", queue_at: ago(9999), created_at: ago(9999) };
  const x = queueItem(r, B({ status: "failed", scraped_count: 286 }), NOW);
  assert.equal(x.group, "problem"); assert.deepEqual(x.acts, ["retry", "delete"]); assert.equal(x.done, 286);
  const r2 = applyAction(r, "retry", NOW); assert.equal(r2.status, "queued"); assert.equal(r2.id, "f"); assert.equal(r2.error, null); assert.equal(r2.queue_at, new Date(NOW).toISOString());
  const q = buildQueue([{ id: "w", status: "queued", queue_at: ago(60) }, r2], () => B({ scraped_count: 286 }), NOW);
  assert.deepEqual(q.wait.map((x) => x.id), ["w", "f"]); assert.equal(q.wait[1].resumeAt, 287); assert.equal(q.problem.length, 0);
  assert.deepEqual(queueItem({ id: "c", status: "failed", error: "count" }, null, NOW).acts, ["delete"]); // 查询失败没有批次：只能删 / 重新查询
});
test("暂停 / 继续：继续回到原位置", () => {
  const p = applyAction({ id: "a", status: "queued", queue_at: ago(100) }, "pause", NOW); assert.equal(p.status, "paused");
  const q = applyAction(p, "resume", NOW); assert.equal(q.status, "queued"); assert.equal(q.queue_at, ago(100));
  assert.equal(applyAction({ status: "succeeded" }, "pause", NOW).status, "succeeded");
  assert.equal(applyAction({ status: "running", progress: { at: ago(5) } }, "retry", NOW).status, "running"); // 正常抓取中不能“重新抓取”
});
test("删除：进行中/排队/暂停的不能删，结束的可以", () => {
  for (const s of ["queued", "running", "paused", "counting"]) assert.equal(canDelete({ status: s }), false, s);
  for (const s of ["succeeded", "batch_done", "failed", "canceled", "counted"]) assert.equal(canDelete({ status: s }), true, s);
  assert.ok(queueItem({ id: "e", status: "succeeded" }, B({ status: "done" }), NOW).acts.includes("delete"));
});

test("page retry labels (timeout vs paging/keyword)", () => {
  const mk = (why) => queueItem({ id: "r", status: "running", progress: { stage: "page_retry", retry: 1, retry_max: 2, retry_why: why, page: 44, at: ago(2) } }, null, NOW).label;
  assert.equal(mk("超时"), "单页超时重试中"); assert.equal(mk(undefined), "单页超时重试中");
  assert.equal(mk("翻页卡住"), "页面重试中"); assert.equal(mk("关键词未生效"), "页面重试中");
  assert.equal(queueItem({ id: "r", status: "running", progress: { stage: "reading", retry: 0, at: ago(2) } }, null, NOW).label, "抓取中");
});
