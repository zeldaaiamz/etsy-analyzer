"""2026-11-04 掉线事故回归：严格登录判定 / 不用旧状态盖新 cookie / 掉线诊断 / 保活节奏 / 暂停→自动恢复 / 桌面浏览器误登录。"""
import json, os, sqlite3, time
from selection import ehunt_session as ES
from selection import ehunt_worker as W

NOW = 1_800_000_000.0
def ck(name, value="v", exp=NOW + 86400, domain=".ehunt.ai"): return {"name": name, "value": value, "domain": domain, "path": "/", "expires": exp}
GUEST = [ck("token", "guest"), ck("ZFSESSID", "z")]
LOGGED = [ck("token", "REALTOK"), ck("local_username", "USERVAL"), ck("user_permission", "p")]

# ---- 严格登录判定 ----
def test_guest_page_without_login_button_is_not_logged_in():
    assert not ES.login_confirmed(GUEST, ES.value_sig(ck("token", "guest")), now=NOW)
def test_local_username_confirms():
    assert ES.login_confirmed(LOGGED, None, now=NOW)
def test_token_change_needs_user_permission():
    base = ES.value_sig(ck("token", "guest"))
    assert not ES.login_confirmed([ck("token", "new")], base, now=NOW)
    assert ES.login_confirmed([ck("token", "new"), ck("user_permission")], base, now=NOW)
def test_expired_local_username_not_enough():
    assert not ES.login_confirmed([ck("local_username", exp=NOW - 1)], None, now=NOW)

# ---- 状态文件新旧 ----
def test_guest_state_never_applied():
    assert not ES.state_is_fresher(GUEST, [], now=NOW)
def test_stale_state_does_not_overwrite_fresher_profile():
    old = [ck("token", "o", NOW + 100), ck("local_username")]
    new = [ck("token", "n", NOW + 9000), ck("local_username")]
    assert not ES.state_is_fresher(old, new, now=NOW) and ES.state_is_fresher(new, old, now=NOW)
def test_state_applied_when_profile_logged_out():
    assert ES.state_is_fresher(LOGGED, GUEST, now=NOW)

# ---- 掉线诊断（不含值） ----
def test_server_kick_when_token_replaced_before_expiry():
    kind, facts = ES.diagnose_logout(GUEST, LOGGED, now=NOW)
    assert kind == ES.SERVER_KICK
    msg = ES.diagnosis_message(kind, facts)
    assert "别的设备" in msg and not any(v in msg for v in ("guest", "REALTOK", "USERVAL"))
def test_natural_expiry():
    saved = [ck("token", "real", NOW - 10), ck("local_username", "me", NOW - 10)]
    assert ES.diagnose_logout(GUEST, saved, now=NOW)[0] == ES.NATURAL_EXPIRY
def test_never_logged_in():
    assert ES.diagnose_logout(GUEST, [], now=NOW)[0] == ES.NEVER_LOGGED_IN
def test_facts_have_no_values():
    _, facts = ES.diagnose_logout(GUEST, LOGGED, now=NOW)
    assert all(v is None or "-" in v or v == "session" for v in facts.values())

# ---- 保活节奏 ----
def test_keepalive_due():
    assert ES.keepalive_due(NOW - 12 * 3600, now=NOW) and not ES.keepalive_due(NOW - 3600, now=NOW)
    assert not ES.keepalive_due(0, now=NOW, busy=True)

# ---- 桌面浏览器误登录检测（只读 name，不读 value） ----
def _mkdb(path, names):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path); c.execute("create table cookies (host_key text, name text, value text, expires_utc integer)")
    exp = int((time.time() + 86400 + 11644473600) * 1e6)
    for n in names: c.execute("insert into cookies values ('.ehunt.ai', ?, 'SECRET', ?)", (n, exp))
    c.commit(); c.close()
def test_desktop_browser_login_detected(tmp_path):
    _mkdb(str(tmp_path / "Fork-4/Default/Cookies"), ["token", "local_username"])
    _mkdb(str(tmp_path / "Fork-5/Default/Cookies"), ["token"])
    hits = ES.desktop_browser_ehunt_login(str(tmp_path / "*/Default/Cookies"))
    assert hits == [str(tmp_path / "Fork-4")]
def test_title_script_has_prefix():
    assert ES.LOGIN_WINDOW_TITLE in ES.title_script()

# ---- 掉线任务暂停 → 重新登录后自动恢复 ----
class Store:
    def __init__(s, rows=()): s.calls = []; s.rows = list(rows)
    def patch_run(s, rid, **kw): s.calls.append(("run", rid, kw))
    def patch_batch(s, bid, **kw): s.calls.append(("batch", bid, kw))
    def _req(s, m, path, body=None, *a, **k):
        s.calls.append((m, path, body)); return s.rows if m == "GET" else None

def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "LOGIN_ALERT", str(tmp_path / "alert.json")); W.LOGIN_WAIT["since"] = None
def test_count_login_expired_pauses_not_fails(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch); st = Store(); e = W.LoginExpired("x"); e.kind = ES.SERVER_KICK
    W.pause_for_login(st, {"id": "r1", "status": "counting"}, e)
    (_, rid, kw), = st.calls
    assert kw["status"] == "paused" and kw["progress"]["stage"] == "login_wait" and kw["progress"]["resume_as"] == "count_queued"
    assert W.LOGIN_WAIT["since"] and json.load(open(tmp_path / "alert.json"))["kind"] == ES.SERVER_KICK
def test_batch_login_expired_keeps_batch_queued(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch); st = Store()
    W.pause_for_login(st, {"id": "r1", "status": "running"}, W.LoginExpired("x"), batch={"id": "b1"})
    assert ("batch", "b1", {"status": "queued"}) in st.calls and st.calls[-1][2]["progress"]["resume_as"] == "queued"

def test_resume_only_after_relogin(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch); p = tmp_path / "state.json"; monkeypatch.setattr(W, "LOGIN_STATE", str(p))
    W.save_login_state({"cookies": GUEST, "origins": []}, str(p))
    W.LOGIN_WAIT["since"] = time.time() - 100
    st = Store([{"id": "r1", "keyword": "candy", "progress": {"stage": "login_wait", "resume_as": "count_queued"}}])
    assert W.maybe_resume_after_relogin(st) == 0          # 状态文件不是登录态 → 不恢复
    W.save_login_state({"cookies": [dict(c, expires=time.time() + 86400) for c in LOGGED], "origins": []}, str(p))
    assert W.maybe_resume_after_relogin(st) == 1
    patch = [c for c in st.calls if c[0] == "PATCH"][0]
    assert patch[2]["status"] == "count_queued" and "status=eq.paused" in patch[1] and W.LOGIN_WAIT["since"] is None
    assert not os.path.exists(tmp_path / "alert.json")

def test_login_expired_error_carries_kind(monkeypatch):
    monkeypatch.setattr(W, "load_login_state", lambda *a, **k: {"cookies": LOGGED})
    e = W.login_expired_error(GUEST)
    assert e.kind == ES.SERVER_KICK and "deploy/box-selection-worker.sh login" in str(e) and "guest" not in str(e)
