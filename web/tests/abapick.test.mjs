// node --test web/tests/ —— 058 从 ABA 导入 10 个：小方块多选
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ABA_PICK_N, togglePick, pickedInOrder, abaOkText, abaPickHtml, nextAbaBatch } from "../js/selscreen.js";
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const L = Array.from({ length: 10 }, (_, i) => ({ root_id: i + 1, keyword: `kw${i + 1}`, root_zh: i === 3 ? null : `词${i + 1}`, seen_count: i === 2 ? 2 : 0 }));
test("一次取 10 个", () => assert.equal(ABA_PICK_N, 10));
test("点一下选中、再点取消", () => { let s = togglePick(new Set(), 3); assert.ok(s.has(3)); s = togglePick(s, 3); assert.ok(!s.has(3)); });
test("选中 8 个：按 ABA 原顺序返回，未选不入队", () => {
  let s = new Set(); for (const id of [10, 1, 5, 2, 7, 3, 9, 4]) s = togglePick(s, id);
  const p = pickedInOrder(L, s); assert.equal(p.length, 8); assert.deepEqual(p.map((x) => x.root_id), [1, 2, 3, 4, 5, 7, 9, 10]);
  assert.equal(abaOkText(8), "确认（已选 8 个）"); assert.equal(abaOkText(0), "确认（未选择）");
});
test("方块：中英文、无中文时显示暂无中文、选中态、不再显示查过、转义", () => {
  const h = abaPickHtml([...L, { root_id: 99, keyword: '<x>"', root_zh: "", seen_count: 0 }], new Set([1]), esc);
  assert.equal((h.match(/class="aba-chip/g) || []).length, 11); assert.match(h, /aba-chip on" data-abapick="1" aria-pressed="true"/);
  assert.match(h, /kw1<\/b><span class="aba-zh">词1/); assert.match(h, /aba-zh none">暂无中文/); assert.ok(!/查过 \d+ 次/.test(h) && !h.includes("aba-seen"));
  assert.ok(!h.includes("<x>")); assert.match(h, /已选 1 \/ 11/); assert.ok(!h.includes("演示数据")); assert.match(abaPickHtml(L, new Set(), esc, true), /演示数据/);
});
test("058 SQL：预览不写 aba_root_sends；导入只入选中的、跳过已导入、走 sel_count", () => {
  const q = readFileSync(new URL("../../supabase/migrations/058_aba_pick_to_selection.sql", import.meta.url), "utf8");
  const prev = q.slice(q.indexOf("function public.aba_preview_for_selection"), q.indexOf("function public.aba_import_selected"));
  assert.ok(!/insert\s+into/i.test(prev)); assert.match(prev, /not exists \(select 1 from public\.aba_root_sends/);
  assert.match(q, /with ordinality/); assert.match(q, /public\.sel_count\(r\.root_word, f\)/); assert.ok(!/delete\s+from\s+public\.sel_keyword_log/i.test(q));
  assert.ok(!/alter table|create table/i.test(q));
});

test("059 依次出词：每次 10 个不一样，出过的（含没选的）不再出现，到底为空", () => {
  const all = Array.from({ length: 25 }, (_, i) => ({ root_id: i + 1 }));
  let [b1, s] = nextAbaBatch(all, new Set()); assert.deepEqual(b1.map((x) => x.root_id), [1,2,3,4,5,6,7,8,9,10]);
  let b2; [b2, s] = nextAbaBatch(all, s); assert.deepEqual(b2.map((x) => x.root_id), [11,12,13,14,15,16,17,18,19,20]);
  let b3; [b3, s] = nextAbaBatch(all, s); assert.equal(b3.length, 5); let b4; [b4] = nextAbaBatch(all, s); assert.equal(b4.length, 0);
});
test("059 文案与 RPC：不再说下次仍会出现，前端调用 aba_next_for_selection", () => {
  const js = readFileSync(new URL("../js/selscreen.js", import.meta.url), "utf8"), h = abaPickHtml(L, new Set(), esc);
  assert.ok(!h.includes("下次仍会出现")); assert.match(h, /下次都不会再出现/); assert.match(js, /rpc\("aba_next_for_selection"/); assert.ok(!/rpc\("aba_preview_for_selection"/.test(js));
});
test("059 SQL：服务端按用户记游标、打开即写、跳过已导入/已查过、不删数据", () => {
  const q = readFileSync(new URL("../../supabase/migrations/059_aba_seq_picker.sql", import.meta.url), "utf8");
  assert.match(q, /create table if not exists public\.sel_aba_shown/); assert.match(q, /primary key \(user_id, root_id\)/);
  assert.match(q, /insert into public\.sel_aba_shown/); assert.match(q, /aba_root_sends s where/); assert.match(q, /sel_keyword_log l where lower/);
  assert.match(q, /order by r\.best_rank nulls last, r\.term_count desc, r\.id/); assert.ok(!/\bdelete\b|drop table|truncate/i.test(q)); assert.match(q, /enable row level security/);
});
