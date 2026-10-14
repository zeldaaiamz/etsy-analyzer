"""Rainforest API（第三方合规数据商）——亚马逊 US 搜索结果。
文档: https://docs.trajectdata.com/rainforestapi  type=search
注：搜索结果通常无 BSR，需对前 N 个 ASIN 再调 type=product 获取 bestsellers_rank（额外计费）。
"""
from .base import AmazonProvider
from ..models import AmazonItem
from ..http import request_json
from ..scoring import estimate_monthly_sales_from_bsr


class RainforestAmazon(AmazonProvider):
    name = "rainforest"

    def __init__(self, api_key, fetch_bsr_top=3):
        if not api_key:
            raise ValueError("RAINFOREST_API_KEY 未配置")
        self.key, self.fetch_bsr_top = api_key, fetch_bsr_top

    def _get(self, **p):
        return request_json("GET", "https://api.rainforestapi.com/request",
                            params={"api_key": self.key, "amazon_domain": "amazon.com", **p})

    def search(self, keyword, limit=10):
        d = self._get(type="search", search_term=keyword)
        items = []
        for r in (d.get("search_results") or [])[:limit]:
            items.append(AmazonItem(asin=r.get("asin", ""), title=r.get("title", ""), keyword=keyword,
                                    price=(r.get("price") or {}).get("value"), rating=r.get("rating"),
                                    review_count=r.get("ratings_total"), url=r.get("link", ""),
                                    image_url=r.get("image", ""), provider=self.name, raw=r))
        for it in items[: self.fetch_bsr_top]:
            try:
                prod = self._get(type="product", asin=it.asin).get("product", {})
                ranks = prod.get("bestsellers_rank") or []
                if ranks:
                    it.bsr, it.bsr_category = ranks[0].get("rank"), ranks[0].get("category", "")
                    it.est_monthly_sales = estimate_monthly_sales_from_bsr(it.bsr, it.bsr_category)
            except Exception:  # noqa: BLE001 单个失败不影响整体
                pass
        return items
