// node --test web/tests/ —— 043 关键词查询记录
import test from "node:test";
import assert from "node:assert/strict";
import { kwKey, seenDates, seenWarn, kwLogCsv } from "../js/selscreen.js";
test("关键词归一：大小写/空格不敏感", () => { assert.equal(kwKey("  Acrylic \t Decoration "), kwKey("acrylic decoration")); });
test("提示文案：单个日期", () => { assert.equal(seenWarn(["2026-10-18T02:10:00Z"]), "该关键词在10月18日已经查询过"); assert.equal(seenWarn([]), ""); });
test("多个日期新的在前、同日合并、按北京时间", () => {
  assert.deepEqual(seenDates(["2026-10-12T06:30:00Z", "2026-10-18T17:00:00Z", "2026-10-18T02:10:00Z"]), ["10月19日", "10月18日", "10月12日"]);
  assert.equal(seenWarn(["2026-10-12T06:30:00Z", "2026-10-18T02:10:00Z", "2026-10-18T03:00:00Z"]), "该关键词在10月18日、10月12日已经查询过");
});
test("CSV 导出：BOM、北京时间、防公式注入", () => {
  const c = kwLogCsv([{ keyword: "=cmd", queried_at: "2026-10-18T02:10:00Z", source: "sel_count" }, { keyword: 'a,"b"', queried_at: "2026-10-12T06:30:00Z", source: "backfill_batches" }]);
  assert.ok(c.startsWith("\ufeff关键词,")); assert.match(c, /'=cmd,2026-10-18 10:10:00,sel_count/); assert.match(c, /"a,""b""",2026-10-12 14:30:00/);
});
