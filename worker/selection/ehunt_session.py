"""EHunt 登录会话：严格登录判定 / 状态文件新旧比较 / 掉线诊断 / 保活节奏 / 桌面浏览器误登录检测。
全部是纯函数或只读操作，绝不记录或返回 cookie 值（只看 name/domain/expires，比较值时只用 hash）。

背景（2026-11-04 事故）：
- 服务器作废会话后会下发新的游客 token（无 local_username/user_permission），旧判定只看“顶栏 Login 消失”→ 误报“已登录已保存”；
- 人工登录两次都落在 box 桌面浏览器（/home/box/chrome-profile/Fork-*），脚本自己的 ehunt-profile 一直是游客；
- 启动时无条件把旧 ehunt-login-state.json 注入，会盖掉 profile 里服务器刷新过的 cookie。
"""
import glob, hashlib, os, shutil, sqlite3, tempfile, time

LOGIN_COOKIE = "local_username"      # 只有真正登录后才有（游客 token 没有）
PERM_COOKIE = "user_permission"
TOKEN_COOKIE = "token"
KEEPALIVE_SECONDS = 12 * 3600
LOGIN_WINDOW_TITLE = "【EHunt 登录专用窗口】"

def _ehunt(cookies): return [c for c in (cookies or []) if "ehunt" in (c.get("domain") or "")]
def _get(cookies, name):
    for c in _ehunt(cookies):
        if c.get("name") == name: return c
    return None
def _alive(c, now):
    if not c: return False
    e = c.get("expires", -1)
    return e is None or e < 0 or e > now
def value_sig(c):
    """cookie 值的短 hash（只用于比较是否变化，不可逆，不落日志原值）。"""
    v = (c or {}).get("value")
    return hashlib.sha256(v.encode()).hexdigest()[:12] if isinstance(v, str) and v else None

def has_login_cookie(cookies, now=None):
    return _alive(_get(cookies, LOGIN_COOKIE), now or time.time())

def login_confirmed(cookies, baseline_token_sig=None, now=None):
    """严格判定：必须有未过期的 local_username；或 token 相对开窗时变化且带 user_permission。
    只看顶栏 Login 消失不算（游客页也可能暂时没有 Login 按钮）。"""
    now = now or time.time()
    if has_login_cookie(cookies, now): return True
    tok = _get(cookies, TOKEN_COOKIE)
    return bool(tok and baseline_token_sig and value_sig(tok) and value_sig(tok) != baseline_token_sig
                and _alive(_get(cookies, PERM_COOKIE), now))

def _exp(c):
    e = (c or {}).get("expires", -1)
    return float(e) if e is not None and e >= 0 else float("inf") if c else -1.0

def state_is_fresher(state_cookies, profile_cookies, now=None):
    """启动时是否应把状态文件注入浏览器：
    - 状态文件里没有有效 local_username（不是登录态）→ 不注入；
    - profile 已有有效登录且其 token 不比状态文件旧 → 不注入（不拿旧的盖新的）；
    - 其余（profile 未登录，或状态文件 token 更晚到期）→ 注入。"""
    now = now or time.time()
    if not has_login_cookie(state_cookies, now): return False
    if not has_login_cookie(profile_cookies, now): return True
    return _exp(_get(state_cookies, TOKEN_COOKIE)) > _exp(_get(profile_cookies, TOKEN_COOKIE))

SERVER_KICK, NATURAL_EXPIRY, NEVER_LOGGED_IN, UNKNOWN = "server_kick", "natural_expiry", "never_logged_in", "unknown"
KIND_ZH = {
    SERVER_KICK: "服务器提前作废了会话（token 未到期就被换成游客 token，常见原因：同一账号在别的设备/浏览器登录，例如在 Mac 上登录会把 box 挤掉）",
    NATURAL_EXPIRY: "登录自然过期（保存的 token / local_username 已过到期时间）",
    NEVER_LOGGED_IN: "这个 profile 从未保存过有效登录（可能登录在了桌面浏览器而不是脚本的登录窗口）",
    UNKNOWN: "原因不明（cookie 看起来仍有效，但页面显示未登录）",
}

def diagnose_logout(page_cookies, saved_state_cookies, now=None):
    """页面判定未登录时分类原因。返回 (kind, facts)；facts 只含 name→是否存在/到期时间，无值。"""
    now = now or time.time()
    lu, tok = _get(page_cookies, LOGIN_COOKIE), _get(page_cookies, TOKEN_COOKIE)
    s_lu, s_tok = _get(saved_state_cookies, LOGIN_COOKIE), _get(saved_state_cookies, TOKEN_COOKIE)
    fmt = lambda c: None if not c else ("session" if _exp(c) == float("inf") else time.strftime("%F %T", time.localtime(_exp(c))))
    facts = {"page_local_username": fmt(lu), "page_token_expires": fmt(tok), "page_user_permission": fmt(_get(page_cookies, PERM_COOKIE)),
             "saved_local_username": fmt(s_lu), "saved_token_expires": fmt(s_tok)}
    if not s_lu and not s_tok: kind = NEVER_LOGGED_IN
    elif (s_tok and 0 <= _exp(s_tok) <= now) or (s_lu and 0 <= _exp(s_lu) <= now): kind = NATURAL_EXPIRY
    elif not lu and tok and s_tok and value_sig(tok) != value_sig(s_tok): kind = SERVER_KICK
    elif not lu and not tok: kind = SERVER_KICK
    else: kind = UNKNOWN
    return kind, facts

def diagnosis_message(kind, facts):
    parts = [f"{k}={v or '无'}" for k, v in facts.items()]
    return f"EHunt 已退出登录：{KIND_ZH.get(kind, kind)}。cookie 概况（不含值）：" + "，".join(parts)

def keepalive_due(last_ok_at, now=None, every=KEEPALIVE_SECONDS, busy=False):
    """空闲且距上次成功检查/抓取 ≥ every 秒才保活；正在跑任务时不保活（任务本身就会刷新会话）。"""
    if busy: return False
    return (now or time.time()) - (last_ok_at or 0) >= every

def _cookie_names_in_db(db_path):
    """只读复制 Chrome Cookies 库，返回 ehunt 域上的 (name, expires_unix)；不读 value 列。"""
    out = []
    if not os.path.exists(db_path): return out
    tmp = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy2(db_path, tmp)
        con = sqlite3.connect(tmp)
        for name, exp in con.execute("select name, expires_utc from cookies where host_key like '%ehunt%'"):
            out.append((name, (exp / 1e6 - 11644473600) if exp else -1))
        con.close()
    except Exception: pass
    finally:
        try: os.unlink(tmp)
        except OSError: pass
    return out

def desktop_browser_ehunt_login(glob_pat="/home/box/chrome-profile/*/Default/Cookies", now=None):
    """box 桌面浏览器（Fork-*）里是否有 EHunt 登录 cookie：有 → 人可能登错了窗口。返回有登录的 profile 目录列表。"""
    now = now or time.time(); hits = []
    for db in glob.glob(glob_pat):
        if any(n == LOGIN_COOKIE and (e < 0 or e > now) for n, e in _cookie_names_in_db(db)):
            hits.append(os.path.dirname(os.path.dirname(db)))
    return hits

def title_script(): return ("(() => { const P = %r; const fix = () => { if (!document.title.startsWith(P)) document.title = P + document.title; };"
                            " try { fix(); new MutationObserver(fix).observe(document.documentElement, {subtree: true, childList: true, characterData: true}); } catch (e) {} })();" % LOGIN_WINDOW_TITLE)
