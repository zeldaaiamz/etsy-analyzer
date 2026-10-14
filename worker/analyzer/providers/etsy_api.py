"""Etsy Open API v3 —— 官方接口，不爬网页。
GET https://openapi.etsy.com/v3/application/listings/{listing_id}  header x-api-key
"""
from .base import EtsyProvider
from ..models import EtsyProduct, Review
from ..http import request_json

BASE = "https://openapi.etsy.com/v3/application"


class EtsyOpenAPI(EtsyProvider):
    def __init__(self, api_key):
        if not api_key:
            raise ValueError("ETSY_API_KEY 未配置")
        self.h = {"x-api-key": api_key}

    def get_listing(self, listing_id, slug=None):
        d = request_json("GET", f"{BASE}/listings/{listing_id}", self.h,
                         params={"includes": "Images,Shop"})
        price = d.get("price") or {}
        imgs = d.get("images") or []
        return EtsyProduct(
            listing_id=listing_id, title=d.get("title", ""), description=d.get("description", ""),
            price=(price.get("amount") / price.get("divisor")) if price.get("divisor") else None,
            currency=price.get("currency_code", "USD"), tags=d.get("tags") or [],
            materials=d.get("materials") or [], shop_name=(d.get("shop") or {}).get("shop_name", ""), shop_id=d.get("shop_id"),
            image_url=imgs[0].get("url_570xN", "") if imgs else "",
            num_favorers=d.get("num_favorers"), raw=d)

    def get_reviews(self, product, limit=50):
        """getReviewsByListing；若该 listing 无评论则退回 getReviewsByShop（店铺整体评论，作为弱信号）。"""
        def fetch(path):
            d = request_json("GET", f"{BASE}{path}", self.h, params={"limit": min(limit, 100)})
            return [Review(text=r.get("review") or "", rating=r.get("rating"),
                           created_at=str(r.get("create_timestamp", ""))) for r in d.get("results", [])
                    if r.get("review")]
        revs = fetch(f"/listings/{product.listing_id}/reviews")
        if not revs and product.shop_id:
            revs = fetch(f"/shops/{product.shop_id}/reviews")
        return revs
