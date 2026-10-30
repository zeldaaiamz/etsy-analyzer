import json, os, stat, time
from selection import ehunt_worker as W

def test_save_converts_session_and_filters(tmp_path):
    p = tmp_path / "st.json"
    st = {"cookies": [{"name": "token", "domain": "ehunt.ai", "value": "v", "path": "/", "expires": -1},
                      {"name": "x", "domain": "other.com", "value": "v", "path": "/", "expires": -1}],
          "origins": [{"origin": "https://ehunt.ai", "localStorage": [{"name": "a", "value": "b"}]}, {"origin": "https://o.com", "localStorage": []}]}
    d = W.save_login_state(st, str(p))
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [c["name"] for c in d["cookies"]] == ["token"] and d["cookies"][0]["expires"] > time.time() + 6 * 86400
    assert len(d["origins"]) == 1 and W.load_login_state(str(p))["cookies"][0]["name"] == "token"

def test_load_missing_or_expired(tmp_path):
    assert W.load_login_state(str(tmp_path / "none.json")) is None
    p = tmp_path / "s.json"; p.write_text(json.dumps({"cookies": [{"name": "t", "domain": "ehunt.ai", "expires": 1}], "origins": []}))
    assert W.load_login_state(str(p)) is None

class Ctx:
    def __init__(s): s.cl, s.added, s.scripts = [], [], []
    def cookies(s): return [{"name": "token", "domain": ".ehunt.ai"}]
    def clear_cookies(s, **k): s.cl.append(k)
    def add_cookies(s, c): s.added += c
    def add_init_script(s, js): s.scripts.append(js)

def test_apply_overrides_stale(tmp_path):
    p = tmp_path / "s.json"
    W.save_login_state({"cookies": [{"name": "token", "domain": "ehunt.ai", "value": "v", "path": "/", "expires": -1}],
                        "origins": [{"origin": "https://ehunt.ai", "localStorage": [{"name": "a", "value": "b"}]}]}, str(p))
    c = Ctx(); assert W.apply_login_state(c, str(p))
    assert c.cl == [{"name": "token", "domain": ".ehunt.ai"}] and c.added[0]["name"] == "token" and len(c.scripts) == 1

def test_apply_noop_without_file(tmp_path):
    c = Ctx(); assert not W.apply_login_state(c, str(tmp_path / "no.json")) and not c.added

def test_cookie_meta_has_no_value():
    m = W._cookie_meta({"name": "token", "domain": "ehunt.ai", "value": "SECRET", "expires": -1})
    assert "SECRET" not in repr(m) and m[3] is True
