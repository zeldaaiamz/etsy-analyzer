// node --test web/tests/ —— 预筛选卡片 ✕ 软隐藏（与删除所选同 RPC）
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { applySoftHide, restoreSoftHide } from "../js/screen.js";

test("applySoftHide：按 id 从列表拿掉并返回 removed", () => {
  const items = [
    { id: "a", listing_id: 1 },
    { id: "b", listing_id: 2 },
    { id: "c", listing_id: 3 },
  ];
  const { items: next, removed } = applySoftHide(items, ["b"]);
  assert.deepEqual(next.map((x) => x.id), ["a", "c"]);
  assert.deepEqual(removed.map((x) => x.id), ["b"]);
  assert.deepEqual(applySoftHide(items, []).items.map((x) => x.id), ["a", "b", "c"]);
  assert.deepEqual(applySoftHide(null, ["a"]).items, []);
});

test("restoreSoftHide：拼回并按 listing_id 排序（演示撤销）", () => {
  const cur = [{ id: "a", listing_id: 1 }, { id: "c", listing_id: 3 }];
  const removed = [{ id: "b", listing_id: 2 }];
  assert.deepEqual(restoreSoftHide(cur, removed).map((x) => x.id), ["a", "b", "c"]);
});

test("卡片 ✕ 文案与软隐藏调用：title/aria 为删除；handler 走 hideOne 而非 V.rej", () => {
  const src = readFileSync(new URL("../js/screen.js", import.meta.url), "utf8");
  assert.match(src, /title="从列表删除"/);
  assert.match(src, /aria-label="删除"/);
  assert.doesNotMatch(src, /title="排除（不再分析）"/);
  assert.match(src, /async function hideOne\(/);
  assert.match(src, /if \(t\.dataset\.srej\) hideOne\(t\.dataset\.srej\);/);
  assert.match(src, /screen_hide_ids/);
  assert.match(src, /screen_unhide/);
  // 排除所选 / 分析剩余数据 仍用 V.rej
  assert.match(src, /data-sselact="rej"/);
  assert.match(src, /V\.rej\.add\(id\)/);
});
