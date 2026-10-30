// node --test web/tests/ —— 058 从 ABA 导入 10 个：小方块多选
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ABA_PICK_N, togglePick, pickedInOrder, abaOkText, abaPickHtml } from "../js/selscreen.js";
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const L = Array.from({ length: 10 }, (_, i) => ({ root_id: i + 1, keyword: `kw${i + 1}`, root_zh: i === 3 ? null : `词${i + 1}`, seen_count: i === 2 ? 2 : 0 }));
test("一次取 10 个", () => assert.equal(ABA_PICK_N, 10));
test("点一下选中、再点取消", () => { let s = togglePick(new Set(), 3); assert.ok(s.has(3)); s = togglePick(s, 3); assert.ok(!s.has(3)); });
test("选中 8 个：按 ABA 原顺序返回，未选不入队", () => {
  let s = new Set(); for (const id of [10, 1, 5, 2, 7, 3, 9, 4]) s = togglePick(s, id);
  const p = pickedInOrder(L, s); assert.equal(p.length, 8); assert.deepEqual(p.map((x) => x.root_id), [1, 2, 3, 4, 5, 7, 9, 10]);
  assert.equal(abaOkText(8), "确认（已选 8 个）"); assert.equal(abaOkText(0), "确认（未选择）");
});
test("方块：中英文、无中文时显示暂无中文、选中态、已查过、转义", () => {
  const h = abaPickHtml([...L, { root_id: 99, keyword: '<x>"', root_zh: "", seen_count: 0 }], new Set([1]), esc);
  assert.equal((h.match(/class="aba-chip/g) || []).length, 11); assert.match(h, /aba-chip on" data-abapick="1" aria-pressed="true"/);
  assert.match(h, /kw1<\/b><span class="aba-zh">词1/); assert.match(h, /aba-zh none">暂无中文/); assert.match(h, /查过 2 次/);
  assert.ok(!h.includes("<x>")); assert.match(h, /已选 1 \/ 11/); assert.ok(!h.includes("演示数据")); assert.match(abaPickHtml(L, new Set(), esc, true), /演示数据/);
});
test("058 SQL：预览不写 aba_root_sends；导入只入选中的、跳过已导入、走 sel_count", () => {
  const q = readFileSync(new URL("../../supabase/migrations/058_aba_pick_to_selection.sql", import.meta.url), "utf8");
  const prev = q.slice(q.indexOf("function public.aba_preview_for_selection"), q.indexOf("function public.aba_import_selected"));
  assert.ok(!/insert\s+into/i.test(prev)); assert.match(prev, /not exists \(select 1 from public\.aba_root_sends/);
  assert.match(q, /with ordinality/); assert.match(q, /public\.sel_count\(r\.root_word, f\)/); assert.ok(!/delete\s+from\s+public\.sel_keyword_log/i.test(q));
  assert.ok(!/alter table|create table/i.test(q));
});
