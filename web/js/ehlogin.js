// 058 EHunt 登录状态横幅 +「重新登录 EHunt」按钮（不输入密码；只写请求标记，box worker 用本地凭据自动登录并回报）。
// 数据：service_status.ehunt_login_state / ehunt_login_msg / ehunt_login_at（worker 写）；sel_runs 中因登录失效失败的任务数。
// 058 未执行（列不存在）→ 只按失败任务显示提示，不显示按钮。
export const LOGIN_RE = /EHunt 登录已失效|EHunt 未登录/;
export function loginFailedCount(runs) { return (runs || []).filter((r) => ["failed", "canceled"].includes(r.status) && LOGIN_RE.test(r.error || "")).length; }
// 返回 null（不显示）或 { kind: bad|warn|ok, title, msg, button, busy }
export function loginBanner(st, failed = 0, has058 = true) {
  const s = st?.ehunt_login_state || null, msg = st?.ehunt_login_msg || "";
  if (s === "relogging") return { kind: "warn", title: "正在自动重新登录 EHunt…", msg: "完成后因登录失效失败的任务会自动重新排队", button: false, busy: true };
  if (s === "needs_manual") return { kind: "bad", title: "EHunt 需要人工登录", msg: (msg.replace(/^EHunt 需要人工登录：?/, "") || "自动登录失败") + "。选品队列已暂停（任务保持排队，不会逐个失败）。请在 box 上运行 deploy/box-selection-worker.sh login 人工登录后，点「重新登录 EHunt」确认。", button: has058, busy: false };
  if (s === "cooldown") return { kind: "bad", title: "EHunt 需要人工登录", msg: msg.replace(/^EHunt 需要人工登录：?/, "") + "。选品队列已暂停。", button: has058, busy: false };
  if (failed > 0) return { kind: "bad", title: "EHunt 登录已失效", msg: `${failed} 个任务因 EHunt 登录失效而失败。worker 会自动重新登录并把这些任务重新排队；也可以点「重新登录 EHunt」立即尝试。`, button: has058, busy: false };
  if (s === "ok" && msg && st?.ehunt_login_at && Date.now() - Date.parse(st.ehunt_login_at) < 10 * 60e3) return { kind: "ok", title: "EHunt 已登录", msg, button: false, busy: false };
  return null;
}
const DEMO_ST = {
  needs_manual: { ehunt_login_state: "needs_manual", ehunt_login_msg: "EHunt 需要人工登录：登录页出现验证码/人机验证", ehunt_login_at: new Date().toISOString() },
  cooldown: { ehunt_login_state: "cooldown", ehunt_login_msg: "EHunt 需要人工登录：30 分钟内自动登录已尝试 2 次（防止账号被锁），约 24 分钟后可再试", ehunt_login_at: new Date().toISOString() },
  relogging: { ehunt_login_state: "relogging", ehunt_login_msg: "正在自动重新登录 EHunt…", ehunt_login_at: new Date().toISOString() },
  ok: { ehunt_login_state: "ok", ehunt_login_msg: "EHunt 已自动重新登录，3 个因登录失效失败的任务已重新排队", ehunt_login_at: new Date().toISOString() },
  expired: {},
};
export function createEhLogin({ $, esc, toast, getSb, DEMO, el = "ssLogin" }) {
  const QSP = new URLSearchParams(location.search); const demoKind = DEMO ? QSP.get("ehlogin") : null;
  const S = { st: null, failed: 0, has058: true, timer: null, sending: false };
  async function load() {
    if (demoKind) { S.st = DEMO_ST[demoKind] || null; S.failed = demoKind === "expired" ? 3 : 0; return render(); }
    if (DEMO) return;
    const sb = getSb(); if (!sb) return;
    const a = await sb.from("service_status").select("ehunt_login_state,ehunt_login_msg,ehunt_login_at,ehunt_relogin_requested_at").eq("id", 1).maybeSingle();
    if (a.error) { S.has058 = false; S.st = null; } else { S.has058 = true; S.st = a.data; }
    const since = new Date(Date.now() - 3 * 864e5).toISOString();
    const b = await sb.from("sel_runs").select("id,status,error").in("status", ["failed", "canceled"]).gte("created_at", since).limit(500);
    S.failed = b.error ? 0 : loginFailedCount(b.data); render();
  }
  function render() {
    const box = $(el); if (!box) return; const B = loginBanner(S.st, S.failed, S.has058);
    if (!B) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="banner banner-${B.kind === "ok" ? "ok" : B.kind} eh-login" role="${B.kind === "bad" ? "alert" : "status"}"><div class="eh-login-row"><div><b>${esc(B.title)}</b>${demoKind ? ' <span class="pill pill-bad">演示数据</span>' : ""}<span class="banner-sub">${esc(B.msg)}</span></div>${B.button ? `<button class="btn btn-primary btn-sm" id="ehRelogin"${S.sending ? " disabled" : ""} title="不需要输入密码：box 上的 worker 用本地保存的 EHunt 账号自动登录一次（30 分钟最多 2 次，防止账号被锁）">${S.sending ? "已发送请求…" : "重新登录 EHunt"}</button>` : ""}</div></div>`;
    $("ehRelogin")?.addEventListener("click", request);
  }
  async function request() {
    S.sending = true; render();
    if (demoKind) { S.st = DEMO_ST.relogging; S.failed = 0; S.sending = false; render(); return toast("演示：已请求重新登录 EHunt", "ok"); }
    const { error } = await getSb().rpc("ehunt_request_relogin"); S.sending = false;
    if (error) { render(); return toast(`请求失败：${error.message}`, "bad"); }
    toast("已请求 box 重新登录 EHunt，稍后自动刷新结果", "ok"); S.st = { ...(S.st || {}), ehunt_login_state: "relogging" }; render();
  }
  function start() { clearInterval(S.timer); load(); if (!demoKind && !DEMO) S.timer = setInterval(() => { if (location.hash.startsWith("#/selscreen")) load(); }, 15000); }
  return { start, load, render };
}
