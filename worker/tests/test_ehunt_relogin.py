"""058 EHunt 自动重新登录：LoginExpired → 自动登录成功 → 任务重排；验证码 → 停止并提示（暂停队列）；30 分钟最多 2 次冷却；
网页按钮请求；凭据只读本地 600 文件且不出现在日志/上报里。全部假 store / 假浏览器，不碰真实 profile、不启动 chrome。"""
import os, re, pathlib, pytest
from selection import relogin as R
from selection import ehunt_worker as W

SECRET = "S3cr3t-pw!"

class Store:
    def __init__(self, req_at=None, state=None):
        self.row = {"ehunt_relogin_requested_at": req_at, "ehunt_login_state": state}; self.calls = []; self.reports = []
    def _req(self, method, path, body=None, prefer=None, retries=None, timeout=None):
        self.calls.append((method, path, body))
        if method == "GET" and "service_status" in path: return [dict(self.row)]
        if method == "PATCH" and "service_status" in path: self.reports.append(body); self.row["ehunt_login_state"] = body["ehunt_login_state"]; return None
        if "ehunt_requeue_login_failed" in path: return {"count": 2, "batch": 1, "lookup": 3}
        raise AssertionError(path)
    def requeued(self): return sum("ehunt_requeue_login_failed" in c[1] for c in self.calls)

class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t

def mk(tmp_path, login_fn, store=None, clock=None):
    clock = clock or Clock(); store = store or Store()
    cd = R.Cooldown(path=str(tmp_path / "relogin.json"), max_tries=2, window=1800, clock=clock)
    rl = R.Relogin(store, login_fn, cooldown=cd); rl.tick()   # 第一次 tick = 初始化（读库）
    return rl, store, clock

def test_login_expired_then_auto_login_requeues(tmp_path):
    n = []; rl, st, _ = mk(tmp_path, lambda: n.append(1) or "logged_in")
    assert rl.tick() is None and not n                 # 没有失效也没有请求：什么都不做
    rl.note_expired(); assert rl.tick() == "ok"
    assert n == [1] and st.requeued() == 1 and rl.state == "ok" and not rl.paused()
    assert [r["ehunt_login_state"] for r in st.reports] == ["relogging", "ok"]
    assert "6 个因登录失效失败的任务已重新排队" in st.reports[-1]["ehunt_login_msg"]
    assert rl.tick() is None                           # 只登录一次

def test_captcha_stops_and_pauses(tmp_path):
    n = []
    def fn(): n.append(1); raise R.NeedsManual(f"{R.MANUAL}：登录页出现验证码/人机验证")
    rl, st, _ = mk(tmp_path, fn)
    rl.note_expired(); assert rl.tick() == "needs_manual"
    assert rl.paused() and st.requeued() == 0 and "EHunt 需要人工登录" in st.reports[-1]["ehunt_login_msg"]
    for _ in range(5): rl.note_expired(); assert rl.tick() is None   # 后续失效不再自动重试（不刷屏、不锁号）
    assert n == [1] and rl.paused()

def test_cooldown_max_two_in_30min(tmp_path):
    n = []
    def fn(): n.append(1); raise RuntimeError("timeout")
    rl, st, clk = mk(tmp_path, fn)
    rl.note_expired(); rl.tick(); rl.state = "ok"     # 模拟：出错后人工点了按钮之类，依然受上限约束
    st.row["ehunt_relogin_requested_at"] = "t1"; assert rl.tick() == "needs_manual"
    st.row["ehunt_relogin_requested_at"] = "t2"; assert rl.tick() == "cooldown"
    assert len(n) == 2 and rl.paused() and "已尝试 2 次" in st.reports[-1]["ehunt_login_msg"]
    clk.t += 1801                                       # 冷却结束 → 自动再试一次
    assert rl.tick() == "needs_manual" and len(n) == 3

def test_cooldown_persists_across_restart(tmp_path):
    clk = Clock(); cd = R.Cooldown(path=str(tmp_path / "r.json"), max_tries=2, window=1800, clock=clk)
    cd.record(); cd.record()
    assert not R.Cooldown(path=str(tmp_path / "r.json"), max_tries=2, window=1800, clock=clk).allowed()
    clk.t += 1801; assert cd.allowed()

def test_web_button_request_after_manual_login(tmp_path):
    rl, st, _ = mk(tmp_path, lambda: "already", store=Store(req_at="old", state="needs_manual"))
    assert rl.paused()                                 # 重启后沿用 needs_manual，旧请求不重复处理
    assert rl.tick() is None and rl.paused()
    st.row["ehunt_relogin_requested_at"] = "new"
    assert rl.tick() == "ok" and not rl.paused() and st.requeued() == 1
    assert "已确认登录" in st.reports[-1]["ehunt_login_msg"]

def test_creds_file_perms_and_no_leak(tmp_path, caplog):
    f = tmp_path / "ehunt.env"; f.write_text(f'EHUNT_EMAIL=a@b.c\nexport EHUNT_PASSWORD="{SECRET}"\n'); os.chmod(f, 0o644)
    with pytest.raises(R.NeedsManual, match="chmod 600"): R.load_creds(str(f), env={})
    os.chmod(f, 0o600); assert R.load_creds(str(f), env={}) == ("a@b.c", SECRET)
    with pytest.raises(R.NeedsManual, match="不存在"): R.load_creds(str(tmp_path / "none"), env={})
    assert R.load_creds(str(tmp_path / "none"), env={"EHUNT_EMAIL": "x", "EHUNT_PASSWORD": "y"}) == ("x", "y")
    # 整个登录流程（假浏览器，密码错误）里密码不进日志、不进上报
    st = Store(); pg = FakePage(after_submit_text="Incorrect password")
    rl = R.Relogin(st, lambda: R.browser_login(creds_fn=lambda: ("a@b.c", SECRET), check_logged_in=lambda c: False, launch=lambda p: FakeCtx(pg), sync_pw=FakePW, settle=1),
                   cooldown=R.Cooldown(path=str(tmp_path / "c.json"), clock=Clock())); rl.tick()
    rl.note_expired(); assert rl.tick() == "needs_manual" and "密码被拒绝" in rl.msg
    assert pg.filled == ["a@b.c", SECRET]
    assert SECRET not in caplog.text and SECRET not in repr(st.calls)

# ---- 假 Playwright ----
class Loc:
    def __init__(self, pg, sel): self.pg, self.sel = pg, sel; self.first = self
    def count(self): return 1 if self.pg.visible(self.sel) else 0
    def nth(self, i): return self
    def is_visible(self): return self.pg.visible(self.sel)
    def inner_text(self, timeout=None): return self.pg.text
    def fill(self, v, timeout=None): self.pg.filled.append(v)
    def click(self, timeout=None): self.pg.text = self.pg.after; self.pg.url = self.pg.after_url
class FakePage:
    def __init__(self, text="Log in", after_submit_text="", captcha=False, after_url="https://ehunt.ai/login"):
        self.text, self.after, self.captcha, self.after_url, self.url, self.filled = text, after_submit_text, captcha, after_url, "", []
    def visible(self, sel): return self.captcha and "captcha" in sel
    def goto(self, u, **k): self.url = u
    def locator(self, sel): return Loc(self, sel)
class FakeCtx:
    def __init__(self, pg): self.pg = pg; self.closed = False
    def new_page(self): return self.pg
    def close(self): self.closed = True
class FakePW:
    def __enter__(self): return self
    def __exit__(self, *a): return False

@pytest.fixture(autouse=True)
def _nosleep(monkeypatch): monkeypatch.setattr(R.time, "sleep", lambda s: None)

def test_browser_login_captcha_does_not_fill(tmp_path):
    pg = FakePage(captcha=True); ctx = FakeCtx(pg)
    with pytest.raises(R.NeedsManual, match="验证码"):
        R.browser_login(creds_fn=lambda: ("a", "b"), check_logged_in=lambda c: False, launch=lambda p: ctx, sync_pw=FakePW)
    assert pg.filled == [] and ctx.closed

def test_browser_login_sms_code_after_submit():
    pg = FakePage(after_submit_text="Please enter the verification code sent to your phone")
    with pytest.raises(R.NeedsManual, match="短信/邮箱验证码"):
        R.browser_login(creds_fn=lambda: ("a", "b"), check_logged_in=lambda c: False, launch=lambda p: FakeCtx(pg), sync_pw=FakePW, settle=2)

def test_browser_login_success_and_already():
    pg = FakePage(after_url="https://ehunt.ai/etsy-product-research"); seen = []
    ok = lambda c: (seen.append(1), len(seen) > 1)[1]   # 第一次未登录，提交后已登录
    assert R.browser_login(creds_fn=lambda: ("a", "b"), check_logged_in=ok, launch=lambda p: FakeCtx(pg), sync_pw=FakePW) == "logged_in"
    called = []
    assert R.browser_login(creds_fn=lambda: called.append(1), check_logged_in=lambda c: True, launch=lambda p: FakeCtx(FakePage()), sync_pw=FakePW) == "already"
    assert called == []                                  # 已登录：不读凭据、不填表

def test_worker_paths_signal_expired(monkeypatch):
    """handle_run（查询数量）遇到 LoginExpired → 通知自动登录；_open_list 跳转登录页现在也抛 LoginExpired。"""
    hits = []; monkeypatch.setattr(W, "ON_EXPIRED", [lambda: hits.append(1)])
    monkeypatch.setattr(W, "count_ehunt", lambda kw, p: (_ for _ in ()).throw(W.LoginExpired(W.LOGIN_MSG)))
    class S:
        def __init__(self): self.p = []
        def patch_run_if(self, rid, exp, **kw): self.p.append(kw)
    s = S(); W.handle_run(s, {"id": "r1", "status": "counting", "keyword": "x"}, type("A", (), {"fake": None})(), False)
    assert hits == [1] and s.p[0]["status"] == "failed" and "EHunt 登录已失效" in s.p[0]["error"]
    src = pathlib.Path(W.__file__).read_text()
    assert 'raise RuntimeError("EHunt 登录已失效' not in src

def test_main_loop_paused_skips_claim(monkeypatch):
    claims = []
    class St:
        def __init__(self): pass
        def purge(self): return 0
        def lookup_purge(self): return 0
        def exclude_words(self): return []
        def claim(self): claims.append(1); return None
    class RL:
        def tick(self): return "needs_manual"
        def paused(self): return True
    monkeypatch.setattr(W, "Store", St); monkeypatch.setattr(W, "start_heartbeat", lambda s: None)
    monkeypatch.setattr(W, "make_relogin", lambda s: RL()); monkeypatch.setattr(W, "serve_lookups_browser", lambda s: (_ for _ in ()).throw(AssertionError("lookups while paused")))
    monkeypatch.setattr(W.time, "sleep", lambda s: None); monkeypatch.setattr(W, "LOOKUPS_ON", True)
    monkeypatch.setattr(W, "PHASE", ["idle"])
    W.main(["--once"]); assert claims == [] and W.PHASE[0] == "login_paused"

def test_migration_058():
    sql = (pathlib.Path(W.__file__).resolve().parents[2] / "supabase/migrations/058_ehunt_relogin.sql").read_text()
    assert "ehunt_request_relogin" in sql and "grant execute on function public.ehunt_request_relogin() to authenticated" in sql
    assert re.search(r"ehunt_requeue_login_failed\(\) to service_role", sql) and "from public, anon, authenticated" in sql
    assert "password" not in sql.lower()
