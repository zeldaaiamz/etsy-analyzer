"""Keepa API（官方付费数据，domain=1 → amazon.com）。不访问亚马逊前台，无验证码问题。
流程：/search?type=product&asins-only=1 取关键词搜索 ASIN → /product 批量取详情(stats)。
- 价格：stats.current[0]=Amazon 自营价，[1]=新品第三方最低价，[18]=Buy Box（单位：美分，-1=无）
- BSR：stats.current[3]；评分：current[16]/10；评论数：current[17]
- monthlySold：亚马逊页面“过去一个月购买 X+”，属于真实销量信号（优先于 BSR 估算）
- 图片：images[].l 或 imagesCSV → https://m.media-amazon.com/images/I/<id>
Token：按响应中的 tokensLeft/refillIn 管理；429 时按 refillIn 等待重试。
"""
import logging
import time
import urllib.parse
from .base import AmazonProvider
from ..models import AmazonItem, SearchPage
from ..http import request_json, HttpError
from ..scoring import estimate_monthly_sales_from_bsr

log = logging.getLogger("keepa")
BASE = "https://api.keepa.com"
IMG = "https://m.media-amazon.com/images/I/"


def _cur(stats, i):
    try:
        v = (stats or {}).get("current", [])[i]
        return None if v is None or v < 0 else v
    except (IndexError, TypeError):
        return None


def parse_product(p, keyword=""):
    st = p.get("stats") or {}
    price = next((v for v in (_cur(st, 18), _cur(st, 0), _cur(st, 1)) if v is not None), None)
    imgs = [i.get("l") or i.get("m") for i in (p.get("images") or []) if i]
    if not imgs and p.get("imagesCSV"):
        imgs = p["imagesCSV"].split(",")
    bsr = _cur(st, 3)
    cat = ""
    if p.get("salesRankReference") and p.get("categoryTree"):
        cat = next((c.get("name", "") for c in p["categoryTree"] if c.get("catId") == p["salesRankReference"]), "")
    elif p.get("categoryTree"):
        cat = p["categoryTree"][0].get("name", "")
    rating = _cur(st, 16)
    monthly = p.get("monthlySold")
    asin = p.get("asin", "")
    return AmazonItem(
        asin=asin, title=p.get("title") or "", keyword=keyword,
        price=round(price / 100, 2) if price is not None else None,
        rating=round(rating / 10, 1) if rating is not None else None,
        review_count=_cur(st, 17), bsr=bsr, bsr_category=cat,
        est_monthly_sales=monthly if monthly and monthly > 0 else estimate_monthly_sales_from_bsr(bsr),
        url=f"https://www.amazon.com/dp/{asin}", image_url=(IMG + imgs[0]) if imgs and imgs[0] else "",
        provider="keepa",
        raw={"monthlySold": monthly, "sales_source": "monthlySold" if monthly and monthly > 0 else "bsr_estimate",
             "brand": p.get("brand"), "images": imgs[:5]})


class KeepaAmazon(AmazonProvider):
    name = "keepa"

    def __init__(self, api_key, per_keyword=10, stats_days=90, max_wait=600):
        if not api_key:
            raise ValueError("KEEPA_API_KEY 未配置")
        self.key, self.per_keyword, self.stats_days, self.max_wait = api_key, per_keyword, stats_days, max_wait
        self.tokens_used = 0
        self.tokens_left = None
        self.refill_rate = None

    def reset_usage(self):
        self.tokens_used = 0

    def _get(self, path, domain=True, **params):
        q = urllib.parse.urlencode({"key": self.key, **({"domain": 1} if domain else {}), **params})
        waited = 0
        while True:
            try:
                d = request_json("GET", f"{BASE}{path}?{q}", {"Accept-Encoding": "identity"}, timeout=90)
            except HttpError as e:
                msg = str(e).replace(self.key, "***")
                if "HTTP 429" in msg and waited < self.max_wait:  # token 用完：等待补充
                    wait = 60
                    log.warning("keepa 429, wait %ss", wait)
                    time.sleep(wait)
                    waited += wait
                    continue
                raise HttpError(msg) from None
            self.tokens_used += d.get("tokensConsumed", 0) or 0
            self.tokens_left, self.refill_rate = d.get("tokensLeft"), d.get("refillRate")
            return d

    def token_status(self):
        d = self._get("/token", domain=False)
        return {"tokensLeft": d.get("tokensLeft"), "refillRate": d.get("refillRate"), "refillIn": d.get("refillIn")}

    def search(self, keyword, limit=10):
        n = min(limit, self.per_keyword)
        d = self._get("/search", type="product", term=keyword, **{"asins-only": 1})
        asins = (d.get("asinList") or [])[:n]
        items = []
        for i in range(0, len(asins), 100):  # /product 最多 100 个 ASIN 一批
            pd = self._get("/product", asin=",".join(asins[i:i + 100]), stats=self.stats_days)
            items += [parse_product(p, keyword) for p in pd.get("products") or []]
        return SearchPage(keyword=keyword, url=f"https://www.amazon.com/s?k={urllib.parse.quote_plus(keyword)}",
                          total_results=d.get("totalResults") or len(d.get("asinList") or []), items=items)
