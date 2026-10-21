"""产品库评论日期自动抓取（049）：认领 listing_scrape_status=pending → 抓 Etsy 评论日期 → finish_review_scrape 写 listing_reviews + ok/failed。

运行位置：本 box，独立进程（不在 ehunt_worker 进程/线程里 → 不会阻塞 EHunt 获取链接 / 预筛选队列）。ECS 访问不到 etsy.com，所以不放在 analyzer。
低优先级：nice、每轮最多 REVIEW_BATCH（默认 3）个、每个之间间隔 REVIEW_GAP_S（默认 8s）；用浏览器抓取时，
EHunt 获取链接 / 计数 / 预筛选查询有任务在跑就跳过本轮（让出 CPU/网络）。
抓取方式（自动选择）：
  - ETSY_API_KEY 已配置 → Etsy Open API v3 getReviewsByListing（create_timestamp），source='etsy_api'（推荐，稳定）
  - 否则 → Playwright 打开商品页读评论日期（自己的 profile：REVIEW_PROFILE_DIR，不碰 EHunt profile / 桌面浏览器），source='etsy_page'
    注意：2026-10-21 实测 box 出口 IP 打开 etsy.com 被 DataDome 返回 403 → 记为 failed「Etsy 反爬拦截」，用户可点重试或用截图/手动兜底。
不重复抓取：只处理 pending（新加入/回填/用户点重试）及卡死 >15 分钟的 fetching；ok/failed 不会自动再抓。
用法：python -m selection.review_scraper            # 常驻
      python -m selection.review_scraper --once     # 处理一轮
      python -m selection.review_scraper --dry-run 1779663270 1670371447   # 只抓取并打印，不读写数据库
环境：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY（不打印）；可选 ETSY_API_KEY、REVIEW_BATCH、REVIEW_GAP_S、REVIEW_POLL_S、REVIEW_DAYS
"""
import argparse, datetime as dt, json, logging, os, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyzer.review_ocr import parse_reviews  # noqa: E402  同一套 'Oct 13, 2026' / '2026年10月13日' 日期解析

log = logging.getLogger("reviews")
BATCH = int(os.environ.get("REVIEW_BATCH", "3"))
GAP_S = float(os.environ.get("REVIEW_GAP_S", "8"))
POLL_S = float(os.environ.get("REVIEW_POLL_S", "60"))
DAYS = int(os.environ.get("REVIEW_DAYS", "200"))      # 速度只看近 6 个月，多取一点余量
MAX_PAGES = int(os.environ.get("REVIEW_MAX_PAGES", "8"))
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
    """商品页评论区文本 → 日期（每行可能有一个 'Name Oct 13, 2026'）。"""
    return [d for _, d in parse_reviews(text, today)]


def is_blocked(status, html):
    h = (html or "")[:20000].lower()
    return status in (403, 429) or "captcha-delivery" in h or "datadome" in h


class BrowserFetcher:
    source = "etsy_page"
    uses_browser = True

    def __init__(self):
        self._p = self._ctx = None

    def _open(self):
        if not self._ctx:
            from playwright.sync_api import sync_playwright
            self._p = sync_playwright().start()
            self._ctx = self._p.chromium.launch_persistent_context(PROFILE, headless=True, locale="en-US", viewport={"width": 1366, "height": 900})
        return self._ctx

    def close(self):
        try:
            if self._ctx: self._ctx.close()
            if self._p: self._p.stop()
        finally:
            self._p = self._ctx = None

    def __call__(self, lid):
        pg = self._open().new_page()
        try:
            r = pg.goto(f"https://www.etsy.com/listing/{int(lid)}", wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(2500)
            if is_blocked(r.status if r else None, pg.content()):
                raise FetchError(f"Etsy 反爬拦截（HTTP {r.status if r else '?'}）")
            if r and r.status == 404:
                raise FetchError("Etsy 商品不存在或已下架 (404)")
            cutoff, seen = dt.date.today() - dt.timedelta(days=DAYS), []
            for _ in range(MAX_PAGES):
                box = pg.locator("#reviews, [data-reviews-container], [data-region='reviews']").first
                txt = box.inner_text(timeout=8000) if box.count() else pg.inner_text("body")
                ds = page_dates(txt)
                seen += ds
                nxt = pg.locator("#reviews a[aria-label*='Next'], [data-reviews-pagination] a[aria-label*='Next'], #reviews button[aria-label*='Next']").first
                if not ds or min(ds) < cutoff or not nxt.count() or not nxt.is_enabled():
                    break
                nxt.click(); pg.wait_for_timeout(1500)
            return sorted([d for d in seen if d >= cutoff], reverse=True)
        finally:
            pg.close()


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
    def ehunt_busy(self):
        """EHunt 获取链接/计数在跑，或预筛选有主图查询排队 → 让路。"""
        a = self._req("GET", "/rest/v1/sel_runs?status=in.(count_queued,counting,queued,running)&select=id&limit=1") or []
        b = self._req("GET", "/rest/v1/ehunt_lookup_requests?status=in.(queued,running)&select=listing_id&limit=1") or []
        return bool(a or b)


def make_fetcher():
    k = os.environ.get("ETSY_API_KEY", "").strip()
    return ApiFetcher(k) if k else BrowserFetcher()


def run_once(store, fetch, batch=BATCH, gap=GAP_S):
    """一轮：让路检查 → 认领 ≤batch → 逐个抓取写回。返回处理数量。"""
    if getattr(fetch, "uses_browser", False) and store.ehunt_busy():
        log.info("EHunt 队列忙，本轮跳过"); return 0
    ids = store.claim(batch)
    for i, lid in enumerate(ids):
        try:
            dates = fetch(lid)
            store.finish(lid, "ok", dates, fetch.source)
            log.info("reviews %s → ok (%d dates)", lid, len(dates))
        except Exception as e:  # noqa: BLE001  任何异常都写 failed（不留在 fetching）
            msg = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {str(e)[:200]}"
            store.finish(lid, "failed", None, fetch.source, msg)
            log.warning("reviews %s → failed: %s", lid, msg)
        if gap and i < len(ids) - 1:
            _sleep(gap)
    return len(ids)


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); ap.add_argument("--dry-run", nargs="+", metavar="LISTING_ID")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fetch = make_fetcher()
    try:
        if a.dry_run:   # 不读写数据库
            for lid in a.dry_run:
                try:
                    ds = fetch(lid); print(f"{lid}: ok via {fetch.source}, {len(ds)} dates (近 {DAYS} 天)" + (f", newest {ds[0]}, oldest {ds[-1]}" if ds else ""))
                except FetchError as e:
                    print(f"{lid}: failed via {fetch.source}: {e}")
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
