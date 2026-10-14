"""Etsy Open API v3 —— 官方接口，不爬网页。
GET https://openapi.etsy.com/v3/application/listings/{listing_id}  header x-api-key
"""
from .base import EtsyProvider
from ..models import EtsyProduct
from ..http import request_json

BASE = "https://openapi.etsy.com/v3/application"


class EtsyOpenAPI(EtsyProvider):
    def __init__(self, api_key):
        if not api_key:
            raise ValueError("ETSY_API_KEY 未配置")
        self.h = {"x-api-key": api_key}

    def get_listing(self, listing_id):
        d = request_json("GET", f"{BASE}/listings/{listing_id}", self.h,
                         params={"includes": "Images,Shop"})
        price = d.get("price") or {}
        imgs = d.get("images") or []
        return EtsyProduct(
            listing_id=listing_id, title=d.get("title", ""), description=d.get("description", ""),
            price=(price.get("amount") / price.get("divisor")) if price.get("divisor") else None,
            currency=price.get("currency_code", "USD"), tags=d.get("tags") or [],
            materials=d.get("materials") or [], shop_name=(d.get("shop") or {}).get("shop_name", ""),
            image_url=imgs[0].get("url_570xN", "") if imgs else "",
            num_favorers=d.get("num_favorers"), raw=d)
