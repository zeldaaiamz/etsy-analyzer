"""EHunt Etsy 数据 API（第三方）。
POST https://api.ehunt.ai/api/v1/items  header X-VIP-TOKEN，body {"search_key": <listing url>, "page_size": 1}
响应 {code:200, message, data:{list:[...]}, quota:{used_today, remaining_today}}；1 credit/商品，免费 200/天。
注意：EHunt 只给评论数（reviews），不给评论原文 → review_sample 为空，分析如实说明。
"""
import logging
import re
from .base import EtsyProvider
from ..models import EtsyProduct
from ..http import request_json, HttpError

log = logging.getLogger("ehunt")
URL = "https://api.ehunt.ai/api/v1/items"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 etsy-analyzer"
METRICS = ("sales_weekly", "sales_total", "reviews", "reviews_weekly", "favorites", "favorites_weekly",
           "is_bestsell", "is_pick", "is_raving", "status", "ships_from", "release_time", "category")


class EHuntQuotaError(Exception):
    """429：今日额度用尽，不可重试。"""


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

    def __init__(self, api_key):
        if not api_key:
            raise ValueError("EHUNT_API_KEY 未配置")
        self.key = api_key
        self.calls, self.used_today, self.remaining = 0, None, None
        self._totals = {}

    def get_listing(self, listing_id, slug=None):
        url = f"https://www.etsy.com/listing/{listing_id}" + (f"/{slug}" if slug else "")
        try:
            d = request_json("POST", URL, {"X-VIP-TOKEN": self.key, "User-Agent": UA, "Accept": "application/json"}, body={"search_key": url, "page_size": 1}, timeout=60)
        except HttpError as e:
            msg = str(e).replace(self.key, "***")
            if "HTTP 429" in msg:
                raise EHuntQuotaError(f"EHunt 今日额度已用完或被限速（HTTP 429），请明天再试或更换 key：{msg[:200]}") from None
            if "HTTP 401" in msg or "HTTP 403" in msg:
                raise EHuntQuotaError(f"EHunt 拒绝访问（401/403：检查 EHUNT_API_KEY，或 Cloudflare 拦截）：{msg[:200]}") from None
            raise HttpError(msg) from None
        self.calls += 1
        q = (d or {}).get("quota") or {}
        self.used_today, self.remaining = q.get("used_today"), q.get("remaining_today")
        log.info("EHunt listing %s: used_today=%s remaining_today=%s", listing_id, self.used_today, self.remaining)
        if (d or {}).get("code") == 429:
            raise EHuntQuotaError(f"EHunt 今日额度已用完：{d.get('message')}")
        if (d or {}).get("code") != 200:
            raise HttpError(f"EHunt 错误 {d.get('code')}: {d.get('message')}")
        items = ((d.get("data") or {}).get("list")) or []
        it = next((x for x in items if str(listing_id) in str(x.get("product_url", ""))), items[0] if items else None)
        if not it:
            raise HttpError(f"EHunt 未找到 listing {listing_id}")
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
