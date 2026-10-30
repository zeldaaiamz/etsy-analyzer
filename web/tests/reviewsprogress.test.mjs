// node --test web/tests/ —— 产品库评论抓取进度条（listing_scrape_status 客户端聚合）
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { scrapeProgress, scrapeProgressBar, coolHint, SCRAPE_BUSY } from "../js/velocity.js";

test("scrapeProgress：按 status 计数；show 仅当有 pending/fetching", () => {
  const m = new Map([[1, "ok"], [2, "ok"], [3, "pending"], [4, "fetching"], [5, "failed"], [6, "failed"], [7, "weird"]]);
  const p = scrapeProgress(m);
  assert.deepEqual({ ok: p.ok, pending: p.pending, fetching: p.fetching, failed: p.failed, busy: p.busy, total: p.total, show: p.show },
    { ok: 2, pending: 1, fetching: 1, failed: 2, busy: 2, total: 6, show: true });
  assert.equal(scrapeProgress(new Map([[1, "ok"], [2, "failed"]])).show, false);
  assert.equal(scrapeProgress(new Map()).show, false);
  assert.equal(scrapeProgress(null).show, false);
});

test("scrapeProgressBar：文案 + A/B 进度；无 busy 时空串", () => {
  const p = scrapeProgress(new Map([[1, "ok"], [2, "ok"], [3, "pending"], [4, "fetching"], [5, "failed"]]));
  // 已完成 2 / 合计 5（排队 1 · 进行中 1 · 失败 1）→ 40%
  const h = scrapeProgressBar(p);
  assert.match(h, /评论抓取 已完成 2 \/ 合计 5（排队 1 · 进行中 1 · 失败 1）/);
  assert.match(h, /class="lrev-prog"/);
  assert.match(h, /role="progressbar"/);
  assert.match(h, /aria-valuenow="2"/);
  assert.match(h, /aria-valuemax="5"/);
  assert.match(h, /width:40%/);
  assert.equal(scrapeProgressBar(scrapeProgress(new Map([[1, "ok"]]))), "");
  assert.equal(scrapeProgressBar(null), "");
});

test("演示数字 12/30：revprog 垫数", () => {
  const m = new Map();
  for (let i = 0; i < 12; i++) m.set(9100000000 + i, "ok");
  for (let i = 0; i < 10; i++) m.set(9100001000 + i, "pending");
  for (let i = 0; i < 3; i++) m.set(9100002000 + i, "fetching");
  for (let i = 0; i < 5; i++) m.set(9100003000 + i, "failed");
  const p = scrapeProgress(m);
  assert.equal(p.ok, 12); assert.equal(p.pending, 10); assert.equal(p.fetching, 3); assert.equal(p.failed, 5);
  assert.equal(p.total, 30); assert.equal(p.busy, 13); assert.ok(p.show);
  assert.match(scrapeProgressBar(p), /已完成 12 \/ 合计 30（排队 10 · 进行中 3 · 失败 5）/);
  assert.match(scrapeProgressBar(p), /width:40%/); // 12/30
});

test("coolHint：remaining_s / cool_until → 约 N 分钟；过期空串", () => {
  assert.equal(coolHint(null), "");
  assert.equal(coolHint({ cool_remaining_s: 0 }), "");
  assert.equal(coolHint({ cool_remaining_s: 23 * 60 }), "防拦截冷却中，约 23 分钟后继续");
  assert.equal(coolHint({ cool_remaining_s: 90 }), "防拦截冷却中，约 2 分钟后继续"); // ceil
  assert.equal(coolHint({ cool_remaining_s: 30 }), "防拦截冷却中，约 1 分钟后继续");
  const now = Date.parse("2026-10-23T07:00:00Z");
  assert.equal(coolHint({ cool_until: "2026-10-23T07:23:00Z" }, now), "防拦截冷却中，约 23 分钟后继续");
  assert.equal(coolHint({ cool_until: "2026-10-23T06:59:00Z" }, now), ""); // 已过
});

test("scrapeProgressBar：附加冷却提示（演示数字 23 分钟）", () => {
  const p = scrapeProgress(new Map([[1, "ok"], [2, "pending"], [3, "fetching"]]));
  const h = scrapeProgressBar(p, { cool_remaining_s: 23 * 60 });
  assert.match(h, /评论抓取 已完成 1 \/ 合计 3（排队 1 · 进行中 1 · 失败 0） · 防拦截冷却中，约 23 分钟后继续/);
  assert.match(h, /防拦截冷却中，约 23 分钟后继续/);
  assert.equal(scrapeProgressBar(p, { cool_remaining_s: 0 }), scrapeProgressBar(p)); // 无冷却不改文案
  assert.equal(scrapeProgressBar(scrapeProgress(new Map([[1, "ok"]])), { cool_remaining_s: 600 }), ""); // 无 busy 仍空
});

test("app.js：进度条接线 + revprog/revcool 演示 + 轮询刷新", () => {
  const app = readFileSync(new URL("../js/app.js", import.meta.url), "utf8");
  assert.match(app, /scrapeProgressBar\(scrapeProgress\(L\.scr\), L\.cool\)/);
  assert.match(app, /from "\.\/velocity\.js\?v=[0-9a-f]+"/);
  assert.match(app, /scrapeProgress, scrapeProgressBar, coolHint/);
  assert.match(app, /QS\.has\("revprog"\)/);
  assert.match(app, /QS\.has\("revcool"\)/);
  assert.match(app, /loadReviewsCool/);
  assert.match(app, /box-reviews/);
  assert.match(app, /cool_until|cool_remaining_s/);
  assert.match(app, /pad\("ok", 12/);
  assert.match(app, /product_library"\)\.select\("etsy_listing_id"\)/);
  assert.match(app, /function scheduleScrapePoll/);
  // 有 busy 或冷却才轮询（沿用 SCRAPE_BUSY + coolHint）
  assert.ok(SCRAPE_BUSY.has("pending") && SCRAPE_BUSY.has("fetching"));
  assert.match(app, /cooling/);
});

test("红线：无新迁移；不碰 sel_keyword_log / SERVICE_ROLE", async () => {
  const app = readFileSync(new URL("../js/app.js", import.meta.url), "utf8");
  const vel = readFileSync(new URL("../js/velocity.js", import.meta.url), "utf8");
  const { readdirSync } = await import("node:fs");
  assert.doesNotMatch(app, /SERVICE_ROLE/);
  assert.doesNotMatch(vel, /sel_keyword_log|SERVICE_ROLE/);
  const migs = readdirSync(new URL("../../supabase/migrations", import.meta.url));
  // 进度条功能本身不带迁移（055 编号已被 055_library_remove_delete 正常占用，只禁“进度”类迁移）
  assert.ok(!migs.some((f) => /reviews.?progress|scrape.?progress/i.test(f)));
});
