// node --test web/tests/ehlogin.test.mjs —— 058 EHunt 登录状态横幅 / 重新登录按钮
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loginBanner, loginFailedCount } from "../js/ehlogin.js";

test("登录失效失败任务计数：只认登录失效的 failed/canceled", () => {
  assert.equal(loginFailedCount([{ status: "failed", error: "EHunt 登录已失效（…）" }, { status: "canceled", error: "读取中断（EHunt 登录已失效…）" },
    { status: "failed", error: "EHunt 页面响应超时" }, { status: "running", error: "EHunt 未登录" }]), 2);
});
test("需要人工登录：明确中文提示 + 按钮 + 队列已暂停", () => {
  const b = loginBanner({ ehunt_login_state: "needs_manual", ehunt_login_msg: "EHunt 需要人工登录：登录页出现验证码/人机验证" }, 5);
  assert.equal(b.title, "EHunt 需要人工登录"); assert.ok(b.button); assert.match(b.msg, /验证码/); assert.match(b.msg, /已暂停/);
  assert.equal(loginBanner({ ehunt_login_state: "cooldown", ehunt_login_msg: "x" }).title, "EHunt 需要人工登录");
});
test("登录失效但未确定需人工：提示 + 按钮；058 未执行时不显示按钮", () => {
  assert.ok(loginBanner(null, 3).button); assert.equal(loginBanner(null, 3, false).button, false); assert.equal(loginBanner(null, 0), null);
});
test("自动登录中：无按钮；成功 10 分钟内显示结果", () => {
  assert.equal(loginBanner({ ehunt_login_state: "relogging" }, 3).button, false);
  assert.equal(loginBanner({ ehunt_login_state: "ok", ehunt_login_msg: "已重新排队", ehunt_login_at: new Date().toISOString() }).kind, "ok");
  assert.equal(loginBanner({ ehunt_login_state: "ok", ehunt_login_msg: "x", ehunt_login_at: "2020-01-01T00:00:00Z" }), null);
});
test("按钮不收集密码，只调用 ehunt_request_relogin", () => {
  const src = readFileSync(new URL("../js/ehlogin.js", import.meta.url), "utf8");
  assert.ok(!/type=["']?password|EHUNT_PASSWORD/.test(src)); assert.match(src, /rpc\("ehunt_request_relogin"\)/);
});
