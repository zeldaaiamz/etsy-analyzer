"""EHunt Etsy 数据 API（第三方）。
POST https://api.ehunt.ai/api/v1/items  header X-VIP-TOKEN，body {"search_key": <listing url>, "page_size": 1}
响应 {code:200, message, data:{list:[...]}, quota:{used_today, remaining_today}}；1 credit/商品，免费 200/天。
注意：EHunt 只给评论数（reviews），不给评论原文 → review_sample 为空，分析如实说明。
"""
import logging
import os
import re
import threading
import time
from dataclasses import asdict
from .base import EtsyProvider
from ..models import EtsyProduct
from ..http import request_json, HttpError
from ..errors import PermanentError

log = logging.getLogger("ehunt")
URL = "https://api.ehunt.ai/api/v1/items"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 etsy-analyzer"
METRICS = ("sales_weekly", "sales_total", "reviews", "reviews_weekly", "favorites", "favorites_weekly",
           "is_bestsell", "is_pick", "is_raving", "status", "ships_from", "release_time", "category")


class EHuntQuotaError(Exception):
    """429：今日额度用尽，不可重试。breaker=True 表示应全局熔断到次日。"""
    def __init__(self, msg, breaker=False):
        super().__init__(msg)
        self.breaker = breaker


class EHuntRateLimited(Exception):
    """EHunt 429 但并非“今日额度用完”（每秒限速/瞬时错误）：重试若干次仍失败 → 任务回队列稍后再试，不熔断、不计尝试次数。"""
    rate_limited = True


NOT_FOUND = "商品不存在或已下架"
DAILY_LIMIT = 200

# 全进程（跨 worker 线程）EHunt 限速器：串行化请求，相邻两次至少间隔 MIN_INTERVAL 秒（约 1-2 次/秒）
MIN_INTERVAL = float(os.environ.get("EHUNT_MIN_INTERVAL_SECONDS", "0.7"))
BACKOFF = (2, 5, 10)  # 非额度类 429 的重试退避（秒）
_LOCK = threading.Lock()
_LAST = [0.0]
_sleep = time.sleep

_DAILY_RE = re.compile(r"daily|per day|today|今日|今天|每日|当日|额度(已)?(用完|用尽|耗尽|不足)|quota[^.]{0,30}(exhaust|exceed|used up|reached)|"
                       r"insufficient|credits? (exhausted|used up)|余额不足", re.I)
_RATE_RE = re.compile(r"per second|per minute|/s\b|too many requests|rate.?limit|频繁|太快|每秒|稍后|qps|slow down", re.I)


def is_daily_exhausted(message, used=None, remaining=None):
    """只有响应明确说“今日额度用完”或已用 ≥ 每日上限时才当作日额度耗尽；限速/未知 429 一律不算。"""
    if remaining == 0 or (used is not None and used >= DAILY_LIMIT):
        return True
    m = str(message or "")
    return bool(_DAILY_RE.search(m)) and not _RATE_RE.search(m)


def _scrub(s, key):
    s = str(s or "")
    return s.replace(key, "***") if key else s


def _num(v):
    try:
        f = float(str(v).replace(",", "").replace("$", ""))
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def upgrade_image(u):
    return re.sub(r"/il_\d+x(\d+|N)\.", "/il_570xN.", u or "")


class EHuntEtsy(EtsyProvider):
    name = "ehunt"
    no_review_text = True

    def __init__(self, api_key, cache=None, ttl=86400):
        if not api_key:
            raise ValueError("EHUNT_API_KEY 未配置")
        self.key, self.cache, self.ttl = api_key, cache, ttl
        self.calls, self.cache_hits, self.used_today, self.remaining = 0, 0, None, None
        self._totals = {}

    def get_listing(self, listing_id, slug=None):
        ck = f"ehunt:listing:{listing_id}"
        hit = self.cache.get(ck) if self.cache else None
        if hit is not None:
            self.cache_hits += 1
            log.info("EHunt listing %s: cache hit (0 credit)", listing_id)
            if hit.get("not_found"):
                raise PermanentError(f"{NOT_FOUND}（EHunt 未返回 listing {listing_id}，24 小时内缓存结果）")
            p = EtsyProduct(**hit)
            p.raw = {**(p.raw or {}), "cache": True}
            self._totals[listing_id] = ((p.raw or {}).get("ehunt") or {}).get("reviews")
            return p
        p = self._fetch(listing_id, slug)
        if self.cache:
            self.cache.put(ck, "ehunt", asdict(p), self.ttl)
        return p

    def _request(self, url):
        with _LOCK:  # 跨线程串行 + 最小间隔
            wait = _LAST[0] + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                _sleep(wait)
            try:
                return request_json("POST", URL, {"X-VIP-TOKEN": self.key, "User-Agent": UA, "Accept": "application/json"},
                                    body={"search_key": url, "page_size": 1}, timeout=60)
            finally:
                _LAST[0] = time.monotonic()

    def _fetch(self, listing_id, slug=None):
        url = f"https://www.etsy.com/listing/{listing_id}" + (f"/{slug}" if slug else "")
        for attempt in range(len(BACKOFF) + 1):
            try:
                d = self._request(url)
            except HttpError as e:
                msg = _scrub(e, self.key)
                if "HTTP 401" in msg or "HTTP 403" in msg:
                    raise EHuntQuotaError(f"EHunt 拒绝访问（401/403：检查 EHUNT_API_KEY，或 Cloudflare 拦截）：{msg[:200]}") from None
                if "HTTP 429" not in msg:
                    raise HttpError(msg) from None
                d, detail = None, msg[:300]
            else:
                if (d or {}).get("code") != 429:
                    break
                detail = _scrub(f"code=429 message={d.get('message')!r} quota={d.get('quota')!r}", self.key)[:300]
            q = (d or {}).get("quota") or {}
            used, rem = q.get("used_today"), q.get("remaining_today")
            if used is None:
                used = self.used_today  # 本线程上一次真实看到的用量
            if is_daily_exhausted(detail, used, rem):
                log.warning("EHunt listing %s: daily quota exhausted (%s)", listing_id, detail)
                raise EHuntQuotaError(f"EHunt 今日额度已用完，明日自动继续：{detail[:200]}", breaker=True)
            if attempt < len(BACKOFF):
                log.warning("EHunt listing %s: 429 not daily-quota (rate limit?), retry %d/%d in %ss: %s",
                            listing_id, attempt + 1, len(BACKOFF), BACKOFF[attempt], detail)
                _sleep(BACKOFF[attempt])
        else:
            raise EHuntRateLimited(f"EHunt 限速（429，非额度用完），稍后自动重试：{detail[:200]}")
        self.calls += 1
        q = (d or {}).get("quota") or {}
        self.used_today, self.remaining = q.get("used_today"), q.get("remaining_today")
        log.info("EHunt listing %s: used_today=%s remaining_today=%s", listing_id, self.used_today, self.remaining)
        if (d or {}).get("code") != 200:
            raise HttpError(f"EHunt 错误 {d.get('code')}: {d.get('message')}")
        items = ((d.get("data") or {}).get("list")) or []
        # 只接受 product_url 中 listing id 完全一致的条目；绝不回退到搜索第一条（会分析成别的商品）
        pat = re.compile(rf"/listing/{int(listing_id)}(?!\d)")
        it = next((x for x in items if pat.search(str(x.get("product_url", "")))), None)
        if not it:
            if self.cache:
                self.cache.put(f"ehunt:listing:{listing_id}", "ehunt", {"not_found": True}, self.ttl)
            raise PermanentError(f"{NOT_FOUND}（EHunt 未返回 listing {listing_id}）")
        tags = [t.strip() for t in str(it.get("tags") or "").split(",") if t.strip()]
        metrics = {k: it.get(k) for k in METRICS}
        for k in ("sales_weekly", "sales_total", "reviews", "reviews_weekly", "favorites", "favorites_weekly"):
            metrics[k] = _num(metrics[k])
        self._totals[listing_id] = metrics["reviews"]
        return EtsyProduct(
            listing_id=listing_id, title=it.get("title") or "", description="",
            price=_num(it.get("price")), currency="USD", tags=tags,
            shop_name=it.get("store_name") or "", shop_id=None,
            image_url=upgrade_image(it.get("logo_url")), num_favorers=metrics["favorites"],
            raw={"provider": "ehunt", "ehunt": metrics, "product_url": it.get("product_url"),
                 "quota": {"used_today": self.used_today, "remaining_today": self.remaining}})

    def fetch_listing_reviews(self, listing_id, limit, offset):
        return [], self._totals.get(listing_id)
