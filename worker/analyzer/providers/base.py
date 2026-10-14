from abc import ABC, abstractmethod
from ..models import EtsyProduct, ProductAnalysis, AmazonItem


class EtsyProvider(ABC):
    @abstractmethod
    def get_listing(self, listing_id: int) -> EtsyProduct: ...


class AmazonProvider(ABC):
    name = "base"

    @abstractmethod
    def search(self, keyword: str, limit: int = 10) -> list[AmazonItem]: ...


class LLMProvider(ABC):
    @abstractmethod
    def analyze_product(self, p: EtsyProduct) -> ProductAnalysis: ...

    def rank_similarity(self, p: EtsyProduct, items: list[AmazonItem]) -> list[AmazonItem]:
        """默认：基于标题词重叠的相似度；真实 LLM 可覆盖。"""
        base = set(p.title.lower().split())
        for it in items:
            words = set(it.title.lower().split())
            it.similarity = round(len(base & words) / max(1, len(base | words)), 3)
        return sorted(items, key=lambda i: i.similarity or 0, reverse=True)
