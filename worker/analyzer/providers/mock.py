"""Mock 实现：确定性假数据，不访问网络。"""
import hashlib
from .base import EtsyProvider, AmazonProvider, LLMProvider
from ..models import EtsyProduct, ProductAnalysis, AmazonItem
from ..scoring import estimate_monthly_sales_from_bsr


def _h(s, mod):
    return int(hashlib.md5(str(s).encode()).hexdigest(), 16) % mod


class MockEtsy(EtsyProvider):
    def get_listing(self, listing_id):
        return EtsyProduct(listing_id=listing_id,
                           title=f"Personalized Minimalist Line Art Print {listing_id % 1000}",
                           description="Hand-drawn line art, printable wall decor.",
                           price=12.5, tags=["line art", "wall decor", "minimalist", "gift for her"],
                           materials=["paper"], shop_name="MockShop",
                           image_url="https://placehold.co/200", num_favorers=_h(listing_id, 5000))


class MockAmazon(AmazonProvider):
    name = "mock"

    def search(self, keyword, limit=10):
        out = []
        for i in range(min(limit, 5)):
            seed = f"{keyword}-{i}"
            bsr = 500 + _h(seed, 200000)
            out.append(AmazonItem(asin=f"B0MOCK{_h(seed, 10**4):04d}",
                                  title=f"{keyword.title()} Wall Art Print Set #{i}", keyword=keyword,
                                  price=10 + _h(seed, 30), rating=round(3.8 + _h(seed, 12) / 10, 1),
                                  review_count=_h(seed + "r", 5000), bsr=bsr, bsr_category="Home & Kitchen",
                                  est_monthly_sales=estimate_monthly_sales_from_bsr(bsr),
                                  url=f"https://www.amazon.com/dp/B0MOCK{_h(seed, 10**4):04d}",
                                  provider="mock"))
        return out


class MockLLM(LLMProvider):
    def analyze_product(self, p):
        return ProductAnalysis(product_summary=f"[mock] {p.title}",
                               category="Home Decor > Wall Art",
                               target_audience="25-45 岁女性，家居装饰/送礼需求",
                               keywords=(p.tags or [p.title])[:3])
