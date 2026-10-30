// 056：产品不可售徽标 + 手动录入（同一公式）+ 迁移/worker 红线
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { scrapeChip, cardVelocityBadge, computeVelocity, computeManualVelocity, manualActive, validateManualEntry, manualBucketRanges, MANUAL_BUCKETS, scrapeProgress, scrapeProgressBar } from "../js/velocity.js";

const root = new URL("../../", import.meta.url);
const app = readFileSync(new URL("web/js/app.js", root), "utf8");
const DAY = 864e5, NOW = Date.parse("2026-10-28T12:00:00");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

test("unavailable → 红色「抓取失败：产品不可售」，不是重试按钮；原因进 title 且转义", () => {
  const h = scrapeChip("unavailable", 1439586162, '商品已下架（HTTP 410）"><img');
  assert.match(h, /<span class="pb pb-scr bad unav"[^>]*>抓取失败：产品不可售<\/span>/);
  assert.match(h, /HTTP 410/); assert.doesNotMatch(h, /"><img/);
  assert.match(h, /data-rretry="1439586162"[^>]*>↻</); // 只留一个小 ↻（商品恢复在售时手动点）
  assert.doesNotMatch(h, /data-mentry/);                   // 不可售不提供手动录入
  assert.match(cardVelocityBadge(null, "unavailable", 1, esc), /抓取失败：产品不可售/);
});

test("failed（在售抓不到）→ 重试 + 「手动录入」按钮", () => {
  const h = scrapeChip("failed", 4363080306);
  assert.match(h, /data-rretry="4363080306"[^>]*>抓取失败 重试<\/button>/);
  assert.match(h, /<button[^>]*data-mentry="4363080306"[^>]*>手动录入<\/button>/);
  assert.equal(scrapeChip("failed", '1"><x'), scrapeChip("failed", NaN));
});

test("手动录入 = 同一公式：6 段计数 + 最近评论日期 与 computeVelocity(日期) 完全一致", () => {
  const ages = [3, 9, 9, 25, 33, 58, 61, 89, 100, 130, 131, 175, 179, 200, 400];
  const dates = ages.map((a) => new Date(NOW - a * DAY - 3600e3).toISOString());
  const counts = [0, 0, 0, 0, 0, 0]; ages.filter((a) => a < 180).forEach((a) => counts[Math.min(5, Math.floor(a / 30))]++);
  const latest = new Date(NOW - 3 * DAY - 3600e3).toISOString().slice(0, 10);
  const a = computeVelocity(dates, NOW), b = computeManualVelocity({ month_counts: counts, latest_review_date: latest }, NOW);
  for (const k of ["months", "n6", "avg", "recent", "prior", "trend", "stale"]) assert.equal(b[k], a[k], k);
  assert.ok(Math.abs(b.days - a.days) <= 1); assert.equal(b.manual, true);
  assert.deepEqual(counts, [4, 2, 2, 1, 2, 2]);
});

test("validateManualEntry：整数、日期、与分段一致", () => {
  const ok = validateManualEntry(["3", "2", "", "1", "0", "0"], "2026-10-20", NOW);
  assert.deepEqual(ok, { ok: true, counts: [3, 2, 0, 1, 0, 0], latest: "2026-10-20" });
  assert.equal(validateManualEntry(["", "", "", "", "", ""], "", NOW).ok, true); // 全空 = 0 条
  assert.match(validateManualEntry(["-1"], "2026-10-20", NOW).err, /0–10000 的整数/);
  assert.match(validateManualEntry(["1.5"], "2026-10-20", NOW).err, /近 30 天/);
  assert.match(validateManualEntry(["99999"], "2026-10-20", NOW).err, /整数/);
  assert.match(validateManualEntry(["2"], "", NOW).err, /最近一条评论日期/);
  assert.match(validateManualEntry(["2"], "2026-10-29", NOW).err, /不能晚于今天/);
  assert.match(validateManualEntry(["2"], "2026/10/01", NOW).err, /格式/);
  assert.match(validateManualEntry(["2", "1"], "2026-09-10", NOW).err, /更近的时段应为 0/);   // 48 天前 → 近 30 天不应有
  assert.match(validateManualEntry(["0", "0", "3"], "2026-09-10", NOW).err, /31–60 天」至少应有 1 条/);
  assert.match(validateManualEntry(["0", "0", "0", "0", "0", "1"], "2026-01-01", NOW).err, /180 天前/);
  assert.equal(validateManualEntry(["0", "0", "0", "0", "0", "0"], "2026-01-01", NOW).ok, true);
});

test("手动值优先，只有之后显式重抓成功才让位", () => {
  const m = { updated_at: "2026-10-28T10:00:00Z" };
  assert.equal(manualActive(null, "failed"), false);
  assert.equal(manualActive(m, "failed", "2026-10-28T11:00:00Z"), true);
  assert.equal(manualActive(m, "pending", "2026-10-28T11:00:00Z"), true);
  assert.equal(manualActive(m, "ok", "2026-10-28T09:00:00Z"), true);
  assert.equal(manualActive(m, "ok", "2026-10-28T11:00:00Z"), false);
});

test("分段提示：6 段，含日期区间", () => {
  assert.equal(MANUAL_BUCKETS.length, 6);
  const r = manualBucketRanges(NOW); assert.equal(r.length, 6); assert.equal(r[0], "09-29 ~ 10-28");
});

test("进度：unavailable 单独计数，不进合计；文案不变", () => {
  const p = scrapeProgress(new Map([[1, "ok"], [2, "pending"], [3, "unavailable"], [4, "failed"]]));
  assert.equal(p.unavailable, 1); assert.equal(p.total, 3);
  assert.match(scrapeProgressBar(p), /已完成 1 \/ 合计 3（排队 1 · 进行中 0 · 失败 1）/);
});

test("app.js：不可售归入「无法抓取」、手动录入弹窗走 save_manual_velocity、批量跳过", () => {
  assert.match(app, /v === "failed" \|\| v === "unavailable"/);
  assert.match(app, /function openManualEntry\(id\)/);
  assert.match(app, /validateManualEntry\(raw, latest\)/);
  assert.match(app, /sb\.rpc\("save_manual_velocity", \{ p_listing: id, p_counts: counts, p_latest: latest \}\)/);
  assert.match(app, /data-mentry/); assert.match(app, /t\.dataset\?\.mentry\) return openManualEntry/);
  assert.match(app, /st === "unavailable" \|\| \(st !== "pending" && L\.man\.has\(id\)\)\) continue/);
  assert.match(app, /manualActive\(L\.man\.get\(id\), L\.scr\.get\(id\), L\.scrAt\.get\(id\)\)/);
  assert.match(app, /Reviews for this item/); assert.match(app, /Most recent/);
  assert.match(app, /演示数据/);
});

test("056 迁移：unavailable 状态、跳过、红线", () => {
  const migs = readdirSync(new URL("supabase/migrations/", root));
  const f = migs.find((x) => /^056_/.test(x)); assert.ok(f, "056 迁移存在");
  assert.equal(migs.filter((x) => /^056_/.test(x)).length, 1, "056 只有一个迁移"); // 原「无 057–059」在 057/058 正常上线后不再成立
  const sql = readFileSync(new URL(`supabase/migrations/${f}`, root), "utf8");
  const body = sql.split("\n").filter((l) => !l.trim().startsWith("--")).join("\n");
  assert.match(body, /'ok', 'failed', 'pending', 'fetching', 'unavailable'/);
  assert.match(body, /status in \('failed', 'unavailable'\)/);          // 显式重试
  assert.match(body, /skipped_unavailable/); assert.match(body, /skipped_manual/);
  assert.match(body, /status = 'failed';/);                               // 批量只把 failed → pending
  assert.match(sql, /除了获取失败，不重复获取/);
  assert.doesNotMatch(body, /sel_keyword_log/); assert.doesNotMatch(body, /claim_review_scrapes/);
  assert.doesNotMatch(body, /drop table|truncate/i);
});

test("worker：只在失败路径判断不可售；404/410 → Unavailable", () => {
  const py = readFileSync(new URL("worker/selection/review_scraper.py", root), "utf8");
  assert.match(py, /class Unavailable\(FetchError\)/);
  assert.match(py, /if st in \(404, 410\): raise Unavailable/);
  assert.match(py, /if hdr is None:.*\n\s+why = unavailable_reason/);
  assert.match(py, /store\.finish\(lid, "unavailable"/);
  for (const s of ["this item is unavailable", "sorry, this item is sold out", "this shop is taking a short break"]) assert.ok(py.includes(s), s);
});
