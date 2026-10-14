from abc import ABC, abstractmethod
from ..models import EtsyProduct, ProductAnalysis, AmazonItem, SearchPage, ReviewSet

STOP = {"a", "an", "the", "and", "or", "for", "with", "of", "to", "in", "on", "set", "#0", "#1", "#2", "#3", "#4"}


class EtsyProvider(ABC):
    @abstractmethod
    def get_listing(self, listing_id: int) -> EtsyProduct: ...

    PAGE = 100  # Etsy v3 reviews 接口 limit 上限

    def fetch_listing_reviews(self, listing_id, limit, offset):
        """返回 (list[Review], total)。子类实现。"""
        return [], 0

    def fetch_shop_reviews(self, shop_id, limit, offset):
        return [], 0

    def get_reviews(self, product: EtsyProduct, min_reviews: int = 50, supplement_shop: bool = False) -> ReviewSet:
        """规则：至少 min_reviews 条本商品评论；不足则全部使用。
        本商品评论始终为主；supplement_shop=True 时才用同店其它评论补足，并单独标注 source=shop。"""
        got, total, offset = [], None, 0
        while True:
            page, total = self.fetch_listing_reviews(product.listing_id, self.PAGE, offset)
            got += page
            offset += self.PAGE
            if not page or len(got) >= min_reviews or (total is not None and offset >= total):
                break
        for r in got:
            r.source = "listing"
        rs = ReviewSet(got, listing_total=total if total is not None else len(got), min_reviews=min_reviews,
                       shop_supplement_enabled=supplement_shop)
        if supplement_shop and len(got) < min_reviews and product.shop_id:
            need, offset, shop = min_reviews - len(got), 0, []
            seen = {r.text for r in got}
            while len(shop) < need:
                page, stotal = self.fetch_shop_reviews(product.shop_id, self.PAGE, offset)
                rs.shop_total = stotal
                shop += [r for r in page if r.text not in seen]
                offset += self.PAGE
                if not page or (stotal is not None and offset >= stotal):
                    break
            for r in shop[:need]:
                r.source = "shop"
                rs.append(r)
        return rs


class AmazonProvider(ABC):
    name = "base"

    @abstractmethod
    def search(self, keyword: str, limit: int = 10) -> SearchPage: ...


class ScreenshotProvider(ABC):
    """给证据生成图片。返回 (bytes, content_type, image_source) 或 None。"""
    @abstractmethod
    def capture(self, kind: str, url: str, title: str = "", image_url: str = "", meta: dict | None = None): ...


class LLMProvider(ABC):
    @abstractmethod
    def analyze_product(self, p: EtsyProduct, reviews: list) -> ProductAnalysis: ...

    def rank_similarity(self, p: EtsyProduct, items: list[AmazonItem]) -> list[AmazonItem]:
        """默认：标题+tags 词集合 Jaccard；真实 LLM/图像相似度可覆盖。"""
        def words(s):
            return {w.strip(",.()|-").lower() for w in s.split()} - STOP - {""}
        base = words(p.title + " " + " ".join(p.tags))
        for it in items:
            w = words(it.title)
            common = base & w
            it.similarity = round(len(common) / max(1, len(w)), 3)
            it.similarity_reason = (f"标题共有词：{', '.join(sorted(common)[:8])}（{len(common)}/{len(w)}）"
                                    if common else "标题无共同关键词")
        return sorted(items, key=lambda i: i.similarity or 0, reverse=True)
