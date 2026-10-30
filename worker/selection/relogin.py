"""EHunt 自动重新登录（058，Hongyan 2026-10-30 同意方案）。

触发：① worker 遇到 LoginExpired（note_expired()）② 网页「重新登录 EHunt」按钮（service_status.ehunt_relogin_requested_at）。
凭据：只从 box 本地读取 —— 环境变量 EHUNT_EMAIL / EHUNT_PASSWORD，或 EHUNT_CRED_FILE（默认 ~/.config/etsy-selection/ehunt.env，必须权限 600）。
      不进 git、不进 Supabase、不打印、不写日志（日志里只出现“已配置/未配置”）。
登录：在 worker 同一个 Playwright 持久化 profile（EHUNT_PROFILE_DIR）里打开登录页填表；先检查是否已经是登录状态（人工登录后点按钮 = 只确认）。
防锁号：30 分钟内最多 2 次尝试（EHUNT_RELOGIN_MAX / EHUNT_RELOGIN_WINDOW_S），记录在 box 本地 $DATA/relogin.json（跨进程重启保留）。
无法自动登录（验证码 / 短信或邮箱验证 / 密码错误 / 没配置凭据 / 次数用完）→ needs_manual：暂停选品队列与主图查询，
      网页显示「EHunt 需要人工登录」；人工登录后点按钮再确认一次即恢复。
成功 → ehunt_requeue_login_failed()：只重排因登录失效而失败的任务（登录失效 = 获取失败，可重排）。
"""
import json, logging, os, stat, time

log = logging.getLogger("selection.relogin")
DATA = os.path.expanduser(os.environ.get("SEL_DATA_DIR", "~/.local/share/etsy-selection"))
CRED_FILE = os.path.expanduser(os.environ.get("EHUNT_CRED_FILE", "~/.config/etsy-selection/ehunt.env"))
MAX_TRIES = int(os.environ.get("EHUNT_RELOGIN_MAX", "2"))
WINDOW_S = float(os.environ.get("EHUNT_RELOGIN_WINDOW_S", "1800"))
NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
MANUAL = "EHunt 需要人工登录"

# 登录页选择器（可用 EHUNT_LOGIN_UI JSON 覆盖；EHunt 改版时只改环境变量）
LOGIN_UI = {
    "url": "https://ehunt.ai/login",
    "email": "input[type=email], input[name=email], input[autocomplete=username], input[placeholder*='mail' i]",
    "password": "input[type=password]",
    "submit": "button[type=submit], button:has-text('Log in'), button:has-text('Login'), button:has-text('Sign in'), button:has-text('登录')",
    "captcha": "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], iframe[src*='geetest'], [class*='captcha' i], [id*='captcha' i], .geetest_panel",
    "captcha_text": r"(?i)verification code|verify (it'?s|you are) (you|human)|enter the code|sms code|短信|验证码|人机验证",
    "error_text": r"(?i)incorrect|wrong password|invalid (email|password|account)|password is wrong|账号或密码|密码错误|account (is )?(locked|disabled)|too many",
}
def login_ui(): return {**LOGIN_UI, **json.loads(os.environ.get("EHUNT_LOGIN_UI", "{}"))}

class NeedsManual(RuntimeError): pass

# ---------------- 凭据 ----------------
def _parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); k = k.strip().removeprefix("export ").strip(); v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'": v = v[1:-1]
        out[k] = v
    return out

def load_creds(path=None, env=None):
    """返回 (email, password) 或抛 NeedsManual（原因不含任何凭据内容）。环境变量优先，其次本地文件（必须 600 / 不可被组和其他人读）。"""
    env = os.environ if env is None else env
    if env.get("EHUNT_EMAIL") and env.get("EHUNT_PASSWORD"): return env["EHUNT_EMAIL"], env["EHUNT_PASSWORD"]
    path = path or CRED_FILE
    if not os.path.exists(path): raise NeedsManual(f"{MANUAL}：box 上未配置 EHunt 账号（{path} 不存在）")
    if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO): raise NeedsManual(f"{MANUAL}：凭据文件权限过宽，需 chmod 600 {path}")
    d = _parse_env(open(path, encoding="utf-8").read())
    if not d.get("EHUNT_EMAIL") or not d.get("EHUNT_PASSWORD"): raise NeedsManual(f"{MANUAL}：{path} 缺少 EHUNT_EMAIL / EHUNT_PASSWORD")
    return d["EHUNT_EMAIL"], d["EHUNT_PASSWORD"]

# ---------------- 冷却 / 次数上限 ----------------
class Cooldown:
    def __init__(self, path=None, max_tries=None, window=None, clock=time.time):
        self.path = path or os.path.join(DATA, "relogin.json"); self.max = MAX_TRIES if max_tries is None else max_tries
        self.window = WINDOW_S if window is None else window; self.clock = clock
    def _load(self):
        try: return [float(t) for t in json.load(open(self.path)).get("tries", [])]
        except Exception: return []
    def recent(self): now = self.clock(); return [t for t in self._load() if now - t < self.window]
    def allowed(self): return len(self.recent()) < self.max
    def wait_s(self): r = self.recent(); return 0 if len(r) < self.max else max(0, int(min(r) + self.window - self.clock()))
    def record(self):
        r = self.recent() + [self.clock()]; os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"; json.dump({"tries": r}, open(tmp, "w")); os.replace(tmp, self.path)

# ---------------- 浏览器登录（同一 profile）----------------
def _any_visible(pg, sel):
    try:
        loc = pg.locator(sel); return any(loc.nth(i).is_visible() for i in range(loc.count()))
    except Exception: return False

def _body(pg):
    try: return pg.locator("body").inner_text(timeout=3000)
    except Exception: return ""

def detect_block(pg, L=None):
    """验证码 / 短信验证 / 密码错误 → 返回中文原因；否则 None。"""
    import re
    L = L or login_ui()
    if _any_visible(pg, L["captcha"]): return f"{MANUAL}：登录页出现验证码/人机验证"
    t = _body(pg)
    if re.search(L["captcha_text"], t): return f"{MANUAL}：EHunt 要求短信/邮箱验证码"
    if re.search(L["error_text"], t): return f"{MANUAL}：账号或密码被拒绝（请检查 box 上的凭据文件）"
    return None

def browser_login(creds_fn=load_creds, check_logged_in=None, launch=None, sync_pw=None, L=None, settle=8.0):
    """在 worker 的 Playwright profile 里自动登录。成功返回 'already' / 'logged_in'；失败抛 NeedsManual 或其他异常。
    check_logged_in(ctx) → bool：打开产品研究页确认登录状态（复用 ehunt_worker 的顶栏/弹窗检测）。"""
    L = L or login_ui()
    if sync_pw is None: from playwright.sync_api import sync_playwright as sync_pw
    with sync_pw() as p:
        ctx = launch(p)
        try:
            if check_logged_in(ctx): return "already"
            email, pw = creds_fn()
            pg = ctx.new_page(); pg.goto(L["url"], wait_until="domcontentloaded", timeout=60000); time.sleep(2)
            why = detect_block(pg, L)
            if why: raise NeedsManual(why)
            pg.locator(L["email"]).first.fill(email, timeout=15000)
            pg.locator(L["password"]).first.fill(pw, timeout=15000)
            del pw
            pg.locator(L["submit"]).first.click(timeout=15000)
            t0 = time.time()
            while time.time() - t0 < settle:
                time.sleep(1)
                why = detect_block(pg, L)
                if why: raise NeedsManual(why)
                if "login" not in pg.url: break
            if not check_logged_in(ctx):
                raise NeedsManual(detect_block(pg, L) or f"{MANUAL}：自动登录后仍未登录（可能需要验证或页面改版）")
            return "logged_in"
        finally:
            try: ctx.close()
            except Exception: pass

# ---------------- 状态机（worker 主循环调用）----------------
class Relogin:
    """state: ok | relogging | needs_manual | cooldown。paused() = True 时主循环不认领新任务、不处理主图查询（不刷屏失败）。"""
    def __init__(self, store, login_fn, cooldown=None, clock=time.time):
        self.store, self.login_fn, self.cd, self.clock = store, login_fn, cooldown or Cooldown(), clock
        self.expired = False; self.state = "ok"; self.msg = None; self.seen_req = None; self.init = False
    def note_expired(self): self.expired = True
    def paused(self): return self.state in ("needs_manual", "cooldown")
    def _report(self, state, msg):
        self.state, self.msg = state, msg
        try: self.store._req("PATCH", "/rest/v1/service_status?id=eq.1", {"ehunt_login_state": state, "ehunt_login_msg": msg, "ehunt_login_at": NOW()}, "return=minimal", retries=1)
        except Exception as e: log.warning("report login state failed (058 not applied?): %s", str(e)[:120])
    def _requested(self):
        try: rows = self.store._req("GET", "/rest/v1/service_status?id=eq.1&select=ehunt_relogin_requested_at,ehunt_login_state", retries=1) or []
        except Exception: return None
        row = rows[0] if rows else {}
        if not self.init:   # 启动时：沿用库里的 needs_manual（重启不清除暂停）；旧请求视为已处理
            self.init = True; self.seen_req = row.get("ehunt_relogin_requested_at")
            if row.get("ehunt_login_state") == "needs_manual": self.state = "needs_manual"
            return None
        r = row.get("ehunt_relogin_requested_at")
        if r and r != self.seen_req: self.seen_req = r; return r
        return None
    def tick(self):
        """每轮主循环调用一次。返回本轮动作：None / 'ok' / 'needs_manual' / 'cooldown'。"""
        req = self._requested()
        if not (self.expired or req): 
            if self.state == "cooldown" and self.cd.allowed(): self.expired = True   # 冷却结束 → 自动再试
            else: return None
        manual = bool(req)
        if self.state == "needs_manual" and not manual: self.expired = False; return None   # 需要人工：只等按钮，不自动重试
        if not self.cd.allowed():
            w = self.cd.wait_s(); self.expired = False
            self._report("cooldown", f"{MANUAL}：30 分钟内自动登录已尝试 {self.cd.max} 次（防止账号被锁），约 {max(1, w // 60)} 分钟后可再试")
            return "cooldown"
        self.cd.record(); self._report("relogging", "正在自动重新登录 EHunt…" + ("（网页请求）" if manual else ""))
        try:
            how = self.login_fn()
        except NeedsManual as e:
            self.expired = False; self._report("needs_manual", str(e)[:300]); log.warning("relogin: %s", e); return "needs_manual"
        except Exception as e:
            self.expired = False; self._report("needs_manual", f"{MANUAL}：自动登录出错（{type(e).__name__}）"); log.warning("relogin error", exc_info=True); return "needs_manual"
        self.expired = False
        try: n = self.store._req("POST", "/rest/v1/rpc/ehunt_requeue_login_failed", {}, retries=1) or {}
        except Exception as e: n = {}; log.warning("requeue login-failed failed: %s", str(e)[:120])
        tot = sum(int(v or 0) for v in n.values()) if isinstance(n, dict) else 0
        self._report("ok", f"EHunt 已{'确认登录' if how == 'already' else '自动重新登录'}，{tot} 个因登录失效失败的任务已重新排队")
        log.info("relogin %s; requeued %s", how, n); return "ok"
