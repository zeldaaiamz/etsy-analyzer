// node --test web/tests/ —— 产品库评论抓取进度条（listing_scrape_status 客户端聚合）
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { scrapeProgress, scrapeProgressBar, SCRAPE_BUSY } from "../js/velocity.js";

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

test("app.js：进度条接线 + revprog 演示 + 轮询刷新", () => {
  const app = readFileSync(new URL("../js/app.js", import.meta.url), "utf8");
  assert.match(app, /scrapeProgressBar\(scrapeProgress\(L\.scr\)\)/);
  assert.match(app, /from "\.\/velocity\.js\?v=[0-9a-f]+"/);
  assert.match(app, /scrapeProgress, scrapeProgressBar/);
  assert.match(app, /QS\.has\("revprog"\)/);
  assert.match(app, /pad\("ok", 12/);
  assert.match(app, /product_library"\)\.select\("etsy_listing_id"\)/);
  assert.match(app, /进度条：排队\/进行中变化也要刷新/);
  assert.match(app, /function scheduleScrapePoll/);
  // 有 busy 才轮询（沿用 SCRAPE_BUSY）
  assert.ok(SCRAPE_BUSY.has("pending") && SCRAPE_BUSY.has("fetching"));
});

test("红线：无新迁移；不碰 sel_keyword_log / SERVICE_ROLE", async () => {
  const app = readFileSync(new URL("../js/app.js", import.meta.url), "utf8");
  const vel = readFileSync(new URL("../js/velocity.js", import.meta.url), "utf8");
  const { readdirSync } = await import("node:fs");
  assert.doesNotMatch(app, /SERVICE_ROLE/);
  assert.doesNotMatch(vel, /sel_keyword_log|SERVICE_ROLE/);
  const migs = readdirSync(new URL("../../supabase/migrations", import.meta.url));
  assert.ok(!migs.some((f) => /055|reviews.?progress|scrape.?progress/i.test(f)));
});
