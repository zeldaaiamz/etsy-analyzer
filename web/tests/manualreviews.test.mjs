// node --test web/tests/ —— 054 产品库「手动抓取评论速度」
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../js/app.js", import.meta.url), "utf8");
const scraper = readFileSync(new URL("../../worker/selection/review_scraper.py", import.meta.url), "utf8");
const mig = readFileSync(new URL("../../supabase/migrations/054_manual_reviews.sql", import.meta.url), "utf8");

test("迁移 054：RPC queue_library_review_scrapes；仅 authenticated；不动 ok", () => {
  assert.match(mig, /create or replace function public\.queue_library_review_scrapes/);
  assert.match(mig, /grant execute on function public\.queue_library_review_scrapes\(bigint\[\]\) to authenticated/);
  assert.match(mig, /revoke all on function public\.queue_library_review_scrapes/);
  assert.match(mig, /status = 'failed'/);
  assert.match(mig, /skipped_ok/);
  assert.match(mig, /除了获取失败，不重复获取|永不重抓/);
  // 正文不得 DML/DDL 碰 sel_keyword_log（注释可写红线）
  const body = mig.replace(/--[^\n]*/g, "");
  assert.doesNotMatch(body, /sel_keyword_log/);
});

test("app.js：按钮「手动抓取评论速度」+ RPC + 选中缩小范围 + worker 未运行 toast", () => {
  assert.match(app, /手动抓取评论速度/);
  assert.match(app, /data-lmanrev/);
  assert.match(app, /async function queueManualReviews/);
  assert.match(app, /sb\.rpc\("queue_library_review_scrapes"/);
  assert.match(app, /p_listings: useSel \? selected : null/);
  assert.match(app, /评论抓取进程未运行，已排队/);
  assert.match(app, /已排队 \$\{total\} 个评论抓取/);
  assert.match(app, /dataset\?\.lmanrev\) return queueManualReviews/);
  assert.match(app, /reviewsWorkerAlive/);
  assert.match(app, /box-reviews/);
  // 无法抓取侧栏旁 / 失败页也有按钮
  assert.equal((app.match(/data-lmanrev="1"/g) || []).length, 2);
});

test("app.js：演示 nfails=8；noworker toast；点完可离开（不阻塞）", () => {
  assert.match(app, /QS\.get\("nfails"\)/);
  assert.match(app, /QS\.has\("noworker"\)/);
  assert.match(app, /scheduleScrapePoll\(\)/);
});

test("review_scraper：心跳 box-reviews（前端探测用）", () => {
  assert.match(scraper, /box-reviews/);
  assert.match(scraper, /def heartbeat/);
  assert.match(scraper, /worker_heartbeat/);
});

test("红线：不碰 sel_keyword_log；不在迁移里写密钥", () => {
  assert.doesNotMatch(mig, /SERVICE_ROLE|api[_-]?key|password/i);
  const body = mig.replace(/--[^\n]*/g, "");
  assert.doesNotMatch(body, /sel_keyword_log/);
  assert.doesNotMatch(app, /SERVICE_ROLE/);
});
