"""获取链接 worker（按需）：认领 sel_runs → Playwright 已登录 EHunt 的浏览器逐条读列表页 → 每条立即分类 → 写 sel_items。
读页面 DOM，不调用 EHunt API。部署设计见 deploy/etsy-selection-worker.service 与 tools/etsy_selection/README.md（未部署）。

运行位置：本 box（不是 ECS）。自己的 Playwright 持久化目录，不连接/不复制桌面浏览器的 cookie 或 profile。
首次：python -m selection.ehunt_worker --login   # 在 box 显示器（DISPLAY，默认 :1）打开有头浏览器到 EHunt 登录页，人工登录后关闭窗口即保存
运行：python -m selection.ehunt_worker            # 常驻轮询（deploy/box-selection-worker.sh 用 nohup 启动）
      python -m selection.ehunt_worker --fake list.json --once   # 不开浏览器，用回放 JSON 模拟抓取（测试）
环境：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY、ARK_API_KEY（分类也在 box 上跑）、EHUNT_PROFILE_DIR（默认 ~/.local/share/etsy-selection/ehunt-profile）；可选 EHUNT_UI（界面选择器 JSON）、EHUNT_FILTERS（筛选值 JSON）
"""
import argparse, json, os, socket, sys, time, logging, urllib.request, urllib.error, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "etsy_selection"))
import classify as C  # noqa: E402  审定默认：convertible + optional-custom=keep + temperature 0 + 3 次投票

log = logging.getLogger("selection")
WID = f"sel-{socket.gethostname()}-{os.getpid()}"
POLL = 5
PROFILE = os.environ.get("EHUNT_PROFILE_DIR", os.path.expanduser("~/.local/share/etsy-selection/ehunt-profile"))
LOGDIR = os.environ.get("SEL_LOG_DIR", os.path.expanduser("~/.local/share/etsy-selection/logs"))
NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# ---- 瞬时网络错误重试（2026-10-19 23:01：Supabase PATCH RemoteDisconnected 直接让整批失败） ----
import random, http.client, socket as _socket
RETRIES = max(1, int(os.environ.get("SEL_HTTP_RETRIES", "5")))
BACKOFF_BASE = float(os.environ.get("SEL_HTTP_BACKOFF", "1.0"))
_sleep = time.sleep
def is_transient(e):
    if isinstance(e, urllib.error.HTTPError): return e.code == 429 or e.code >= 500
    if isinstance(e, (http.client.RemoteDisconnected, http.client.IncompleteRead, ConnectionError, TimeoutError, _socket.timeout)): return True
    if isinstance(e, urllib.error.URLError): return True      # DNS / 连接被拒 / 超时（reason 为 OSError）
    return False
def _brief(e): return f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else f"{type(e).__name__}: {str(e)[:120]}"

# ---- 实时进度（034 sel_runs.progress jsonb）：网页“获取链接”进度条读这里 ----
# stage：count_open 打开 EHunt → count_filter 设置关键词/筛选 → count_read 读取结果数（查询数量）；
#        read_setup 设置筛选/跳页 → reading 逐行读列表页（同时豆包判断）→ vision 列表读完、等豆包判断剩余条目 → done
# 字段：read 本批已从 EHunt 读出的行数（含续读前已落定的）；judged 已判定（= scraped_count）；vision_pending 豆包判断中；
#       passed / excluded / dup；page 当前页；target 本批目标条数；at 写入时间（UTC，网页判断 worker 是否还活着）。
PROGRESS_MIN_INTERVAL = float(os.environ.get("SEL_PROGRESS_SECONDS", "2"))   # 节流：同一阶段内最多每 2 秒写一次
class Progress:
    def __init__(self, store, rid, min_interval=PROGRESS_MIN_INTERVAL, clock=time.monotonic):
        self.store, self.rid, self.min, self.clock = store, rid, min_interval, clock
        self.state, self.last, self.writes = {}, None, 0
    def update(self, force=False, **kw):
        stage_changed = "stage" in kw and kw["stage"] != self.state.get("stage")
        self.state.update(kw)
        now = self.clock()
        if not (force or stage_changed or self.last is None or now - self.last >= self.min): return False
        self.last = now
        if not PROGRESS_ON[0]: return False
        try: self.store.patch_run(self.rid, progress={**self.state, "at": NOW()}); self.writes += 1; return True
        except urllib.error.HTTPError as e:
            if e.code == 400:   # 034 未执行：progress 列不存在 → 本进程停写，不影响抓取
                log.warning("progress column unavailable (migration 034 not applied?), progress writes disabled"); PROGRESS_ON[0] = False
            else: log.warning("progress write failed: HTTP %s", e.code)
        except Exception: log.warning("progress write failed", exc_info=True)
        return False
PROGRESS_ON = [True]
_PROG = [None]   # 当前任务的 Progress（只在主线程读写）
def stage(name, **kw):
    if _PROG[0]: _PROG[0].update(stage=name, **kw)

class Store:
    """最小 PostgREST 客户端（service_role，只在 box 的 worker.env 中；绝不打印）。只写：run/batch 汇总数 + 符合要求的 sel_items。"""
    def __init__(self):
        self.u = os.environ["SUPABASE_URL"].rstrip("/"); self.k = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    def _req(self, method, path, body=None, prefer="return=representation", retries=None):
        """瞬时网络错误（断连/超时/5xx/429）指数退避重试，默认共 RETRIES 次；PATCH/幂等插入可安全重试。
        非幂等的认领（sel_claim_run / ehunt_lookup_claim）传 retries=1：重试可能多认领一条，交给超时回收。"""
        data = None if body is None else json.dumps(body, default=str).encode()
        tries = retries or RETRIES
        for a in range(tries):
            r = urllib.request.Request(self.u + path, data, method=method,
                                       headers={"apikey": self.k, "Authorization": "Bearer " + self.k, "Content-Type": "application/json", "Prefer": prefer})
            try:
                with urllib.request.urlopen(r, timeout=60) as f:
                    t = f.read(); return json.loads(t) if t else None
            except Exception as e:
                if not is_transient(e) or a == tries - 1: raise
                d = min(30, BACKOFF_BASE * 2 ** a) + random.random() * BACKOFF_BASE
                log.warning("supabase %s %s: %s, retry %d/%d in %.1fs", method, path.split("?")[0], _brief(e), a + 1, tries - 1, d); _sleep(d)
    def claim(self): r = self._req("POST", "/rest/v1/rpc/sel_claim_run", {"p_worker": WID}, retries=1); return r if r and r.get("id") else None
    def run(self, rid): return (self._req("GET", f"/rest/v1/sel_runs?id=eq.{rid}&select=*") or [{}])[0]
    def patch_run(self, rid, **kw): self._req("PATCH", f"/rest/v1/sel_runs?id=eq.{rid}", kw, "return=minimal")
    def patch_run_if(self, rid, expect, **kw):  # 只在仍是 expect 时更新（查询中被用户取消 → 不再改回“待抓取”）；同一次请求，不多一次往返
        self._req("PATCH", f"/rest/v1/sel_runs?id=eq.{rid}&status=eq.{expect}", kw, "return=minimal")
    def open_batch(self, rid):
        r = self._req("GET", f"/rest/v1/sel_batches?run_id=eq.{rid}&status=in.(queued,running)&order=batch_no&limit=1"); return r[0] if r else None
    def patch_batch(self, bid, **kw): self._req("PATCH", f"/rest/v1/sel_batches?id=eq.{bid}", kw, "return=minimal")
    def new_batch(self, body):
        try: self._req("POST", "/rest/v1/sel_batches", body, "return=minimal")
        except urllib.error.HTTPError as e:
            if e.code != 409: raise   # 409 = 同一批已存在（上一次请求其实成功了，重试撞唯一键）→ 视为成功
    def passed_ids(self, rid): return {str(x["etsy_listing_id"]) for x in (self._req("GET", f"/rest/v1/sel_items?run_id=eq.{rid}&select=etsy_listing_id") or [])}
    def insert_item(self, body): self._req("POST", "/rest/v1/sel_items?on_conflict=run_id,etsy_listing_id", body, "return=minimal,resolution=ignore-duplicates")
    def exclude_words(self): return self._req("GET", "/rest/v1/sel_exclude_words?select=category,word,enabled&enabled=eq.true") or []
    def purge(self): return self._req("POST", "/rest/v1/rpc/sel_purge_older_than", {})
    # 033 预筛选主图查询（ECS ehunt_web provider 写请求，本 worker 认领并写回主图）
    def lookup_claim(self, n): return self._req("POST", "/rest/v1/rpc/ehunt_lookup_claim", {"p_worker": WID, "p_limit": n}, retries=1) or []
    def lookup_patch(self, lid, **kw): self._req("PATCH", f"/rest/v1/ehunt_lookup_requests?listing_id=eq.{int(lid)}", kw, "return=minimal")
    def lookup_purge(self): return self._req("POST", "/rest/v1/rpc/ehunt_lookup_purge", {})
    def heartbeat(self, info): self._req("POST", "/rest/v1/worker_heartbeat?on_conflict=worker_id", {"worker_id": HB_ID, "last_seen": NOW(), "info": info},
                                         "return=minimal,resolution=merge-duplicates", retries=1)

# ---- 单页超时自动重试（2026-10-20 22:59 事故：第 14 页 reading rows 卡 637s，整个队列被堵约 10 分钟） ----
# 每页（翻页/跳页 + 逐行读取）有硬超时；超时 → 记 warning → 关掉浏览器上下文，重新打开列表并设置筛选、跳回同一页重读，最多重试 PAGE_RETRIES 次。
# 一页先整页读进内存、读完才 yield：超时的那一页一行都没有交出去 → 重读不会重复；已交出（已落定/在判断中）的 rank 绝不重读。
# 全部重试都失败 → 抛 PageTimeout，走 handle_run 现有失败路径（本批“已停止”+中文原因，「继续本批」从断点接着读），队列继续往下走。
PAGE_TIMEOUT_S = float(os.environ.get("EHUNT_PAGE_TIMEOUT_S", "90"))     # 正常一页 8-17s
PAGE_RETRIES = max(0, int(os.environ.get("EHUNT_PAGE_RETRIES", "2")))   # 超时后重试次数（共 1 + 2 = 3 次）
PAGE_KILL_GRACE_S = float(os.environ.get("EHUNT_PAGE_KILL_GRACE_S", "20"))  # 超过硬超时这么久仍卡在单个浏览器调用里 → 看门狗杀掉本 profile 的 chromium
class PageTimeout(RuntimeError): pass
class PageStuck(RuntimeError): pass          # 翻页到第 N 页超时（页码/首行没变）→ 与超时同样重开同一页重试（2026-10-21 menu 第 2 页）
class KeywordNotApplied(RuntimeError): pass  # 关键词未生效（Selected: 中没有 Keyword）→ 同样重开重试（2026-10-21 book 续读）
class RunStopped(BaseException): pass        # 重试间隙发现用户已取消/暂停：立即停（BaseException：不被页内 except Exception 吞掉）
RETRY_KINDS = {PageTimeout: "超时", PageStuck: "翻页卡住", KeywordNotApplied: "关键词未生效"}

def _kill_profile_browser(profile=None):
    """看门狗（另一线程）：只杀本进程后代中 --user-data-dir=本 profile 的 chromium，让主线程卡住的 Playwright 调用立即报错返回。"""
    profile = os.path.realpath(profile or PROFILE); me = os.getpid(); killed = []
    try:
        procs = {}
        for d in os.listdir("/proc"):
            if not d.isdigit(): continue
            try:
                ppid = int(open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()[1])
                cmd = open(f"/proc/{d}/cmdline", "rb").read().decode(errors="replace").split("\0")
                procs[int(d)] = (ppid, cmd)
            except Exception: continue
        def descends(pid):
            for _ in range(20):
                pid = procs.get(pid, (0,))[0]
                if pid == me: return True
                if pid <= 1: return False
            return False
        for pid, (_, cmd) in procs.items():
            if any(a.startswith("--user-data-dir=") and os.path.realpath(a.split("=", 1)[1]) == profile for a in cmd) and descends(pid):
                try: os.kill(pid, 9); killed.append(pid)
                except Exception: pass
    except Exception: log.warning("page watchdog kill failed", exc_info=True)
    return killed

class _Watchdog:
    """一页一次：deadline + grace 后仍未 disarm → 调 kill()。正常页（8-17s）只多一个空闲计时器线程，不影响速度。"""
    def __init__(self, seconds, kill):
        import threading
        self.fired = False; self.kill = kill
        self.t = threading.Timer(seconds, self._fire); self.t.daemon = True; self.t.start()
    def _fire(self):
        self.fired = True; log.warning("ehunt page watchdog: browser call stuck past hard timeout, killing browser to unblock"); self.kill()
    def disarm(self): self.t.cancel()

def page_loop(open_session, start_rank, end_rank, ps, between=None, timeout=None, retries=None, clock=time.monotonic, watchdog=None, should_stop=None):
    """与浏览器无关的翻页/超时/重试骨架（单测直接用假 session）。
    open_session(page) → session：已打开列表并停在第 page 页；session.read(page, deadline) → (rows, has_next)，rows = [(rank, row)]，
    超过 deadline 抛 PageTimeout；翻页卡住 PageStuck、关键词未生效 KeywordNotApplied 同样重试；should_stop() 为真 → 抛 RunStopped（取消/暂停）。
    session.next(page, deadline) 翻到第 page 页；session.ctx 给 between；session.close()。"""
    timeout = PAGE_TIMEOUT_S if timeout is None else timeout; retries = PAGE_RETRIES if retries is None else retries
    watchdog = watchdog or (lambda s: _Watchdog(s, _kill_profile_browser))
    page = (start_rank - 1) // ps + 1; done = start_rank - 1; sess = None; need_next = False
    try:
        while done < end_rank:
            last = None
            for att in range(retries + 1):
                if att and should_stop and should_stop(): raise RunStopped()
                deadline = clock() + timeout; wd = watchdog(timeout + PAGE_KILL_GRACE_S); t0 = clock()
                try:
                    if sess is None: sess = open_session(page)
                    elif need_next: sess.next(page, deadline)
                    need_next = False
                    rows, has_next = sess.read(page, deadline)
                    if att and _PROG[0]: _PROG[0].update(retry=0, retry_why=None)   # 重试成功：清掉网页上的“超时重试中”
                    break
                except LoginExpired: raise
                except Exception as e:
                    kind = next((v for k, v in RETRY_KINDS.items() if isinstance(e, k)), None)
                    if kind is None and (wd.fired or clock() > deadline or "Timeout" in type(e).__name__): kind = "超时"
                    if kind is None: raise
                    last = (kind, e)
                    log.warning("ehunt page %s: %s after %.0fs (attempt %d/%d, limit %.0fs): %s — reopening same page (rank %s→)",
                                page, "timed out" if kind == "超时" else "stuck paging" if kind == "翻页卡住" else "keyword not applied",
                                clock() - t0, att + 1, retries + 1, timeout, str(e)[:120], done + 1)
                    if att < retries: stage("page_retry", page=page, retry=att + 1, retry_max=retries, retry_why=kind)
                    if sess is not None:
                        try: sess.close()
                        except Exception: log.warning("closing timed-out session failed", exc_info=True)
                    sess = None; need_next = False
                finally: wd.disarm()
            else:
                kind, e = last
                if kind == "超时": raise PageTimeout(f"EHunt 第 {page} 页读取超时（每次 {timeout:.0f} 秒，已重试 {retries} 次仍失败）")
                raise type(e)(f"EHunt 第 {page} 页{kind}（{str(e)[:80]}；已重试 {retries} 次仍失败）")
            for rank, row in rows:
                if rank <= done: continue          # 重读时跳过已交出的 rank（防重复）
                if rank > end_rank: break
                yield row; done = rank
            if between and sess is not None:
                try: between(sess.ctx)
                except Exception: log.warning("lookup between pages failed", exc_info=True)
            if not has_next or done >= end_rank: break
            page += 1; need_next = True
    finally:
        if sess is not None: sess.close()

def scrape_ehunt(keyword, params, start_rank, end_rank, between=None, should_stop=None):
    """读 EHunt 排名 start_rank..end_rank（含）：界面设好筛选 → 跳到对应页（每页 20）→ 逐行读 DOM。只在内存里产出行，不落盘。
    between(ctx)：每读完一页调用一次（穿插处理预筛选主图查询，用同一浏览器上下文的新标签页，不影响本页状态）。
    每页硬超时 PAGE_TIMEOUT_S，超时重开同一页重试 PAGE_RETRIES 次（见 page_loop）。"""
    import re
    from playwright.sync_api import sync_playwright
    U = ui(); ps = int(U["page_size"]); num = lambda s: float("".join(ch for ch in (s or "") if ch.isdigit() or ch == ".") or 0)
    def d7_of(t): m = re.search(U["d7_regex"], t or ""); return int(m.group(1).replace(",", "")) if m else 0
    def total_of(t):  # “637” / “6.26K” / “1.2M”
        t = re.split(r"↑", t or "")[0].strip().upper(); k = 1000 if t.endswith("K") else 1000000 if t.endswith("M") else 1
        return int(round(num(t) * k))
    def left_ms(deadline):   # 每个 Playwright 调用的超时不超过本页剩余时间（最多 30s）
        r = deadline - time.monotonic()
        if r <= 0: raise PageTimeout("本页读取超时")
        return max(1000, min(30000, int(r * 1000)))
    class Sess:
        def __init__(s, page):
            s.ctx = None
            s.ctx, s.pg = _open_list(p, keyword, params)
            try:
                stage("read_setup", page=page)
                s.pg.wait_for_selector(U["row"], timeout=30000)
                if page > 1:  # 续读/重试：分页 “Go to” 输入框
                    old = s.first_href(); s.pg.fill(U["jump_input"], str(page)); s.pg.keyboard.press("Enter"); s.wait_page(page, old, time.monotonic() + PAGE_TIMEOUT_S)
            except BaseException: s.close(); raise
        def close(s):
            if s.ctx is not None:
                c, s.ctx = s.ctx, None
                try: c.close()
                except Exception as e: log.warning("ehunt ctx close failed (browser already gone?): %s", str(e)[:120])
        def first_href(s):
            e = s.pg.query_selector(U["row"] + " " + U["title"]); return e.get_attribute("href") if e else None
        def wait_page(s, n, old, deadline):  # SPA 翻页：等到当前页码 = n 且首行变化
            for i in range(60):
                left_ms(deadline)
                if i % 10 == 0: dismiss_overlays(s.pg, U)
                a_ = s.pg.query_selector(U["active_page"])
                if a_ and a_.inner_text().strip() == str(n) and s.first_href() != old: break
                time.sleep(0.5)
            else: raise PageStuck(f"翻页到第 {n} 页超时")
            time.sleep(1.5)
        def next(s, n, deadline):
            old = s.first_href(); _click(s.pg, U, U["next"], timeout=left_ms(deadline)); s.wait_page(n, old, deadline)
        def read(s, page, deadline):
            pg = s.pg; pg.set_default_timeout(left_ms(deadline))
            pg.wait_for_selector(U["row"], timeout=left_ms(deadline)); dismiss_overlays(pg, U); t_page = time.time()
            rank = (page - 1) * ps; out = []
            log.info("ehunt page %s: reading rows (rank %s→, target %s)", page, rank + 1, end_rank); stage("reading", page=page)
            for tr in pg.query_selector_all(U["row"]):
                pg.set_default_timeout(left_ms(deadline))
                a = tr.query_selector(U["title"])
                if not a: continue
                rank += 1
                if rank < start_rank: continue
                if rank > end_rank: break
                lk = tr.query_selector(U["link"]); href = (lk.get_attribute("href") if lk else "") or a.get_attribute("href") or ""
                lid = "".join(ch for ch in href.rstrip("/").split("/listing/")[-1].split("/product-detail/")[-1].split("/")[0] if ch.isdigit())
                img = tr.query_selector(U["img"]); cell = lambda k: (lambda e: e.inner_text() if e else "")(tr.query_selector(U[k]))
                out.append((rank, {"rank": rank, "page": page, "title": a.inner_text().strip(), "listing_id": lid, "url": f"https://www.etsy.com/listing/{lid}", "image_url": img.get_attribute("src") if img else "",
                                   "price": num(cell("price")), "total_sales": total_of(cell("total")), "sales_7d": d7_of(cell("d7"))}))
                time.sleep(0.3)
            left_ms(deadline)
            has_next = rank < end_rank and pg.query_selector(U["next"]) is not None
            pg.set_default_timeout(30000)
            log.info("ehunt page %s done: rank=%s, %.0fs", page, rank, time.time() - t_page)
            return out, has_next
    with sync_playwright() as p:
        yield from page_loop(Sess, start_rank, end_rank, ps, between=between, should_stop=should_stop)

# 标准筛选（需求文档 §2）：数量与抓取都必须带上，否则会拿到未筛选的总数（如 43,805）。
# EHunt 的筛选不在 URL 里（https://ehunt.ai/etsy-product-research 无查询参数），只在页面状态里 → 必须通过界面操作设置。
FILTER_DEFAULTS = {"price_min": 8, "price_max": 40, "min_sales": 100, "min_sales_7d": 4, "sort": "↓7-day sales"}  # 7 天销量 >3 即 Min 4
# 界面选择器/文字（2026-10-16 已登录实测校准；EHunt 改版时用 EHUNT_UI（JSON）覆盖，不改代码）
UI_DEFAULTS = {
    "url": "https://ehunt.ai/etsy-product-research",
    "banner_close": "button.ehunt-ai-banner__close",          # 顶部 “Meet the new EHunt AI” 横幅会挡住点击
    # Element Plus 弹窗（el-overlay 遮罩）会拦截所有点击：营销弹窗关掉即可；登录提示弹窗 = 账号已退出登录，关掉也搜不了（关键词不生效）
    "overlay": ".el-overlay", "overlay_close": ".el-dialog__headerbtn, .el-message-box__headerbtn, [aria-label='Close this dialog']",
    "login_dialog": ".login-tip-dialog, .el-overlay-dialog[aria-label='Unlock more EHunt features For Free!']",
    "header_login": ".EtsyHunt-navbar a:text-is('Login'), .navbar a:text-is('Login')",   # 只认文字 Login；li.nav-item.login 登录后也在（放邮箱菜单），不能用
    "search_input": "input[placeholder^='Enter a product name, tag, product ID']",
    "search_btn": ".el-input-group__append >> text=Search",   # 不能用 button:has-text('Search')：会命中 “Batch search” 弹窗
    "price_dd": "span.truncate:text-is('Price(USD)')",
    "sales_dd": "span.truncate:text-is('Sales')",
    "popover": ".el-popover[aria-hidden='false']",             # 当前展开的下拉面板
    "total_sales_line": "xpath=.//div[normalize-space(text())='Total sales:']/..",
    "sales7_line": "xpath=.//div[normalize-space(text())='7-day sales:']/..",
    "chip_keyword": "Keyword: {keyword}",
    "chip_price": "Price: {price_min}~{price_max}", "chip_total": "Total sales: {min_sales}~", "chip_7d": "7-day sales: {min_sales_7d}~",  # 10-16 实测确认；Min 为闭区间
    "min_input": "input[placeholder='Min']", "max_input": "input[placeholder='Max']",
    "ok_btn": "[role=button]:text-is('OK')",                   # div[role=button]，不是 <button>；只在 popover 内找
    "sort_dd": ":text('Export to CSV') >> xpath=following::*[contains(normalize-space(.),'sales')][1]",
    "selected_row": ":text('Selected:') >> xpath=..",
    "count_text": ":text-matches('Find [0-9,]+ products')",
    "count_regex": r"Find ([\d,]+) products",
    # 列表（Element Plus el-table）：第 1 列 商品信息（图/标题/价格），第 2 列 Total sales + 绿色 ↑N（7 天销量）
    "row": "table.el-table__body tr.el-table__row", "title": "a.product-title-clamp", "link": "a[href*='etsy.com/listing/']",
    "price": "td:nth-child(1) span.text-brand", "total": "td:nth-child(2) span:first-child",
    "d7": "td:nth-child(2)", "d7_regex": r"↑\s*([\d,]+)", "img": "td:nth-child(1) img",
    "next": "button.btn-next:not([disabled])", "active_page": ".el-pager li.is-active",
    "page_size": 20, "jump_input": ".el-pagination__jump input",   # “Go to”/跳至 输入框，回车跳页
}
def ui(): return {**UI_DEFAULTS, **json.loads(os.environ.get("EHUNT_UI", "{}"))}
def filters(params): return {**FILTER_DEFAULTS, **json.loads(os.environ.get("EHUNT_FILTERS", "{}")), **{k: v for k, v in (params or {}).items() if k in FILTER_DEFAULTS}}

class LoginExpired(RuntimeError): pass
LOGIN_MSG = "EHunt 登录已失效（页面顶栏显示 Login/Register，搜索弹出登录提示）：需要在 box 上运行 deploy/box-selection-worker.sh login 重新人工登录"

def _visible(loc):
    try: return [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]
    except Exception: return []

def _check_login(pg, U):
    if _visible(pg.locator(U["login_dialog"])) or _visible(pg.locator(U["header_login"])): raise LoginExpired(LOGIN_MSG)

def dismiss_overlays(pg, U, rounds=4):
    """关掉所有可见的 el-overlay 弹窗：先点右上角关闭 → 再按 Escape → 最后把遮罩隐藏。登录提示弹窗直接报登录失效。返回关掉的弹窗名。"""
    closed = []
    for _ in range(rounds):
        ovs = _visible(pg.locator(U["overlay"]))
        if not ovs: break
        _check_login(pg, U)
        for ov in ovs:
            try: name = (ov.locator("[aria-label]").first.get_attribute("aria-label", timeout=500) if ov.locator("[aria-label]").count() else "") or ov.inner_text(timeout=500)[:40]
            except Exception: name = "?"
            closed.append(name); log.info("ehunt: dismiss overlay %r", name)
            try:
                b = _visible(ov.locator(U["overlay_close"]))
                if b: b[0].click(timeout=3000); time.sleep(0.6)
            except Exception: pass
        if not _visible(pg.locator(U["overlay"])): break
        try: pg.keyboard.press("Escape"); time.sleep(0.6)
        except Exception: pass
        if _visible(pg.locator(U["overlay"])):   # 兜底：隐藏遮罩并恢复页面滚动（不删 DOM，避免 Vue 报错）
            pg.evaluate("""() => { document.querySelectorAll('.el-overlay').forEach(e => { if (e.offsetParent !== null || getComputedStyle(e).display !== 'none') e.style.display = 'none'; });
                                   document.body.classList.remove('el-popup-parent--hidden'); document.body.style.overflow = ''; }""")
            time.sleep(0.3)
    return closed

def _click(pg, U, target, timeout=10000):
    """点击前后都清弹窗；被遮罩拦截超时 → 清弹窗再试一次（共 3 次）。target 是选择器字符串或 Locator。"""
    from playwright.sync_api import TimeoutError as PWTimeout
    loc = pg.locator(target).first if isinstance(target, str) else target
    for attempt in range(3):
        dismiss_overlays(pg, U)
        try: loc.click(timeout=timeout); break
        except PWTimeout:
            log.warning("ehunt: click %s timed out (attempt %s), clearing overlays", target if isinstance(target, str) else "locator", attempt + 1)
            if attempt == 2: dismiss_overlays(pg, U); raise
    time.sleep(0.5); dismiss_overlays(pg, U)

BROWSER_CHANNEL = os.environ.get("EHUNT_BROWSER_CHANNEL", "")   # 空 = Playwright 自带 chromium；"chrome" = 系统 Chrome
SYSTEM_CHROME = "/opt/google/chrome/chrome"
def launch_ctx(p, headless=True, viewport=None):
    """打开 EHunt 持久化上下文。2026-10-22 11:07 事故：box 上 playwright 被升级到 1.64.0 而 ~/.cache/ms-playwright 里没有对应浏览器
    （“Executable doesn't exist … chrome-headless-shell”）→ 所有查询/抓取打不开浏览器。自带浏览器缺失时自动改用系统 Chrome（同一 profile），
    不需要人工 playwright install；其它启动错误原样抛出。"""
    kw = dict(headless=headless, viewport=viewport or {"width": 1440, "height": 1000}, locale="en-US")
    if BROWSER_CHANNEL: return p.chromium.launch_persistent_context(PROFILE, channel=BROWSER_CHANNEL, **kw)
    try: return p.chromium.launch_persistent_context(PROFILE, **kw)
    except Exception as e:
        if "Executable doesn't exist" not in str(e) or not os.path.exists(SYSTEM_CHROME): raise
        log.warning("bundled Playwright browser missing; falling back to system Chrome (channel=chrome)")
        return p.chromium.launch_persistent_context(PROFILE, channel="chrome", **kw)

def _open_list(p, keyword, params):
    """打开 EHunt 产品研究页并通过界面设置 关键词 + 价格 + 总销量 + 排序，校验 Selected: 标签后返回。"""
    U, F = ui(), filters(params)
    counting = (_PROG[0].state.get("stage") or "").startswith("count") if _PROG[0] else False
    stage("count_open" if counting else "read_setup")
    ctx = launch_ctx(p)
    pg = ctx.pages[0] if ctx.pages else ctx.new_page(); pg.goto(U["url"], wait_until="networkidle", timeout=60000)
    if "login" in pg.url: ctx.close(); raise RuntimeError("EHunt 登录已失效：需要在 box 上运行 --login 重新人工登录")
    if counting: stage("count_filter")
    try:
        b = pg.query_selector(U["banner_close"])
        if b and b.is_visible(): b.click(); time.sleep(0.5)
        dismiss_overlays(pg, U); _check_login(pg, U)
        for attempt in range(3):   # 页面水合前填写的关键词会被清空 → 以 Selected: 的 Keyword 标签为准，最多重试 3 次
            dismiss_overlays(pg, U); pg.fill(U["search_input"], keyword); _click(pg, U, U["search_btn"]); pg.wait_for_load_state("networkidle"); time.sleep(2 + attempt * 2)
            dismiss_overlays(pg, U)
            if U["chip_keyword"].format(keyword=keyword).replace(" ", "").lower() in pg.locator(U["selected_row"]).first.inner_text().replace(" ", "").lower(): break
        else: raise KeywordNotApplied(f"关键词未生效：Selected: 中没有 Keyword: {keyword}")
        # Basic: Price(USD) → 自定义 Min/Max → OK（都在展开的 popover 里）
        _click(pg, U, U["price_dd"]); time.sleep(1); pop = pg.locator(U["popover"])
        pop.locator(U["min_input"]).first.fill(str(F["price_min"])); pop.locator(U["max_input"]).first.fill(str(F["price_max"]))
        _click(pg, U, pop.locator(U["ok_btn"]).first); pg.wait_for_load_state("networkidle"); time.sleep(2)
        # Sales → Total sales: Min 100 + 7-day sales: Min 4（>3）→ OK
        _click(pg, U, U["sales_dd"]); time.sleep(1); pop = pg.locator(U["popover"])
        pop.locator(U["total_sales_line"]).locator(U["min_input"]).first.fill(str(F["min_sales"]))
        pop.locator(U["sales7_line"]).locator(U["min_input"]).first.fill(str(F["min_sales_7d"]))
        _click(pg, U, pop.locator(U["ok_btn"]).first); pg.wait_for_load_state("networkidle"); time.sleep(2)
        dismiss_overlays(pg, U)
        # 排序默认 ↓7-day sales，校验
        st = pg.locator(U["sort_dd"]).first.inner_text().strip()
        if "7-day" not in st: raise RuntimeError(f"排序不是 7 天销量降序：{st}")
        sel = pg.locator(U["selected_row"]).first.inner_text()
        need = [U["chip_keyword"].format(keyword=keyword), U["chip_price"].format(**F), U["chip_total"].format(**F), U["chip_7d"].format(**F)]
        miss = [x for x in need if x.replace(" ", "").lower() not in sel.replace(" ", "").lower()]
        if miss: raise RuntimeError(f"筛选未生效，Selected: 缺少 {miss}（实际：{sel[:120]}）")
    except Exception:
        try: os.makedirs(LOGDIR, exist_ok=True); pg.screenshot(path=f"{LOGDIR}/ehunt-fail-{int(time.time())}.png")
        except Exception: pass
        ctx.close(); raise
    return ctx, pg

def count_ehunt(keyword, params):
    """第 1 步：只读结果数文字 “Find 20 products”。"""
    import re
    from playwright.sync_api import sync_playwright
    U = ui()
    with sync_playwright() as p:
        ctx, pg = _open_list(p, keyword, params)
        try:
            stage("count_read"); txt = pg.locator(U["count_text"]).first.inner_text()
            m = re.search(U["count_regex"], txt)
            if not m: raise RuntimeError(f"未识别结果数文字：{txt[:80]}")
            return int(m.group(1).replace(",", ""))
        finally: ctx.close()

# ---- 预筛选主图查询（033 ehunt_lookup_requests）：EHunt API 无订阅 → 用已登录的 EHunt 网页按 listing id 搜索 ----
# 只读取一项：主图 URL（Hongyan 2026-10-19：不读标题/价格/销量/收藏/评论）。结果必须是 /listing/<id> 完全一致的行，绝不取第一条。
LOOKUP_BATCH = max(1, int(os.environ.get("EHUNT_WEB_BATCH", "10")))
LOOKUP_INTERVAL = float(os.environ.get("EHUNT_WEB_MIN_INTERVAL", "1.0"))   # 相邻两次搜索至少间隔（秒），约 ≤1 次/秒
LOOKUP_READS = 3                                                            # 结果表最多读 3 次（每次间隔 1.5 秒）才判定“未找到”
LOGIN_TAG = "EHunt 未登录"
_LAST_LOOKUP = [0.0]

def main_image(u):
    """EHunt 列表缩略图 → Etsy 主图 il_570xN（只这一张）。"""
    import re
    u = (u or "").strip()
    if u.startswith("//"): u = "https:" + u
    return re.sub(r"/il_[0-9a-zA-Z]+x[0-9a-zA-Z]+\.", "/il_570xN.", u) if re.match(r"^https://i\.etsystatic\.com/", u) else u

def match_image(rows, listing_id):
    """rows: [(href, img_src)]。只接受 href 含 /listing/<id>（后面不是数字）的行。
    返回主图 URL；行匹配但图还没加载/是占位图（data:、空）→ ""；没有匹配行 → None。"""
    import re
    pat = re.compile(rf"/listing/{int(listing_id)}(?!\d)"); hit = None
    for href, src in rows:
        if pat.search(href or ""):
            u = main_image(src)
            if re.match(r"^https://", u or ""): return u
            hit = ""
    return hit

# 商品详情页兜底（2026-10-22 核实：18 条“网页搜不到 listing”在 ehunt.ai/product-detail/{id} 都能打开，Listing Update Time 为 2026-08~10、
# 有销量 —— 并未下架，是产品研究页按 listing id 搜索搜不到（该页只收录部分商品）。搜不到时再打开详情页取第一张 etsystatic 主图；
# 详情页对不存在的 id 显示 “Product is not included or invalid” 且没有任何 etsystatic 图片 → 仍判定未找到。）
DETAIL_URL = "https://ehunt.ai/product-detail/{}"
DETAIL_FALLBACK = os.environ.get("EHUNT_DETAIL_FALLBACK", "1") != "0"
def pick_detail_image(srcs, body_text, listing_id):
    """srcs: 详情页所有 <img> 的 src；body_text: 页面文字（必须含该 listing id，防止读到别的商品）。返回 il_570xN 主图或 None。"""
    import re
    if str(int(listing_id)) not in (body_text or ""): return None
    for u in srcs or []:
        u = main_image(u)
        if re.match(r"^https://i\.etsystatic\.com/.+/il_", u or ""): return u
    return None
def detail_image(dp, lid, reads=6):
    _throttle(); dp.goto(DETAIL_URL.format(int(lid)), wait_until="domcontentloaded", timeout=45000)
    if "login" in dp.url: raise LoginExpired(LOGIN_MSG)
    for _ in range(reads):
        time.sleep(1.5)
        try: u = pick_detail_image(dp.eval_on_selector_all("img", "els => els.map(e => e.currentSrc || e.src)"), dp.inner_text("body"), lid)
        except Exception: u = None
        if u: return u
        if "not included or invalid" in (dp.inner_text("body") or ""): return None
    return None

def _throttle():
    wait = _LAST_LOOKUP[0] + LOOKUP_INTERVAL - time.monotonic()
    if wait > 0: time.sleep(wait)
    _LAST_LOOKUP[0] = time.monotonic()

def page_finder(pg):
    """返回 finder(listing_id) → 主图 URL | None；登录失效抛 LoginExpired。在 EHunt 产品研究页的搜索框里输入 listing id。"""
    U = ui()
    def rows():
        out = []
        for tr in pg.query_selector_all(U["row"]):
            lk = tr.query_selector(U["link"]) or tr.query_selector(U["title"]); img = tr.query_selector(U["img"])
            out.append(((lk.get_attribute("href") if lk else "") or "", (img.get_attribute("src") if img else "") or ""))
        return out
    def finder(lid):
        _throttle(); dismiss_overlays(pg, U); _check_login(pg, U)
        pg.fill(U["search_input"], str(int(lid))); _click(pg, U, U["search_btn"])
        try: pg.wait_for_load_state("networkidle", timeout=20000)
        except Exception: pass
        seen = None
        for i in range(LOOKUP_READS):
            time.sleep(1.5); dismiss_overlays(pg, U); _check_login(pg, U)
            u = match_image(rows(), lid)
            if u: return u
            seen = u if u is not None else seen
        if seen is None and DETAIL_FALLBACK:   # 搜索页搜不到 → 商品详情页兜底（同一上下文的另一个标签页，复用）
            try:
                if dp[0] is None or dp[0].is_closed(): dp[0] = pg.context.new_page()
                u = detail_image(dp[0], lid)
                if u: log.info("lookup %s: found via product-detail fallback", lid); return u
            except LoginExpired: raise
            except Exception as e: log.warning("lookup %s: product-detail fallback failed: %s", lid, str(e)[:160])
        return seen   # "" = EHunt 有这条但没有主图（懒加载占位图）；None = 搜索和详情页都找不到
    dp = [None]
    def cleanup():
        try:
            if dp[0] is not None and not dp[0].is_closed(): dp[0].close()
        except Exception: pass
    finder.cleanup = cleanup
    return finder

def open_lookup_page(ctx):
    U = ui(); pg = ctx.new_page(); pg.goto(U["url"], wait_until="networkidle", timeout=60000)
    if "login" in pg.url: raise LoginExpired(LOGIN_MSG)
    b = pg.query_selector(U["banner_close"])
    if b and b.is_visible(): b.click(); time.sleep(0.5)
    dismiss_overlays(pg, U); _check_login(pg, U)
    return pg

def _requeue_lookup(store, lid):
    try: store.lookup_patch(lid, status="queued", claimed_by=None, claimed_at=None)
    except Exception: log.warning("requeue lookup %s failed", lid, exc_info=True)

def serve_lookups(store, finder_factory, limit=LOOKUP_BATCH, max_rounds=20):
    """认领并处理查询请求，直到队列空（或 max_rounds 轮）。finder_factory() 惰性打开页面（没有请求就不开浏览器）。
    done=主图；not_found=EHunt 搜索和商品详情页都找不到；login_expired=登录失效（本轮剩余全部标记，不算失败，ECS 端熔断并提示重新登录）。
    打不开查询页（goto 超时 / 浏览器起不来）不是这几条 listing 的问题 → 本轮已认领未处理的放回队列（不标 error），下一轮主循环再试；
    Playwright 被污染 → 同样放回队列并抛 PlaywrightPoisoned，main() 换新进程。requested_at 不变，ECS 端排队超时判断照旧。"""
    finder, n = None, 0
    for _ in range(max_rounds):
        reqs = store.lookup_claim(limit)
        if not reqs: break
        for i, r in enumerate(reqs):
            lid = r["listing_id"]; opening = finder is None
            try:
                if opening: finder = finder_factory()
                opening = False
                u = finder(lid)
                store.lookup_patch(lid, status="not_found" if u is None else "done", image_url=u or None, error=None, finished_at=NOW())
                log.info("lookup %s: %s", lid, "not found" if u is None else "image" if u else "found, no image")
            except LoginExpired as e:
                for r2 in reqs[i:]: store.lookup_patch(r2["listing_id"], status="login_expired", error=f"{LOGIN_TAG}：{e}"[:300], finished_at=NOW())
                log.warning("lookup: %s", e); return n
            except Exception as e:
                if is_poisoned(e) or opening:
                    for r2 in reqs[i:]: _requeue_lookup(store, r2["listing_id"])
                    if is_poisoned(e): raise PlaywrightPoisoned(str(e)[:300]) from e
                    log.warning("lookup page open failed; %d request(s) requeued: %s", len(reqs) - i, str(e)[:160]); return n
                store.lookup_patch(lid, status="error", error=str(e)[:300], finished_at=NOW()); log.warning("lookup %s failed", lid, exc_info=True)
                finder = None   # 页面可能已坏：下一条重开
            n += 1
    return n

def serve_lookups_browser(store, ctx=None):
    """ctx=None：自己开持久化上下文（主循环空闲时）；否则在选品抓取的同一上下文里开新标签页（翻页间穿插）。
    根因（2026-10-22 07:44 / 07:58 “Playwright Sync API inside the asyncio loop”）：旧版 factory 每次被调用都 enter 一个新的 sync_playwright()；
    第一次打开查询页 goto 超时后 serve_lookups 把 finder 置空、下一条再调 factory → 在第一个 sync_playwright 仍未退出时又进入第二个 → 报错，
    本轮剩余每条都被标 error（ECS 端显示取数失败）。现在：一个调用内只进入一次 sync_playwright、只开一次上下文，重开的只是页面。"""
    from contextlib import ExitStack
    with ExitStack() as es:
        st = {"p": None, "c": ctx, "pg": None}
        def factory():
            if st["c"] is None:
                if st["p"] is None:
                    if playwright_leaked(): raise PlaywrightPoisoned(ASYNCIO_LOOP_MSG + "（查询前检测到残留事件循环）")
                    from playwright.sync_api import sync_playwright
                    st["p"] = es.enter_context(sync_playwright())
                c = launch_ctx(st["p"]); es.callback(c.close); st["c"] = c
            if st["pg"] is not None:
                try: st["pg"].close()
                except Exception: pass
                st["pg"] = None
            pg = open_lookup_page(st["c"]); st["pg"] = pg
            if ctx is not None: es.callback(lambda: (not pg.is_closed()) and pg.close())
            f = page_finder(pg); es.callback(f.cleanup)   # 详情页兜底的标签页：抓取上下文里用完即关
            return f
        return serve_lookups(store, factory)

def lookup_dry(ids):
    """真实浏览器、只读：逐条查询主图并打印 JSON；不连数据库、不写任何东西。"""
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        c = launch_ctx(p)
        try:
            finder = page_finder(open_lookup_page(c))
            for lid in ids:
                t = time.time()
                try: u = finder(lid); out.append({"listing_id": lid, "status": "not_found" if u is None else "done", "image_url": u or None, "seconds": round(time.time() - t, 1)})
                except LoginExpired as e: out.append({"listing_id": lid, "status": "login_expired", "error": str(e)}); break
                except Exception as e: out.append({"listing_id": lid, "status": "error", "error": str(e)[:200]})
        finally: c.close()
    print(json.dumps(out, ensure_ascii=False, indent=1)); return out

def fake_scrape(path, start_rank=1, end_rank=10**9):
    for r in json.load(open(path)):
        if start_rank <= r["rank"] <= end_rank: time.sleep(0.05); yield {**r, "page": (r["rank"] - 1) // 20 + 1}

# 排除原因：不入库，只写 box 本地 JSONL（3 天后自动删除），供 序猿 用 lookup.py 抽查
EXCL_DIR = os.environ.get("SEL_EXCL_DIR", os.path.expanduser("~/.local/share/etsy-selection/exclusion-logs"))
KEEP_SECONDS = 3 * 86400
def log_exclusion(run, batch_no, row, reason, detail, source):
    os.makedirs(EXCL_DIR, mode=0o700, exist_ok=True)
    with open(os.path.join(EXCL_DIR, f"{run['id']}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": NOW(), "run_id": run["id"], "keyword": run.get("keyword"), "batch_no": batch_no, "rank": row.get("rank"),
                            "listing_id": str(row["listing_id"]), "title": row.get("title"), "reason": reason, "detail": detail, "source": source,
                            "matched_word": (detail or "")[4:] if (detail or "").startswith("关键词:") else None}, ensure_ascii=False) + "\n")
def purge_local():
    if not os.path.isdir(EXCL_DIR): return 0
    n = 0; cutoff = time.time() - KEEP_SECONDS
    for fn in os.listdir(EXCL_DIR):
        fp = os.path.join(EXCL_DIR, fn)
        if fn.endswith(".jsonl") and os.path.getmtime(fp) < cutoff: os.remove(fp); n += 1
    return n

# ---- 豆包视觉并发（规则/投票/判定完全不变，只是并行发请求）----
import threading, random, urllib.error
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
VISION_CONC = max(1, int(os.environ.get("SEL_VISION_CONCURRENCY", "6")))      # 同时处理的条目数
ARK_INFLIGHT = max(1, int(os.environ.get("SEL_ARK_MAX_INFLIGHT", "12")))     # 同时在途的 Ark 请求上限
_ARK_SEM = threading.BoundedSemaphore(ARK_INFLIGHT); _TL = threading.local()
_VOTE_POOL = ThreadPoolExecutor(max_workers=ARK_INFLIGHT, thread_name_prefix="ark")
def _ark_vision(title, img):
    """与 classify.vision 相同的请求体；加在途上限 + 429/5xx/网络错误指数退避重试。最终失败 → 待确认（同原逻辑）。"""
    import base64
    b = {'model': C.MODEL, 'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(img).decode()}},
         {'type': 'text', 'text': C.PROMPT + title}]}], 'response_format': {'type': 'json_object'}, 'thinking': {'type': 'disabled'}, 'temperature': 0}
    err = ""
    for a in range(6):
        try:
            with _ARK_SEM:
                r = json.load(urllib.request.urlopen(urllib.request.Request(C.URL, json.dumps(b).encode(), {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + C.KEY}), timeout=120))
            t = r['choices'][0]['message']['content']; t = t[t.find('{'):t.rfind('}') + 1]
            return json.loads(t), r.get('usage', {})
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code != 429 and e.code < 500 and a >= 1: break      # 4xx（非 429）不无限重试
        except Exception as e: err = str(e)[:200]
        time.sleep(min(30, 2 ** a) + random.random())
    return {'分类': '待确认', '原因': '视觉调用失败', '说明': err}, {}
def _vision_hook(title, img):  # classify_one 内部调用：优先返回预先并发拿到的投票结果（按顺序）
    pre = getattr(_TL, "pre", None)
    return pre.pop(0) if pre else _ark_vision(title, img)
def _up_hook(u):
    pre = getattr(_TL, "img", None)
    return pre if pre else _ORIG_UP(u)
_ORIG_UP = C.up; C.vision = _vision_hook; C.up = _up_hook
def classify_parallel(o):
    """一条：下载图片 → 3 次投票并发 → 交给原 classify_one 汇总（判定逻辑零改动）。"""
    runs = C.ARGS.runs
    try:
        u, img = _ORIG_UP(o['image_url'])
        votes = [f.result() for f in [_VOTE_POOL.submit(_ark_vision, o['title'], img) for _ in range(runs)]]
        _TL.img, _TL.pre = (u, img), votes
        return C.classify_one(o, runs)
    finally: _TL.img = _TL.pre = None

def close_scraper(scraper):
    """根因（2026-10-19 23:01）：scrape_ehunt 是生成器，内部 `with sync_playwright()`。消费方（process_batch）中途抛异常时，
    生成器停在 yield 处没有被关闭 —— Playwright 的 asyncio 事件循环仍处于“运行中”并挂在主线程上，
    之后本进程每次 sync_playwright() 都报 “Playwright Sync API inside the asyncio loop”，直到重启。
    这里显式 close()：在 yield 处抛 GeneratorExit → finally 关闭浏览器上下文 → sync_playwright 正常退出。可重复调用。"""
    close = getattr(scraper, "close", None)
    if close:
        try: close()
        except Exception: log.warning("closing scraper failed", exc_info=True)

ASYNCIO_LOOP_MSG = "Playwright Sync API inside the asyncio loop"
EXIT_RECYCLE = 75   # deploy/box-selection-worker.sh 的守护循环看到任何退出码都会重启进程
class PlaywrightPoisoned(RuntimeError): pass
def playwright_leaked():
    """本线程是否残留了运行中的事件循环（sync_playwright 未正常退出的痕迹）。"""
    import asyncio
    try: return asyncio._get_running_loop() is not None
    except Exception: return False
def is_poisoned(e): return isinstance(e, PlaywrightPoisoned) or ASYNCIO_LOOP_MSG in str(e)

SEEN = {}  # run_id → 本进程已见过的 listing（含被排除的，只在内存）；跨批去重
IDLE_LOOKUP_EVERY = float(os.environ.get("SEL_IDLE_LOOKUP_SECONDS", "10"))
def process_batch(store, run, batch, scraper, idle=None):
    """抓取顺序进行；规则通过的条目丢进视觉线程池，不阻塞翻页。结果按抓取顺序（rank）在主线程依次落定：计数/进度/入库都只在主线程，天然线程安全。"""
    rid, bno = run["id"], batch["batch_no"]
    seen = SEEN.setdefault(rid, set()) | store.passed_ids(rid); SEEN[rid] = seen
    # 继续本批（sel_resume）：从上次停止时已落定的计数接着累加；抓取起点由 main() 从 offset_start + scraped_count 开始
    cnt = {k: int(batch.get(f"{k}_count") or 0) for k in ("scraped", "passed", "excluded", "dup")}; reasons = dict(batch.get("excluded_by_reason") or {}); calls = [0]; clock = threading.Lock()
    store.patch_batch(batch["id"], status="running", started_at=NOW())
    def flush():
        store.patch_batch(batch["id"], scraped_count=cnt["scraped"], passed_count=cnt["passed"], excluded_count=cnt["excluded"], dup_count=cnt["dup"], excluded_by_reason=reasons)
        store.patch_run(rid, heartbeat_at=NOW())
    def finalize(row, kind, val):   # kind: dup | rule | vision
        cnt["scraped"] += 1
        if kind == "dup": cnt["dup"] += 1
        elif kind == "rule":
            r = val; g = C.reason_group(r[1]); reasons[g] = reasons.get(g, 0) + 1; cnt["excluded"] += 1
            log_exclusion(run, bno, row, r[1], r[2], "兜底" if r[1] == "7天销量≤3" else "规则")
        else:
            o = val
            if o["结果"] == "排除":
                g = C.reason_group(o.get("原因", "")); reasons[g] = reasons.get(g, 0) + 1; cnt["excluded"] += 1
                log_exclusion(run, bno, row, o.get("原因"), o.get("说明"), "视觉")
            else:                                             # 只有符合要求的入库
                lid = str(row["listing_id"]); sp = lambda v: [x.strip() for x in (v or "").split(";") if x.strip()]
                store.insert_item({"run_id": rid, "batch_id": batch["id"], "user_id": run["user_id"], "batch_no": bno, "etsy_listing_id": int(lid),
                    "rank": row.get("rank"), "page": row.get("page"), "title": o["title"], "price_min": o["price_min"], "price_max": o["price_max"],
                    "total_sales": o["total_sales"], "sales_7d": o["sales_7d"], "image_url": o.get("image_url_hd") or o.get("image_url"),
                    "verdict": o["结果"], "reason": o.get("原因") or None, "note": o.get("备注") or None, "explain": o.get("说明"), "material": o.get("材质"),
                    "acrylic_fit": o.get("acrylic_fit") or None, "custom_type": o.get("定制类型"), "selling_points": sp(o.get("核心卖点")),
                    "occasions": sp(o.get("适用场合")), "votes": o.get("votes")})
                cnt["passed"] += 1
        if cnt["scraped"] % 5 == 0:   # 每 5 条落定刷新一次进度（网页进度条 = 本批 scraped_count / 本批条数）
            store.patch_batch(batch["id"], last_page=row.get("page")); flush()
        if cnt["scraped"] % 20 == 0: log.info("run %s batch %s: done=%s passed=%s excluded=%s dup=%s vision_calls=%s", rid, bno, cnt["scraped"], cnt["passed"], cnt["excluded"], cnt["dup"], calls[0])
    def vision_task(o):
        try:
            o2, usage, n = classify_parallel(o)
            with clock: calls[0] += n
            return o2
        except Exception as e: return {**o, "结果": "待确认", "备注": f"视觉失败：{str(e)[:80]}", "原因": "视觉调用失败"}   # 拿不准就保留
    base = cnt["scraped"]; prog = _PROG[0]; n_seen = 0
    def tick(force=False, **kw):
        if prog: prog.update(force=force, read=base + n_seen, judged=cnt["scraped"], vision_pending=sum(1 for _, k, _v in pend if k == "vision"),
                             passed=cnt["passed"], excluded=cnt["excluded"], dup=cnt["dup"], **kw)
    pend = deque()   # 按抓取顺序：(row, kind, value|future)
    def drain(block=False):
        while pend:
            row, kind, v = pend[0]
            if kind == "vision":
                if not block and not v.done(): return
                # 列表已读完、只剩等豆包（长批次可能几分钟）：浏览器已关闭，期间每 IDLE_LOOKUP_EVERY 秒穿插处理预筛选主图查询，不让查询干等到本批结束
                while block and idle and not v.done():
                    try: v.result(timeout=IDLE_LOOKUP_EVERY)
                    except FutureTimeout:
                        try: idle()
                        except PlaywrightPoisoned: raise
                        except Exception: log.warning("lookups while waiting for vision failed", exc_info=True)
                    except BaseException: break
                v = v.result()
            pend.popleft(); finalize(row, kind, v); tick()
    pool = ThreadPoolExecutor(max_workers=VISION_CONC, thread_name_prefix="vision")
    try:
        for row in scraper:
            if n_seen % 20 == 0 and (st_now := store.run(rid).get("status")) in ("canceled", "paused"):   # 037：暂停 = 停在断点，批次标记已停止，继续时从断点读
                for prow, k, v in pend:
                    if k == "vision": v.cancel()
                    if k != "dup": seen.discard(str(prow["listing_id"]))   # 未落定的行：继续本批时要重新读
                pool.shutdown(wait=True, cancel_futures=True); pend.clear()
                store.patch_batch(batch["id"], status="canceled", finished_at=NOW()); flush(); tick(force=True, stage=st_now); return st_now
            n_seen += 1; lid = str(row["listing_id"])
            if lid in seen: pend.append((row, "dup", None))       # 跨批/同批重复：不重复调用、不重复入库
            else:
                seen.add(lid); o = C.dedupe([row])[0]
                r = C.guard(o) or C.rule(o["title"])               # ① 标题规则 + 7 天销量兜底：命中即排除，不调豆包
                pend.append((row, "rule", r) if r else (row, "vision", pool.submit(vision_task, o)))   # ② 规则通过 → 豆包视觉×3（并发）
            drain(); tick(**({"stage": "reading"} if n_seen == 1 else {}))
        tick(stage="vision")   # 列表已读完：剩下的只是等豆包判断
        drain(block=True)
    except BaseException:
        # 失败：已落定的计数尽量写回（继续本批从 offset_start + scraped_count 接着读），未落定的豆包任务取消
        for prow, k, v in pend:
            if k == "vision": v.cancel()
            if k != "dup": seen.discard(str(prow["listing_id"]))
        pend.clear()
        try: flush(); tick(force=True, stage="canceled")
        except Exception: log.warning("flush after failure failed", exc_info=True)
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        close_scraper(scraper)   # 关键：生成器里的 with sync_playwright() 必须立即退出，否则事件循环残留（见 close_scraper）
    flush()
    # 本批完成：只在这里更新“还剩 N 条未读”
    run = store.run(rid); remaining = max(0, (run.get("max_rows") or 0) - batch["offset_end"])
    store.patch_batch(batch["id"], status="done", finished_at=NOW(), remaining_after=remaining); flush(); tick(force=True, stage="done")
    agg = {k: (run.get(f"{k}_count") or 0) + cnt[k] for k in ("scraped", "passed", "excluded")}
    rr = dict(run.get("excluded_by_reason") or {}); [rr.__setitem__(k, rr.get(k, 0) + v) for k, v in reasons.items()]
    upd = dict(scraped_count=agg["scraped"], passed_count=agg["passed"], excluded_count=agg["excluded"], excluded_by_reason=rr,
               vision_calls=(run.get("vision_calls") or 0) + calls[0], remaining_count=remaining)
    if remaining <= 0:   # 按条数结束（max_rows ≤ 5000）；“继续读取”后批次号可能超过 5
        store.patch_run(rid, status="succeeded", finished_at=NOW(), **upd)
    elif run.get("read_all"):  # 一键读取全部剩余：自动排下一批
        s0 = batch["offset_end"] + 1; e0 = min(batch["offset_end"] + (run.get("batch_size") or 1000), run["max_rows"])
        try:
            store.new_batch({"run_id": rid, "user_id": run["user_id"], "batch_no": bno + 1, "offset_start": s0, "offset_end": e0, "page_start": (s0 + 19) // 20, "page_end": (e0 + 19) // 20})
            paused = run.get("status") == "paused"   # 037：本批最后 20 行内点了暂停 → 下一批排好但保持暂停
            store.patch_run(rid, status="paused" if paused else "queued", current_batch=bno + 1, **upd)
        except urllib.error.HTTPError as e:
            # 例：024 的 batch_no ≤ 5 约束（先读 200 条再一键读取全部 4,800 → 第 6 批插不进去）。本批已完成，不能让外层把它改成“已停止”，
            # 否则「继续本批」会重排一个已读完的批次并再次失败。→ 本批完成 + 提示手动继续（035 去掉该约束后不再发生）
            log.warning("auto-queue batch %s failed: HTTP %s", bno + 1, e.code)
            store.patch_run(rid, status="batch_done", read_all=False, error=f"自动排第 {bno + 1} 批失败（HTTP {e.code}），本批结果已保存，可点「读取下一批」继续", **upd)
    else:
        store.patch_run(rid, status="batch_done", **upd)
    return "done"

def login(url):
    """一次性人工登录：有头浏览器开在 box 显示器上，等人登录完成后关闭窗口；不自动填写任何账号信息。"""
    from playwright.sync_api import sync_playwright
    os.environ.setdefault("DISPLAY", ":1"); os.makedirs(PROFILE, mode=0o700, exist_ok=True)
    with sync_playwright() as p:
        ctx = launch_ctx(p, headless=False, viewport={"width": 1440, "height": 900})
        pg = ctx.pages[0] if ctx.pages else ctx.new_page(); pg.goto(url)
        print(f"已在 DISPLAY={os.environ['DISPLAY']} 打开 {url}；请人工登录 EHunt，完成后关闭浏览器窗口（登录状态保存在 {PROFILE}）", flush=True)
        try: pg.wait_for_event("close", timeout=0)
        except Exception: pass
        try: ctx.close()
        except Exception: pass
    print("登录窗口已关闭，profile 已保存", flush=True)

WORDS_TTL = int(os.environ.get("SEL_WORDS_TTL", "300"))
LOOKUPS_ON = os.environ.get("EHUNT_WEB_LOOKUPS", "1") != "0"   # 预筛选主图查询开关
def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); ap.add_argument("--fake", help="回放 JSON，代替真实抓取")
    ap.add_argument("--login", action="store_true", help="一次性人工登录 EHunt（有头浏览器，box 显示器）")
    ap.add_argument("--login-url", default="https://ehunt.ai/etsy-product-research")
    ap.add_argument("--lookup-dry", help="逗号分隔 listing id：真实查询主图并打印，不连数据库（测试用）")
    a = ap.parse_args(argv); logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.login: return login(a.login_url)
    if a.lookup_dry: return lookup_dry([int(x) for x in a.lookup_dry.split(",") if x.strip()])
    store = Store(); last_purge = 0; last_words = 0
    if not a.fake: start_heartbeat(store)
    while True:
        if time.time() - last_purge > 86400:  # 启动时 + 每 24 小时：DB 3 天清理（pg_cron 不可用时的兜底）+ 本地排除日志清理
            try: log.info("purge db=%s local=%s", store.purge(), purge_local())
            except Exception: log.warning("purge failed", exc_info=True)
            try: log.info("purge lookups=%s", store.lookup_purge())
            except Exception: log.warning("lookup purge failed (migration 033 not applied?)")
            last_purge = time.time()
        if time.time() - last_words > WORDS_TTL:  # 排除词表：后台 sel_exclude_words，5 分钟刷新；表不可用 → 内置兜底
            try: rows = store.exclude_words(); log.info("exclude words: %s (%d rows)", C.set_words(rows), len(rows))
            except Exception: log.warning("exclude words load failed, using built-in", exc_info=True); C.set_words(None)
            last_words = time.time()
        lookups = LOOKUPS_ON and not a.fake
        PHASE[0] = "lookups"
        if lookups:   # 预筛选主图查询优先于选品（很快：每条约 3-5 秒）；表未部署（033 未执行）→ 本进程内关闭
            try: serve_lookups_browser(store)
            except urllib.error.HTTPError as e:
                if e.code in (404, 400): log.warning("ehunt_lookup_requests unavailable (HTTP %s), lookups disabled", e.code); globals()["LOOKUPS_ON"] = False
                else: log.warning("lookups failed", exc_info=True)
            except Exception as e:
                if is_poisoned(e): log.critical("Playwright poisoned during lookups; exiting %s for restart", EXIT_RECYCLE); recycle()
                log.warning("lookups failed", exc_info=True)
        run = store.claim()
        if run:
            log.info("claimed run %s kw=%s status=%s", run["id"], run["keyword"], run["status"])
            _PROG[0] = Progress(store, run["id"]); PHASE[0] = "counting" if run["status"] == "counting" else "batch"
            try: handle_run(store, run, a, lookups)
            finally: _PROG[0] = None
        PHASE[0] = "idle"
        if playwright_leaked():   # 兜底自检：任何路径漏关了 sync_playwright → 立即换新进程（守护脚本重启），绝不带病继续
            log.critical("asyncio loop still running after run (Playwright not closed); exiting %s for restart", EXIT_RECYCLE); recycle()
        if a.once: break
        time.sleep(POLL)

# box 心跳（007 worker_heartbeat，worker_id = box-selection；无需迁移）：独立线程每 HB_EVERY 秒写一次，
# 长时间抓取 / 豆包视觉批次期间也照常写 → ECS 端据此区分“box 离线（休眠/worker 没在跑）”与“box 在线但正忙”。
HB_ID = os.environ.get("SEL_HEARTBEAT_ID", "box-selection")
HB_EVERY = max(5, int(os.environ.get("SEL_HEARTBEAT_SECONDS", "30")))
PHASE = ["idle"]
def start_heartbeat(store, every=None):
    every = every or HB_EVERY; t0 = time.time()
    def loop():
        warned = False
        while True:
            try: store.heartbeat({"pid": os.getpid(), "phase": PHASE[0], "uptime_s": int(time.time() - t0), "lookups": LOOKUPS_ON}); warned = False
            except Exception as e:
                if not warned: log.warning("heartbeat failed: %s", _brief(e) if is_transient(e) else str(e)[:120]); warned = True
            time.sleep(every)
    th = threading.Thread(target=loop, name="heartbeat", daemon=True); th.start(); return th

def recycle(): logging.shutdown(); os._exit(EXIT_RECYCLE)

def handle_run(store, run, a, lookups):
    """处理一条认领到的 run。Playwright 被污染（asyncio loop 报错）→ 把 run 放回队列后退出进程，由守护脚本重启；
    其他失败：查询数量 → failed（用户重新查询）；抓取批次 → 本批标记“已停止”+原因，网页「继续本批」从断点接着读。"""
    if run["status"] == "counting":
        stage("count_open")
        try:
            n = len(json.load(open(a.fake))) if a.fake else count_ehunt(run["keyword"], run.get("params") or {})
            store.patch_run_if(run["id"], "counting", status="counted", total_count=n, counted_at=NOW(), heartbeat_at=NOW())
            if _PROG[0]: _PROG[0].update(force=True, stage="count_done", total=n)
        except Exception as e:
            if is_poisoned(e):
                log.critical("Playwright poisoned during count; requeue run %s and exit for restart", run["id"])
                _safe(store.patch_run, run["id"], status="count_queued", locked_by=None, error=None); recycle()
            log.exception("count failed"); _safe(store.patch_run_if, run["id"], "counting", status="failed", error=str(e)[:300], finished_at=NOW())
        return
    b = store.open_batch(run["id"])
    if not b: return store.patch_run(run["id"], status="failed", error="没有待读批次", finished_at=NOW())
    sc = None
    try:
        s0 = b["offset_start"] + int(b.get("scraped_count") or 0)   # 继续本批：跳过已落定的行
        if b.get("scraped_count"): log.info("resume batch %s from rank %s", b["batch_no"], s0)
        k0 = int(b.get("scraped_count") or 0)   # 新批次：从空进度开始（不沿用上一批的计数）
        if _PROG[0]: _PROG[0].state = {}
        stage("read_setup", batch_no=b["batch_no"], target=b["offset_end"] - b["offset_start"] + 1, read=k0, judged=k0,
              vision_pending=0, passed=int(b.get("passed_count") or 0), excluded=int(b.get("excluded_count") or 0), dup=int(b.get("dup_count") or 0), page=(s0 - 1) // 20 + 1)
        sc = fake_scrape(a.fake, s0, b["offset_end"]) if a.fake else scrape_ehunt(run["keyword"], run.get("params") or {}, s0, b["offset_end"],
                                                                               between=(lambda c: serve_lookups_browser(store, c)) if lookups and LOOKUPS_ON else None,
                                                                               should_stop=lambda: store.run(run["id"]).get("status") in ("canceled", "paused"))
        process_batch(store, run, b, sc, idle=(lambda: serve_lookups_browser(store)) if lookups and LOOKUPS_ON and not a.fake else None)
    except RunStopped:   # 重试间隙用户取消/暂停：同 process_batch 的暂停路径 —— 批次“已停止”，run 状态保持用户设的（不写错误）
        log.info("run %s stopped by user during page retry", run["id"])
        _safe(store.patch_batch, b["id"], status="canceled", finished_at=NOW())
        st_now = (_safe(store.run, run["id"]) or {}).get("status") or "canceled"
        if _PROG[0]: _safe(_PROG[0].update, force=True, stage=st_now, retry=0, retry_why=None)
    except Exception as e:
        close_scraper(sc)
        if is_poisoned(e):
            log.critical("Playwright poisoned during batch; requeue run %s and exit for restart", run["id"])
            _safe(store.patch_batch, b["id"], status="queued"); _safe(store.patch_run, run["id"], status="queued", locked_by=None); recycle()
        log.exception("batch failed")
        msg = f"读取中断（{_brief(e) if is_transient(e) else str(e)[:200]}），已读部分已保留，可点「继续本批」接着读"
        _safe(store.patch_batch, b["id"], status="canceled", finished_at=NOW())          # 029 sel_resume 只恢复“已停止”的批次
        _safe(store.patch_run, run["id"], status="canceled", error=msg[:300], finished_at=NOW())
    finally:
        close_scraper(sc)

def _safe(fn, *a, **kw):
    try: return fn(*a, **kw)
    except Exception: log.warning("%s failed", getattr(fn, "__name__", "call"), exc_info=True)

if __name__ == "__main__": main()
