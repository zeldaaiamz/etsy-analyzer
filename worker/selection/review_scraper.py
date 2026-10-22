"""产品库评论日期自动抓取（049）：认领 listing_scrape_status=pending → 读 Etsy 商品页评论日期 → finish_review_scrape 写 listing_reviews + ok/failed。

运行位置：本 box，独立进程（不在 ehunt_worker 进程里 → 不阻塞 EHunt 获取链接 / 预筛选队列）。ECS 访问不到 etsy.com，所以不放在 analyzer。
抓取方式（自动选择）：有 ETSY_API_KEY → Etsy Open API v3（source='etsy_api'）；否则（当前）→ 有头 Chrome 读网页（source='etsy_page'）：
  1 有头 Chrome（channel=chrome）+ 自己的持久化 profile REVIEW_PROFILE_DIR（不碰 EHunt profile / 桌面浏览器），专用显示 REVIEW_DISPLAY（默认 :77，没有就自己起 Xvfb）
  2 首页预热（会话开始、每 10 个商品、被拦截恢复后）
  3 商品页 →「View all reviews for this item」（只接受弹窗标题 Reviews for this item，否则 failed「仅店铺评论」）→ Most recent → 每页 8 条，
    遇到早于 REVIEW_DAYS（200）天的评论 / REVIEW_MAX_PAGES（20）页 / 最后一页即停；每条评论只取评论人旁的日期
  4 节奏：商品之间 30–60s，翻页 3–6s；一个会话最多 REVIEW_SESSION_PAGES（30）页，然后冷却 20–30 分钟
  5 被拦截（403/验证）→ 该商品 failed「Etsy 反爬拦截」，整个 worker 暂停 30–60 分钟；本轮剩余的放回 pending
  6 低优先级：nice 10；EHunt 获取链接/计数/预筛选查询忙时跳过本轮
  7 每日上限 REVIEW_DAILY_CAP（60）个
  显示不可用 → 不认领、暂停 10 分钟；抓取中途丢失 → 该商品 failed + 暂停（不崩溃）。
不重复抓取：只处理 pending（新加入/回填/用户点重试）及卡死 >15 分钟的 fetching；ok/failed 不会自动再抓。
用法：python -m selection.review_scraper            # 常驻
      python -m selection.review_scraper --once     # 处理一轮
      python -m selection.review_scraper --dry-run 1346032769 1779663270 --json out.json   # 只抓取并打印，不读写数据库
环境：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY（不打印）；可选 ETSY_API_KEY、REVIEW_*（见上）
"""
import argparse, datetime as dt, json, logging, os, random, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyzer.review_ocr import parse_reviews  # noqa: E402  同一套 'Oct 13, 2026' / '2026年10月13日' 日期解析

log = logging.getLogger("reviews")
BATCH = int(os.environ.get("REVIEW_BATCH", "3"))
POLL_S = float(os.environ.get("REVIEW_POLL_S", "60"))
DAYS = int(os.environ.get("REVIEW_DAYS", "200"))      # 速度只看近 6 个月，多取一点余量
MAX_PAGES = int(os.environ.get("REVIEW_MAX_PAGES", "20"))      # 每页 8 条 → 最多 160 条/商品（约 1k 评论、每月 25 条的商品够 6 个月）
SESSION_PAGES = int(os.environ.get("REVIEW_SESSION_PAGES", "30"))  # 一个浏览器会话最多看这么多页，然后冷却
COOL_S = (20 * 60, 30 * 60)                                         # 会话冷却 20–30 分钟
BLOCK_PAUSE_S = (30 * 60, 60 * 60)                                  # 403 后暂停 30–60 分钟
LISTING_GAP_S = (float(os.environ.get("REVIEW_GAP_MIN_S", "30")), float(os.environ.get("REVIEW_GAP_MAX_S", "60")))
WARM_EVERY = 10                                                     # 每 10 个商品回首页预热一次
DAILY_CAP = int(os.environ.get("REVIEW_DAILY_CAP", "60"))
DISPLAY = os.environ.get("REVIEW_DISPLAY", ":77")                   # 专用 Xvfb（agent 桌面 :1/:4… 随会话消失，不能依赖）
START_XVFB = os.environ.get("REVIEW_START_XVFB", "1") not in ("0", "false", "no")
DISPLAY_PAUSE_S = 10 * 60
PROFILE = os.environ.get("REVIEW_PROFILE_DIR", os.path.expanduser("~/.local/share/etsy-selection/reviews-profile"))
API = "https://openapi.etsy.com/v3/application"
_sleep = time.sleep


class FetchError(RuntimeError):
    """抓取失败（写 failed + error，等用户重试）。"""


def _get_json(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise FetchError({404: "Etsy 商品不存在或已下架 (404)", 429: "Etsy API 限流 (429)", 403: "Etsy API 拒绝 (403，检查 key)"}.get(e.code, f"Etsy API HTTP {e.code}")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"网络错误：{type(e).__name__}") from None


class ApiFetcher:
    source = "etsy_api"
    uses_browser = False

    def __init__(self, key, get=_get_json, today=None):
        self.h, self.get, self.today = {"x-api-key": key}, get, today

    def __call__(self, lid):
        cutoff = (self.today or dt.date.today()) - dt.timedelta(days=DAYS)
        out, off = [], 0
        for _ in range(MAX_PAGES):
            d = self.get(f"{API}/listings/{int(lid)}/reviews?" + urllib.parse.urlencode({"limit": 100, "offset": off}), self.h) or {}
            res = d.get("results") or []
            ds = [dt.datetime.fromtimestamp(int(r["create_timestamp"]), dt.timezone.utc).date() for r in res if r.get("create_timestamp")]
            out += [x for x in ds if x >= cutoff]
            off += len(res)
            if len(res) < 100 or (ds and min(ds) < cutoff) or off >= (d.get("count") or 0):
                break
        return sorted(out, reverse=True)


def page_dates(text, today=None):
    """评论文本 → 日期（每行可能有一个 'Name Oct 13, 2026'）。"""
    return [d for _, d in parse_reviews(text, today)]


def is_blocked(status, html):
    h = (html or "")[:20000].lower()
    return status in (403, 429) or "captcha-delivery" in h or "datadome" in h


class Blocked(FetchError):
    """Etsy 反爬拦截 → 该 listing failed，整个 worker 暂停 30–60 分钟。"""


class DisplayUnavailable(FetchError):
    """有头浏览器没有可用显示 → 不认领；抓取中途丢失 → 该 listing failed + 暂停。"""


_ITEM_HDR = re.compile(r"reviews\s+for\s+this\s+item\s*\(?\s*([\d,.]+\s*[kK]?)?", re.I)
_SHOP_HDR = re.compile(r"reviews\s+for\s+this\s+shop\s*\(?\s*([\d,.]+\s*[kK]?)?", re.I)
def _count(x):
    """'80' → 80；'1,234' → 1234；'1k' / '1.2k' → 1000 / 1200（Etsy 大数只显示到 k）。"""
    if not x: return None
    x = x.strip().replace(",", "")
    try: return int(float(x[:-1]) * 1000) if x[-1] in "kK" else int(float(x))
    except ValueError: return None
def heading_kind(text):
    """弹窗标题 → ('item'|'shop'|None, 数量或 None)。只接受 item（商品评论）；shop = 店铺评论（含其它商品）。"""
    for kind, rx in (("item", _ITEM_HDR), ("shop", _SHOP_HDR)):
        m = rx.search(text or "")
        if m: return kind, _count(m.group(1))
    return None, None


def read_listing(nav, lid, today=None, days=None, max_pages=None, log_pages=None):
    """一个商品：打开商品页 → 「View all reviews for this item」→ Most recent → 逐页读日期（每页 8 条），
    遇到早于 days 天的评论 / max_pages 页 / 最后一页即停。nav 为浏览器驱动（测试里用假的）。返回 (日期新→旧, info)。"""
    today = today or dt.date.today(); cutoff = today - dt.timedelta(days=days or DAYS)
    st = nav.open_listing(lid)
    if is_blocked(st, nav.html()): raise Blocked(f"Etsy 反爬拦截（HTTP {st}）")
    if st == 404: raise FetchError("Etsy 商品不存在或已下架 (404)")
    if st != 200: raise FetchError(f"Etsy 商品页 HTTP {st}")
    info = {"header_count": nav.shop_count(), "pages": 0}   # 商品页评分旁「(N)」（打开的是店铺评论弹窗）
    hdr = nav.open_item_reviews()
    if hdr is None:
        if nav.has_shop_reviews(): raise FetchError("仅店铺评论（商品页没有「View all reviews for this item」）")
        if "taking a short break" in (nav.html() or "").lower() or "taking a short break" in (getattr(nav, "body_text", lambda: "")() or "").lower():
            raise FetchError("店铺暂停营业（页面不显示评论）")
        raise FetchError("商品暂无评论区")
    kind, n = heading_kind(hdr)
    if kind != "item": raise FetchError(f"仅店铺评论（弹窗标题：{(hdr or '')[:40]}）")
    info["item_count"] = n
    if not nav.sort_recent(): raise FetchError("找不到「Most recent」排序")
    out = []
    for i in range(max_pages or MAX_PAGES):
        ds = nav.page_dates(today); info["pages"] = i + 1
        if i == 0 and not ds: raise FetchError("评论弹窗里没读到日期（页面结构可能变了）")   # 不把读取失败当成「0 条」
        if log_pages is not None: log_pages.append(ds)
        if is_blocked(None, nav.html()): raise Blocked("Etsy 反爬拦截（翻页时出现验证）")
        out += [d for d in ds if d >= cutoff]
        if not ds or min(ds) < cutoff: break
        if i == (max_pages or MAX_PAGES) - 1: info["truncated"] = True; break   # 页数上限：近 6 个月可能不完整
        if not nav.next_page(): break
    return sorted(out, reverse=True), info


class PlaywrightNav:
    """真实浏览器驱动（有头 Chrome + 自己的持久化 profile；人类节奏）。"""
    def __init__(self, page, gap=(3, 6), sleep=None):
        self.pg, self.gap, self.sleep = page, gap, sleep or time.sleep
        self.views = 0
    def _pause(self, a=None, b=None): self.sleep(random.uniform(a or self.gap[0], b or self.gap[1]))
    def warm_up(self):
        r = self.pg.goto("https://www.etsy.com/", wait_until="domcontentloaded", timeout=60000); self.views += 1
        self._pause(5, 8); self.pg.mouse.move(400 + random.random() * 300, 300); self.pg.mouse.wheel(0, 500 + random.random() * 400); self._pause(3, 5)
        if is_blocked(r.status if r else None, self.pg.content()): raise Blocked(f"Etsy 反爬拦截（首页 HTTP {r.status if r else '?'}）")
    def open_listing(self, lid):
        r = self.pg.goto(f"https://www.etsy.com/listing/{int(lid)}", wait_until="domcontentloaded", timeout=60000, referer="https://www.etsy.com/")
        self.views += 1; self._pause(5, 8); return r.status if r else None
    def html(self): return self.pg.content()[:20000]
    def body_text(self):
        try: return self.pg.inner_text("body", timeout=5000)[:3000]
        except Exception: return ""  # noqa: BLE001
    def shop_count(self):
        t = self.pg.locator("[data-reviews-deep-dive-trigger]").first
        try: return int(re.sub(r"[^\d]", "", t.inner_text(timeout=3000)) or 0) if t.count() else None
        except Exception: return None  # noqa: BLE001
    def has_shop_reviews(self): return self.pg.locator("[data-reviews-deep-dive-trigger]").count() > 0
    def _dlg(self): return self.pg.locator(".deep-dive-sheet").last
    def open_item_reviews(self):
        r = self.pg.locator("#reviews").first   # 评论区异步加载：先滚到评论区
        if r.count(): r.scroll_into_view_if_needed(); self._pause(2, 4)
        b = self.pg.locator("[data-view-all-reviews-button]").first
        if not b.count(): return None
        b.scroll_into_view_if_needed(); self._pause(1, 2); b.click(); self._pause()
        h = self._dlg().locator("h3, h2").first
        return h.inner_text(timeout=10000) if h.count() else None
    def sort_recent(self):
        d = self._dlg(); trig = d.locator("[data-wt-menu-trigger]").first
        if not trig.count(): return False
        trig.click(); self._pause(1, 2)
        it = d.locator("[data-sort-option='Recency'], button:has-text('Most recent')").first
        if not it.count(): return False
        first = d.locator("[data-review-region]").first.get_attribute("data-review-region") if d.locator("[data-review-region]").count() else None
        it.click(); self.views += 1; self._pause()
        try: self.pg.wait_for_function("f => { const e = document.querySelectorAll('.deep-dive-sheet [data-review-region]'); return e.length && e[0].getAttribute('data-review-region') !== f; }", arg=first, timeout=8000)
        except Exception: pass  # noqa: BLE001  最新的正好排第一时列表首条不变
        return True
    def page_dates(self, today=None):
        if os.environ.get("REVIEW_SHOT_DIR"):   # e2e 证据：每页截图
            self._shot = getattr(self, "_shot", 0) + 1; self._dlg().screenshot(path=os.path.join(os.environ["REVIEW_SHOT_DIR"], f"{self._shot:03d}.png"))
        cards = self._dlg().locator("[data-review-region]")
        out = []
        for i in range(cards.count()):
            ds = page_dates(cards.nth(i).inner_text(timeout=5000), today)
            if ds: out.append(ds[0])   # 每条评论只取第一个日期（评论人旁），避免正文里的日期
        return out
    def next_page(self):
        d = self._dlg(); n = d.locator("button:has-text('Next'), a:has-text('Next')").last
        if not n.count() or not n.is_enabled() or (n.get_attribute("aria-disabled") or "") == "true": return False
        first = d.locator("[data-review-region]").first.get_attribute("data-review-region")
        n.click(); self.views += 1; self._pause()
        try: self.pg.wait_for_function("f => { const e = document.querySelectorAll('.deep-dive-sheet [data-review-region]'); return e.length && e[0].getAttribute('data-review-region') !== f; }", arg=first, timeout=10000)
        except Exception: return False  # noqa: BLE001  翻页没变化 → 视为最后一页
        return True


def display_ok(display):
    n = (display or "").lstrip(":").split(".")[0]
    return bool(n.isdigit() and os.path.exists(f"/tmp/.X11-unix/X{n}"))


def ensure_display(display, start=True, spawn=subprocess.Popen, wait=2.0):
    """有头浏览器的显示：已有就用；没有且允许时自己起一个 Xvfb（不依赖 agent 桌面，那些会随会话消失）；仍不可用 → False。"""
    if display_ok(display): return True
    if not start: return False
    try:
        spawn(["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    for _ in range(int(wait * 10)):
        if display_ok(display): return True
        _sleep(0.1)
    return display_ok(display)


class BrowserFetcher:
    """有头 Chrome、持久化 profile、首页预热、人类节奏、会话页数上限 + 冷却、403 暂停、每日上限。"""
    source = "etsy_page"
    uses_browser = True

    def __init__(self, clock=time.time, nav_factory=None):
        self.clock, self.nav_factory = clock, nav_factory
        self._p = self._ctx = self.nav = None
        self.pause_until = 0.0; self.pause_reason = ""
        self.session_listings = 0; self.day = None; self.day_count = 0

    # ---- 节流状态 ----
    def ready(self):
        """可以认领吗？返回 (bool, 原因)。"""
        now = self.clock()
        if now < self.pause_until: return False, f"暂停中（{self.pause_reason}，还剩 {int(self.pause_until - now)}s）"
        today = time.strftime("%F", time.localtime(now))
        if self.day != today: self.day, self.day_count = today, 0
        if self.day_count >= DAILY_CAP: return False, f"今日已抓 {self.day_count} 个（上限 {DAILY_CAP}）"
        if self.nav_factory is None and not ensure_display(DISPLAY, START_XVFB):
            self.pause(DISPLAY_PAUSE_S, f"显示 {DISPLAY} 不可用"); return False, self.pause_reason
        return True, ""
    def pause(self, secs, why):
        self.pause_until, self.pause_reason = self.clock() + secs, why; self.close()
        log.warning("reviews worker 暂停 %ds：%s", secs, why)

    def _open(self):
        if self.nav is None:
            if self.nav_factory: self.nav = self.nav_factory()
            else:
                if not display_ok(DISPLAY): raise DisplayUnavailable(f"显示 {DISPLAY} 不可用")
                os.environ["DISPLAY"] = DISPLAY
                from playwright.sync_api import sync_playwright
                try:
                    self._p = sync_playwright().start()
                    self._ctx = self._p.chromium.launch_persistent_context(PROFILE, channel=os.environ.get("REVIEW_CHROME_CHANNEL", "chrome"), headless=False, locale="en-US",
                        viewport={"width": 1366, "height": 900}, args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"])
                except Exception as e:  # noqa: BLE001
                    self.close()
                    if "display" in str(e).lower() or "x server" in str(e).lower(): raise DisplayUnavailable(f"浏览器无法打开显示 {DISPLAY}") from None
                    raise
                self.nav = PlaywrightNav(self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page())
            self.session_listings = 0
            self.nav.warm_up()
        elif self.session_listings and self.session_listings % WARM_EVERY == 0:
            self.nav.warm_up()
        return self.nav

    def close(self):
        try:
            if self._ctx: self._ctx.close()
            if self._p: self._p.stop()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._p = self._ctx = self.nav = None

    def __call__(self, lid, log_pages=None):
        try:
            nav = self._open()
            dates, info = read_listing(nav, lid, log_pages=log_pages)
            self.last_info = info
            return dates
        except Blocked as e:
            self.pause(random.uniform(*BLOCK_PAUSE_S), str(e)); raise
        except DisplayUnavailable as e:
            self.pause(DISPLAY_PAUSE_S, str(e)); raise
        except FetchError:
            raise
        except Exception as e:  # noqa: BLE001  浏览器崩溃/显示丢失等：本条 failed，关浏览器，下条重开
            msg = str(e).lower(); self.close()
            if "display" in msg or "x server" in msg or "target page, context or browser has been closed" in msg:
                self.pause(DISPLAY_PAUSE_S, "浏览器/显示异常"); raise DisplayUnavailable(f"浏览器/显示异常：{type(e).__name__}") from None
            raise
        finally:
            self.session_listings += 1; self.day_count += 1
            if self.nav is not None and getattr(self.nav, "views", 0) >= SESSION_PAGES:
                self.pause(random.uniform(*COOL_S), f"本会话已看 {self.nav.views} 页，冷却")


class Store:
    """最小 PostgREST 客户端（service_role，不打印）。"""
    def __init__(self, url=None, key=None):
        self.u = (url or os.environ["SUPABASE_URL"]).rstrip("/"); self.k = key or os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    def _req(self, method, path, body=None, prefer="return=representation"):
        r = urllib.request.Request(self.u + path, None if body is None else json.dumps(body, default=str).encode(), method=method,
                                   headers={"apikey": self.k, "Authorization": "Bearer " + self.k, "Content-Type": "application/json", "Prefer": prefer})
        with urllib.request.urlopen(r, timeout=60) as f:
            t = f.read(); return json.loads(t) if t else None

    def claim(self, n): return [int(x) for x in (self._req("POST", "/rest/v1/rpc/claim_review_scrapes", {"p_limit": n}) or [])]
    def finish(self, lid, status, dates=None, source="etsy_page", error=None):
        self._req("POST", "/rest/v1/rpc/finish_review_scrape", {"p_listing": int(lid), "p_status": status, "p_dates": [d.isoformat() for d in (dates or [])],
                                                                 "p_source": source, "p_error": error}, "return=minimal")
    def requeue(self, lid):  # 认领后没来得及抓（暂停/冷却）→ 放回 pending，attempts 不算
        self._req("POST", "/rest/v1/rpc/requeue_review_scrape", {"p_listing": int(lid)}, "return=minimal")
    def ehunt_busy(self, now=None):
        """EHunt 获取链接/计数在跑（15 分钟内有心跳），或近 15 分钟有排队/进行中的任务或预筛选主图查询 → 让路。
        选品 worker 没在跑时留下的陈旧队列（如 11:07 起没人处理的 lookup）不算忙，否则评论抓取会被永远挡住。"""
        since = urllib.parse.quote(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) - 15 * 60)))
        a = self._req("GET", f"/rest/v1/sel_runs?status=in.(count_queued,counting,queued,running)&or=(heartbeat_at.gte.{since},created_at.gte.{since},queue_at.gte.{since})&select=id&limit=1") or []
        b = self._req("GET", f"/rest/v1/ehunt_lookup_requests?status=in.(queued,running)&or=(requested_at.gte.{since},claimed_at.gte.{since})&select=listing_id&limit=1") or []
        return bool(a or b)


def make_fetcher():
    k = os.environ.get("ETSY_API_KEY", "").strip()
    return ApiFetcher(k) if k else BrowserFetcher()


def run_once(store, fetch, batch=BATCH, gap=None):
    """一轮：节流/显示检查 → 让路检查 → 认领 ≤batch → 逐个抓取写回。返回处理数量。"""
    ok, why = fetch.ready() if hasattr(fetch, "ready") else (True, "")
    if not ok:
        log.info("reviews 本轮跳过：%s", why); return 0
    if getattr(fetch, "uses_browser", False) and store.ehunt_busy():
        log.info("EHunt 队列忙，本轮跳过"); return 0
    ids = store.claim(batch)
    for i, lid in enumerate(ids):
        if i and hasattr(fetch, "ready") and not fetch.ready()[0]:   # 中途被拦截/冷却 → 余下的放回 pending（不算失败）
            for rest in ids[i:]: store.requeue(rest)
            break
        try:
            dates = fetch(lid)
            store.finish(lid, "ok", dates, fetch.source)
            log.info("reviews %s → ok (%d dates) %s", lid, len(dates), getattr(fetch, "last_info", ""))
        except Exception as e:  # noqa: BLE001  任何异常都写 failed（不留在 fetching）
            msg = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {str(e)[:200]}"
            store.finish(lid, "failed", None, fetch.source, msg)
            log.warning("reviews %s → failed: %s", lid, msg)
        if i < len(ids) - 1:
            _sleep(gap if gap is not None else random.uniform(*LISTING_GAP_S))
    return len(ids)


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); ap.add_argument("--dry-run", nargs="+", metavar="LISTING_ID")
    ap.add_argument("--json", help="dry-run：把每个 listing 的日期/页/数量写到这个 JSON 文件")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fetch = make_fetcher()
    try:
        if a.dry_run:   # 不读写数据库
            res = {}
            for i, lid in enumerate(a.dry_run):
                ok, why = fetch.ready() if hasattr(fetch, "ready") else (True, "")
                if not ok: print(f"{lid}: skipped: {why}"); res[lid] = {"status": "skipped", "error": why}; continue
                if i: _sleep(random.uniform(*LISTING_GAP_S))
                t0, pages = time.time(), []
                try:
                    ds = fetch(lid, log_pages=pages) if isinstance(fetch, BrowserFetcher) else fetch(lid)
                    info = getattr(fetch, "last_info", {})
                    res[lid] = {"status": "ok", "dates": [d.isoformat() for d in ds], **info, "page_dates": [[d.isoformat() for d in p] for p in pages], "secs": round(time.time() - t0, 1)}
                    print(f"{lid}: ok via {fetch.source}, {len(ds)} dates (近 {DAYS} 天)" + (f", newest {ds[0]}, oldest {ds[-1]}" if ds else "") + f" {info} {res[lid]['secs']}s")
                except FetchError as e:
                    res[lid] = {"status": "failed", "error": str(e), "secs": round(time.time() - t0, 1)}
                    print(f"{lid}: failed via {fetch.source}: {e}")
            if a.json: Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1))
            return 0
        store = Store()
        os.nice(10) if hasattr(os, "nice") else None
        while True:
            try:
                n = run_once(store, fetch)
            except Exception as e:  # noqa: BLE001  数据库/网络瞬时错误：下轮再来
                log.warning("round error: %s", type(e).__name__); n = 0
            if a.once: return 0
            if getattr(fetch, "close", None) and not n: fetch.close()   # 空闲时关浏览器
            _sleep(POLL_S if not n else 1)
    finally:
        getattr(fetch, "close", lambda: None)()


if __name__ == "__main__":
    sys.exit(main())
