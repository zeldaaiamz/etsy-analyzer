"""Keepamore HTTP API（第三方，封装 Keepa 数据 + ABA 关键词数据）。US 站。
认证：请求头 X-API-Key（km_ 开头）。响应 {code:'0000', msg, data, metadata{unitsConsumed, quotaHint}}。
- GET /api/productsearch?country=us&term=…&stats=90  关键词搜索（亚马逊自然排序，排除广告），固定 5 units/次
  搜索结果不含评分/评论数 → 对前 N 个 ASIN 再调
- GET /api/product?country=us&asin=a,b&stats=90&rating=1&history=false  1 unit/唯一 ASIN（≤100/批）
- GET /api/quota  0 units
产品对象为 Keepa 格式，复用 keepa.parse_product（monthlySold=亚马逊“过去一月购买”）。
"""
import logging
import time
import urllib.parse
from .base import AmazonProvider
from .keepa import parse_product
from ..models import SearchPage
from ..http import request_json, HttpError

log = logging.getLogger("keepamore")
BASE = "https://mcp.keepamore.com/api"


class KeepamoreAmazon(AmazonProvider):
    name = "keepamore"

    def __init__(self, api_key, per_keyword=10, stats_days=90, max_wait=300):
        if not api_key:
            raise ValueError("KEEPAMORE_API_KEY 未配置")
        self.key, self.per_keyword, self.stats_days, self.max_wait = api_key, per_keyword, stats_days, max_wait
        self.units_used, self.calls, self.remaining, self.warning = 0, 0, None, None

    def reset_usage(self):
        self.units_used, self.calls = 0, 0

    def usage_text(self):
        t = f"Keepamore 本任务 {self.calls} 次调用、消耗 {self.units_used} units"
        if self.remaining is not None:
            t += f"，资源包剩余 {self.remaining}"
        return t

    def _get(self, path, **params):
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        waited = 0
        while True:
            try:
                d = request_json("GET", url, {"X-API-Key": self.key}, timeout=180)
            except HttpError as e:
                msg = str(e).replace(self.key, "***")
                if "HTTP 429" in msg and waited < self.max_wait:  # 限速 20 次/分钟
                    time.sleep(15)
                    waited += 15
                    continue
                raise HttpError(msg) from None
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
        d = self._get("/productsearch", country="us", term=keyword, stats=self.stats_days)
        found = d.get("products") or []
        asins = list(dict.fromkeys(p.get("asin") for p in found if p.get("asin")))[:n]
        items = []
        if asins:
            pd = self._get("/product", country="us", asin=",".join(asins), stats=self.stats_days, rating=1, history="false")
            items = [parse_product(p, keyword) for p in pd.get("products") or []]
            for it in items:
                it.provider = self.name
        return SearchPage(keyword=keyword, url=f"https://www.amazon.com/s?k={urllib.parse.quote_plus(keyword)}",
                          total_results=len(found), items=items)
