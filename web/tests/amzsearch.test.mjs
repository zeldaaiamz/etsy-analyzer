// node --test web/tests/  —— PM-001 一键亚马逊搜索
import test from "node:test";
import assert from "node:assert/strict";
import { amzUrls, openAll, applyEdit, canShowAmz, amzBlock, MAX_KW } from "../js/amzsearch.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const R = { id: "x", stage: "candidate", competitor_asin: "B0ABCDEF12", amazon_keywords: [
  { keyword: "church note", source: "title" }, { keyword: "bible & prayer journal", source: "review" }, { keyword: "sermon notebook", source: "original-title" }] };
const fakeWin = (blockAfter = Infinity) => { const calls = []; return { calls, open(u, t) { calls.push([u, t]); return calls.length > blockAfter ? null : {}; } }; };

test("click opens exactly N tabs with correct US URLs", () => {
  const w = fakeWin(), blocked = openAll(amzUrls(R), w);
  assert.equal(blocked, 0);
  assert.deepEqual(w.calls, [["https://www.amazon.com/s?k=church%20note", "_blank"],
    ["https://www.amazon.com/s?k=bible%20%26%20prayer%20journal", "_blank"], ["https://www.amazon.com/s?k=sermon%20notebook", "_blank"]]);
});
test("ASIN toggle off by default, on adds /dp/ page", () => {
  assert.equal(amzUrls(R).length, 3);
  assert.deepEqual(amzUrls(R, true).at(-1), "https://www.amazon.com/dp/B0ABCDEF12");
  assert.equal(amzUrls({ ...R, competitor_asin: null }, true).length, 3);
});
test("blocked pop-ups are counted", () => { assert.equal(openAll(amzUrls(R), fakeWin(1)), 2); });
test("stage visibility", () => {
  assert.ok(canShowAmz("candidate") && canShowAmz("active") && !canShowAmz("rejected"));
  assert.match(amzBlock(R, { esc }), /data-amz="x"/);
  assert.doesNotMatch(amzBlock({ ...R, stage: "rejected" }, { esc }), /data-amz=/);
  const none = amzBlock({ ...R, amazon_keywords: [] }, { esc });
  assert.match(none, /data-amz="x" disabled/); assert.match(none, /title="关键词未生成"/);
  assert.match(amzBlock(R, { esc }), /data-amzasin="x">/); // 竞品 ASIN 默认不勾选
});
test("edit: keep sources, new = manual, dedupe, max 5", () => {
  const out = applyEdit(R.amazon_keywords, ["Church  Note", "", "summer note", "church note"]);
  assert.deepEqual(out.map((k) => [k.keyword, k.source]), [["church note", "title"], ["summer note", "manual"]]);
  assert.throws(() => applyEdit([], Array.from({ length: MAX_KW + 1 }, (_, i) => `k${i}`)));
});
test("direction label shown; edit keeps/sets direction", async () => {
  const { DIR_NAME } = await import("../js/amzsearch.js");
  const r = { ...R, amazon_keywords: [{ keyword: "retro phone camera magnet", source: "image", direction: 1 }, { keyword: "record your life", source: "image", direction: 3 }] };
  const html = amzBlock(r, { esc });
  assert.match(html, /关键词 \(2\) ▾/); assert.match(html, /D1 精准词/); assert.doesNotMatch(html, /D3 产品文案/); assert.match(amzBlock(r, { esc, open: true }), /D3 产品文案\/卖点词/); assert.equal(Object.keys(DIR_NAME).length, 5);
  assert.match(amzBlock(r, { esc, editing: true }), /<option value="1" selected>D1 精准词/);
  const out = applyEdit(r.amazon_keywords, [{ keyword: "retro phone camera magnet", direction: "1" }, { keyword: "church supplies for women", direction: "5" }, { keyword: "no dir", direction: "" }]);
  assert.deepEqual(out.map((k) => [k.keyword, k.source, k.direction]), [["retro phone camera magnet", "image", 1], ["church supplies for women", "manual", 5], ["no dir", "manual", undefined]]);
});
