from abc import ABC, abstractmethod
from ..models import EtsyProduct, ProductAnalysis, AmazonItem, SearchPage

STOP = {"a", "an", "the", "and", "or", "for", "with", "of", "to", "in", "on", "set", "#0", "#1", "#2", "#3", "#4"}


class EtsyProvider(ABC):
    @abstractmethod
    def get_listing(self, listing_id: int) -> EtsyProduct: ...

    def get_reviews(self, product: EtsyProduct, limit: int = 50) -> list:
        """返回 list[Review]；默认无评论。"""
        return []


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
