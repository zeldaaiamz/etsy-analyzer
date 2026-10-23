// node --test web/tests/ —— 获取链接 QA 2026-10-19
import test from "node:test";
import assert from "node:assert/strict";
import { normKw, safeName, friendlyErr, chipText, waitedMin } from "../js/selscreen.js";
import { tooBroad } from "../js/exwords.js";

test("关键词：去首尾空格、合并连续空白", () => {
  assert.equal(normKw("  acrylic \t  decoration  "), "acrylic decoration");
  assert.equal(normKw("   "), ""); assert.equal(normKw(null), "");
});
test("下载文件名保留中文，其他符号换成 _", () => {
  assert.equal(safeName("亚克力 摆件"), "亚克力_摆件");
  assert.equal(safeName("Mr & Mrs sign"), "Mr_Mrs_sign");
  assert.equal(safeName(""), "links"); assert.equal(safeName("x".repeat(300)).length, 60);
});
test("worker 原始报错 → 中文提示", () => {
  assert.match(friendlyErr("It looks like you are using Playwright Sync API inside the asyncio loop.\nPlease use the Async API instead."), /重启 box/);
  assert.match(friendlyErr("Remote end closed connection without response"), /连接中断/);
  assert.match(friendlyErr("Page.click: Timeout 30000ms exceeded.\nCall log:"), /超时/);
  const zh = "读取中断（HTTP 503），已读部分已保留，可点「继续本批」接着读"; assert.equal(friendlyErr(zh), zh);
  assert.equal(friendlyErr(null), "");
});
test("最近 3 天标签按状态显示", () => {
  assert.equal(chipText({ status: "counted", total_count: 296 }), "待抓取 · 共 296 条");
  assert.equal(chipText({ status: "counted", total_count: 0 }), "0 条 · 不抓取");
  assert.equal(chipText({ status: "counting" }), "查询中");
  assert.equal(chipText({ status: "failed", total_count: null }), "查询失败");
  assert.equal(chipText({ status: "canceled", passed_count: 3 }), "已停止 · 符合 3 条");
  assert.equal(chipText({ status: "succeeded", passed_count: 1200 }), "符合 1,200 条");
});
test("等待分钟数", () => { assert.equal(waitedMin("2026-10-19T15:00:00Z", Date.parse("2026-10-19T15:07:30Z")), 7); assert.equal(waitedMin(null), 0); });
test("排除词太宽", () => {
  for (const w of ["a|", ".*", "\\w+", "|name", "(x)?"]) assert.ok(tooBroad(w), w);
  for (const w of ["name", "personali[sz]ed?", "photo(?!\\s*(booths?|props?)\\b)", "super ?bowl", "monogram\\w*"]) assert.ok(!tooBroad(w), w);
});
test("最近 3 天查找记录：最多 10 条、新的在前、3 天外不显示；折叠默认桌面展开/手机收起，记住选择", async () => {
  const { recentList, recentOpen, RECENT_N } = await import("../js/selscreen.js");
  const now = Date.parse("2026-10-20T00:00:00Z"), rows = Array.from({ length: 14 }, (_, i) => ({ id: `r${i}`, created_at: new Date(now - i * 36e5).toISOString() }));
  rows.push({ id: "old", created_at: new Date(now - 4 * 864e5).toISOString() });
  const L = recentList([...rows].reverse(), now);
  assert.equal(RECENT_N, 10); assert.equal(L.length, 10); assert.equal(L[0].id, "r0"); assert.equal(L[9].id, "r9"); assert.ok(!L.some((r) => r.id === "old"));
  const st = (v) => ({ getItem: () => v });
  assert.equal(recentOpen(st(null), false), true); assert.equal(recentOpen(st(null), true), false);
  assert.equal(recentOpen(st("0"), false), false); assert.equal(recentOpen(st("1"), true), true); assert.equal(recentOpen(undefined, false), true);
});
test("送去预筛选状态：按批次，显示最近发送的批次", async () => {
  const { sentInfo, cnNum } = await import("../js/selscreen.js");
  assert.deepEqual([1, 2, 5, 10, 12, 20, 23].map(cnNum), ["一", "二", "五", "十", "十二", "二十", "二十三"]);
  assert.equal(sentInfo([]).text, "未发送"); assert.equal(sentInfo(undefined).sent, false); assert.equal(sentInfo([{ batch_no: 1, sent_to_screen_at: null }]).text, "未发送");
  const s = sentInfo([{ batch_no: 1, sent_to_screen_at: "2026-10-20T01:00:00Z" }, { batch_no: 2, sent_to_screen_at: "2026-10-20T02:00:00Z" }, { batch_no: 3 }]);
  assert.equal(s.text, "第二批已发送"); assert.equal(s.tip, "第一、二批已发送到预筛选");
  assert.equal(sentInfo([{ batch_no: 2, sent_to_screen_at: "2026-10-20T01:00:00Z" }, { batch_no: 1, sent_to_screen_at: "2026-10-20T03:00:00Z" }]).text, "第一批已发送"); // 以最近一次发送为准
});
test("历史列表认识 paused（037 队列）", async () => {
  const { chipText } = await import("../js/selscreen.js");
  assert.equal(chipText({ status: "paused", passed_count: 12 }), "已暂停 · 已符合 12 条");
});
