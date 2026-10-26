// node --test web/tests/ —— 预筛选列表去重（041）
import test from "node:test";
import assert from "node:assert/strict";
import { dedupeItems, listingKey } from "../js/screen.js";
test("规范化 key：listing id 优先，否则取 URL 里的数字（忽略域名/查询串/斜杠）", () => {
  assert.equal(listingKey({ listing_id: 123 }), "123");
  for (const u of ["https://www.etsy.com/listing/123/x?ref=a", "https://de.etsy.com/listing/123/", "http://etsy.com/listing/123#r"]) assert.equal(listingKey({ etsy_url: u }), "123");
});
test("同一 listing 跨批次 / 重发 / URL 变体只显示 1 条，保留有数据的、顺序不变", () => {
  const items = [
    { id: "a", listing_id: 1, status: "queued", etsy_url: "https://www.etsy.com/listing/1/x" },
    { id: "b", listing_id: 2, status: "screening" },
    { id: "c", listing_id: null, status: "screening", etsy_url: "https://de.etsy.com/listing/1/x?ref=y" },
    { id: "d", listing_id: 2, status: "screening" },
    { id: "e", listing_id: 3, status: "failed" }];
  assert.deepEqual(dedupeItems(items).map((x) => x.id), ["b", "c", "e"]);
  assert.deepEqual(dedupeItems([]), []); assert.deepEqual(dedupeItems(null), []);
});
test("1 万条去重仍是线性的（不拖慢列表）", () => {
  const xs = Array.from({ length: 20000 }, (_, i) => ({ id: String(i), listing_id: (i % 10000) + 1, status: "screening" }));
  const t = Date.now(); assert.equal(dedupeItems(xs).length, 10000); assert.ok(Date.now() - t < 200);
});
