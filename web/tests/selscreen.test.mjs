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
