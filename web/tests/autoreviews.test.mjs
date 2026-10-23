// node --test web/tests/ —— 049 产品库评论日期自动抓取：卡片状态徽标
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { scrapeChip, cardVelocityBadge, SCRAPE_BUSY, computeVelocity } from "../js/velocity.js";
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
test("pending / fetching → 评论抓取中", () => {
  for (const s of ["pending", "fetching"]) { assert.ok(SCRAPE_BUSY.has(s)); assert.match(scrapeChip(s, 1), /评论抓取中/); assert.doesNotMatch(scrapeChip(s, 1), /data-rretry/); }
});
test("failed → 抓取失败 重试 按钮（带 listing id）", () => {
  const h = scrapeChip("failed", 1779663270); assert.match(h, /<button[^>]*data-rretry="1779663270"[^>]*>抓取失败 重试<\/button>/);
  assert.equal(scrapeChip("failed", '1"><img'), scrapeChip("failed", NaN)); // id 强制数字，不注入
});
test("ok / 无状态 → 不显示", () => { assert.equal(scrapeChip("ok", 1), ""); assert.equal(scrapeChip(undefined, 1), ""); });
test("速度模式：有速度显示速度；没有 → 抓取状态；都没有 → 无评论日期数据", () => {
  const now = Date.parse("2026-10-21T00:00:00Z"), v = computeVelocity(["2026-10-10", "2026-09-01"], now);
  assert.match(cardVelocityBadge(v, "pending", 1, esc), /速度 2\/6 · 2条/);
  assert.match(cardVelocityBadge(null, "pending", 1, esc), /评论抓取中/);
  assert.match(cardVelocityBadge(null, "failed", 1, esc), /抓取失败 重试/);
  assert.match(cardVelocityBadge(null, "ok", 1, esc), /无评论日期数据/);
});
test("app.js 接线：重试 RPC、点击处理、轮询", () => {
  const a = readFileSync(new URL("../js/app.js", import.meta.url), "utf8");
  assert.match(a, /sb\.rpc\("retry_review_scrape", \{ p_listing: id \}\)/);
  assert.match(a, /dataset\?\.rretry\) return retryScrape\(Number/);
  assert.match(a, /cardVelocityBadge\(velOf\(r\)/);
  assert.match(a, /function scheduleScrapePoll/);
});
test("评论抓取中徽标：评论抓取优先提示", () => {
  assert.match(scrapeChip("pending", 1), /评论抓取优先/);
  assert.doesNotMatch(scrapeChip("pending", 1), /低优先级/);
});

