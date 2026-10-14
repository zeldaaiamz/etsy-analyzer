"""Keepamore HTTP API（第三方，封装 Keepa 数据 + ABA 关键词数据）。US 站。
认证：请求头 X-API-Key（km_ 开头）。响应 {code:'0000', msg, data, metadata{unitsConsumed, quotaHint}}。
- GET /api/productsearch?country=us&term=…&stats=90  关键词搜索（亚马逊自然排序，排除广告），固定 5 units/次
  搜索结果不含评分/评论数 → 对前 N 个 ASIN 再调
- GET /api/product?country=us&asin=a,b&stats=90&rating=1&history=false  1 unit/唯一 ASIN（≤100/批）
- GET /api/quota  0 units
产品对象为 Keepa 格式，复用 keepa.parse_product（monthlySold=亚马逊“过去一月购买”）。
"""
import http.client
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
from .base import AmazonProvider
from .keepa import parse_product
from ..models import SearchPage
from ..http import request_json, HttpError

log = logging.getLogger("keepamore")
BASE = "https://mcp.keepamore.com/api"
RETRY_BACKOFF = (2, 5, 10)  # 瞬时网络错误/5xx 原地重试间隔（秒），共 1+3 次
MIN_INTERVAL = 3.0  # 20 次/分钟 → 每次请求间隔 ≥3s（重试也计入）
_rl_lock, _rl_last = threading.Lock(), [0.0]


def _throttle():
    """全局限速（进程内所有 worker 线程共享）：每次请求预约一个时间槽，间隔 ≥ MIN_INTERVAL（20 次/分钟）。
    只在锁内预约、锁外等待：等 Keepamore 的线程不会挡住其它线程的 Etsy/LLM/截图等调用。"""
    with _rl_lock:
        now = time.monotonic()
        slot = max(now, _rl_last[0] + MIN_INTERVAL)
        _rl_last[0] = slot
    if slot > now:
        time.sleep(slot - now)


def is_transient(e):
    """可安全重试的瞬时错误：SSL EOF、连接重置/中断、超时、HTTP 5xx。4xx（含 429 另行处理）不算。"""
    if isinstance(e, HttpError):
        m = re.search(r"HTTP (\d{3})", str(e))
        return bool(m and 500 <= int(m.group(1)) < 600)
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        return is_transient(e.reason) if isinstance(e.reason, BaseException) else True
    return isinstance(e, (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout, EOFError,
                          http.client.IncompleteRead, http.client.RemoteDisconnected))


class KeepamoreAmazon(AmazonProvider):
    name = "keepamore"

    def __init__(self, api_key, per_keyword=10, stats_days=90, max_wait=300, cache=None, ttl=86400, on_wait=None):
        if not api_key:
            raise ValueError("KEEPAMORE_API_KEY 未配置")
        self.key, self.per_keyword, self.stats_days, self.max_wait = api_key, per_keyword, stats_days, max_wait
        self.cache, self.ttl, self.on_wait = cache, ttl, on_wait
        self.units_used, self.calls, self.cache_hits, self.remaining, self.warning = 0, 0, 0, None, None

    def reset_usage(self):
        self.units_used, self.calls, self.cache_hits = 0, 0, 0

    def usage_text(self):
        t = f"Keepamore 本任务 {self.calls} 次调用、消耗 {self.units_used} units"
        if self.cache_hits:
            t += f"、缓存命中 {self.cache_hits} 次"
        if self.remaining is not None:
            t += f"，资源包剩余 {self.remaining}"
        return t

    def _get(self, path, **params):
        """只用于幂等 GET。失败的请求没有返回 metadata.unitsConsumed，本地不记账；重试成功才按响应计费。"""
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        waited, tries = 0, 0
        while True:
            try:
                _throttle()
                d = request_json("GET", url, {"X-API-Key": self.key}, timeout=180)
            except Exception as e:  # noqa: BLE001
                msg = str(e).replace(self.key, "***")
                if "HTTP 429" in msg and waited < self.max_wait:  # 限速 20 次/分钟
                    time.sleep(15)
                    waited += 15
                    if self.on_wait:
                        self.on_wait()  # 心跳，避免长时间限速等待被当成卡死
                    continue
                if is_transient(e) and tries < len(RETRY_BACKOFF):
                    delay = RETRY_BACKOFF[tries]
                    tries += 1
                    log.warning("Keepamore %s transient error (%s: %s), retry %d/%d in %ss", path,
                                type(e).__name__, msg[:200], tries, len(RETRY_BACKOFF), delay)
                    time.sleep(delay)
                    if self.on_wait:
                        self.on_wait()
                    continue
                if isinstance(e, HttpError):
                    raise HttpError(msg) from None
                raise
            if (d or {}).get("code") != "0000":
                raise HttpError(f"Keepamore {path} 错误 {d.get('code')}: {d.get('msg')}")
            m = d.get("metadata") or {}
            self.calls += 1
            self.units_used += m.get("unitsConsumed") or 0
            self.remaining = (m.get("quotaHint") or {}).get("packRemaining", self.remaining)
            if m.get("keyWarning") and m["keyWarning"] != self.warning:
                self.warning = m["keyWarning"]
                log.warning("Keepamore: %s", self.warning)
            return d.get("data") or {}

    def quota(self):
        return self._get("/quota")

    def search(self, keyword, limit=10):
        n = min(limit, self.per_keyword)
        sk = f"km:search:{self.stats_days}:{keyword.strip().lower()}"
        hit = self.cache.get(sk) if self.cache else None
        if hit is not None:
            self.cache_hits += 1
            all_asins, total = hit.get("asins") or [], hit.get("total") or 0
        else:
            d = self._get("/productsearch", country="us", term=keyword, stats=self.stats_days)
            found = d.get("products") or []
            all_asins, total = list(dict.fromkeys(p.get("asin") for p in found if p.get("asin"))), len(found)
            if self.cache:
                self.cache.put(sk, "keepamore", {"asins": all_asins, "total": total}, self.ttl)
        asins = all_asins[:n]
        prods = {}
        if asins and self.cache:
            got = self.cache.get_many([f"km:product:{self.stats_days}:{a}" for a in asins])
            prods = {k.rsplit(":", 1)[1]: v for k, v in got.items()}
            self.cache_hits += len(prods)
        missing = [a for a in asins if a not in prods]
        if missing:
            pd = self._get("/product", country="us", asin=",".join(missing), stats=self.stats_days, rating=1, history="false")
            for p in pd.get("products") or []:
                if p.get("asin"):
                    prods[p["asin"]] = p
                    if self.cache:
                        self.cache.put(f"km:product:{self.stats_days}:{p['asin']}", "keepamore", p, self.ttl)
        items = [parse_product(prods[a], keyword) for a in asins if a in prods]
        for it in items:
            it.provider = self.name
        return SearchPage(keyword=keyword, url=f"https://www.amazon.com/s?k={urllib.parse.quote_plus(keyword)}",
                          total_results=total, items=items)
