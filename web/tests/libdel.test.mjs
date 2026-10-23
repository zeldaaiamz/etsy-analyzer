// node --test web/tests/ —— 055 产品库「删除产品」直接删除
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../js/app.js", import.meta.url), "utf8");
const mig = readFileSync(new URL("../../supabase/migrations/055_library_remove_delete.sql", import.meta.url), "utf8");
const arch = readFileSync(new URL("../../ARCHITECTURE.md", import.meta.url), "utf8");

test("迁移 055：delete_library_items；不删 scrape/reviews；保留 remove_from_library 语义说明", () => {
  assert.match(mig, /create or replace function public\.delete_library_items/);
  assert.match(mig, /grant execute on function public\.delete_library_items\(uuid\[\], uuid\[\]\) to authenticated/);
  assert.match(mig, /revoke all on function public\.delete_library_items/);
  assert.match(mig, /listing_scrape_status/);
  assert.match(mig, /listing_reviews/);
  assert.match(mig, /不删 listing_scrape_status/);
  assert.match(mig, /remove_from_library 保留/);
  assert.match(mig, /drop function if exists public\.delete_rejected_library/);
  assert.match(mig, /delete_rejected_library\(\) returns jsonb/);
  const body = mig.replace(/--[^\n]*/g, "");
  assert.doesNotMatch(body, /sel_keyword_log/);
  assert.doesNotMatch(body, /delete from public\.listing_scrape_status/);
  assert.doesNotMatch(body, /delete from public\.listing_reviews/);
});

test("app.js：卡片+批量走 delete_library_items；确认「删除不可恢复」；不回我的任务", () => {
  assert.match(app, /async function removeLibItems\(items\)/);
  assert.match(app, /sb\.rpc\("delete_library_items"/);
  assert.match(app, /删除不可恢复/);
  assert.match(app, /不会回到「我的任务」/);
  assert.match(app, /data-unlib/);
  assert.match(app, /data-lbulk="remove"/);
  assert.match(app, /删除产品/);
  assert.match(app, /data-unlib[^>]*>删除产品/);
  assert.match(app, /data-lbulk="remove">删除产品/);
  // 硬删入口不得再用「移出产品库」；软移出（任务列表 libBtn / libToggle toast）仍保留旧文案
  const cardBulk = [...app.matchAll(/data-unlib[^>]*>[^<]+|data-lbulk="remove">[^<]+/g)].map((m) => m[0]).join("\n");
  assert.match(cardBulk, /删除产品/);
  assert.doesNotMatch(cardBulk, /移出产品库/);
  assert.match(app, /title="点击移出产品库"/);
  assert.match(app, /已移出产品库 \$\{jobs\.length\} 个，已回到任务列表/);
  // 卡片/批量都调 removeLibItems（不再传 ask=false 跳过确认）
  assert.match(app, /dataset\?\.unlib[\s\S]*?removeLibItems\(\[r\]\)/);
  assert.match(app, /lbulk === "remove"\) return removeLibItems/);
  assert.doesNotMatch(app, /对应任务已回到/);
  // removeLibItems：直接删除文案；不得再说「已回到」
  const fn = app.slice(app.indexOf("async function removeLibItems"), app.indexOf("async function deleteJobs"));
  assert.match(fn, /delete_library_items/);
  assert.match(fn, /删除不可恢复/);
  assert.match(fn, /不会回到「我的任务」/);
  assert.doesNotMatch(fn, /已回到/);
  assert.doesNotMatch(fn, /remove_from_library/);
});

test("app.js：加入后撤销仍用 remove_from_library（任务回列表）", () => {
  assert.match(app, /async function undoAdd/);
  const undo = app.slice(app.indexOf("async function undoAdd"), app.indexOf("async function removeLibItems"));
  assert.match(undo, /remove_from_library/);
  assert.doesNotMatch(undo, /delete_library_items/);
});

test("app.js：delete_rejected_library 读 jsonb.deleted_library；文案不可恢复", () => {
  assert.match(app, /delete_rejected_library/);
  assert.match(app, /data\?\.deleted_library/);
  const rej = app.slice(app.indexOf("async function deleteAllRejected"), app.indexOf("function libAllBulk"));
  assert.match(rej, /删除不可恢复/);
  assert.match(rej, /不会回到「我的任务」/);
  assert.doesNotMatch(rej, /会回到“我的任务”/);
});

test("ARCHITECTURE 记载 055", () => {
  assert.match(arch, /055/);
  assert.match(arch, /delete_library_items/);
});

test("红线：不碰 sel_keyword_log；不在迁移写密钥", () => {
  assert.doesNotMatch(mig, /SERVICE_ROLE|api[_-]?key|password/i);
  const body = mig.replace(/--[^\n]*/g, "");
  assert.doesNotMatch(body, /sel_keyword_log/);
  assert.doesNotMatch(app, /SERVICE_ROLE/);
});
