from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class EtsyProduct:
    listing_id: int
    title: str
    description: str = ""
    price: Optional[float] = None
    currency: str = "USD"
    tags: list = field(default_factory=list)
    materials: list = field(default_factory=list)
    shop_name: str = ""
    shop_id: Optional[int] = None
    image_url: str = ""
    num_favorers: Optional[int] = None
    raw: dict = field(default_factory=dict)


@dataclass
class ProductAnalysis:
    product_summary: str
    category: str
    target_audience: str
    keywords: list
    keyword_reasoning: str = ""
    audience_evidence: dict = field(default_factory=dict)
    keyword_items: list = field(default_factory=list)  # PM-001：[{keyword, source, basis}]


@dataclass
class AmazonItem:
    asin: str
    title: str
    keyword: str = ""
    price: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    bsr: Optional[int] = None
    bsr_category: str = ""
    est_monthly_sales: Optional[int] = None
    similarity: Optional[float] = None
    url: str = ""
    image_url: str = ""
    provider: str = ""
    match_type: str = ""          # identical | similar
    similarity_reason: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Review:
    text: str
    rating: Optional[int] = None
    created_at: Optional[str] = None
    source: str = "listing"   # listing（本商品评论）| shop（同店其它商品评论，补充用）


class ReviewSet(list):
    """评论列表 + 来源统计。"""
    def __init__(self, items=(), listing_total=None, shop_total=None, min_reviews=50, shop_supplement_enabled=False):
        super().__init__(items)
        self.listing_total, self.shop_total = listing_total, shop_total
        self.min_reviews, self.shop_supplement_enabled = min_reviews, shop_supplement_enabled

    @property
    def listing_used(self):
        return sum(1 for r in self if r.source == "listing")

    @property
    def shop_used(self):
        return sum(1 for r in self if r.source == "shop")

    def stats(self):
        return {"min_reviews": self.min_reviews, "listing_reviews_total": self.listing_total,
                "listing_reviews_used": self.listing_used, "shop_reviews_used": self.shop_used,
                "shop_reviews_total": self.shop_total, "shop_supplement_enabled": self.shop_supplement_enabled}


@dataclass
class SearchPage:
    keyword: str
    url: str
    total_results: Optional[int] = None
    items: list = field(default_factory=list)


@dataclass
class Assessment:
    competition_level: str  # low/medium/high
    demand_level: str
    opportunity_score: int  # 0-100
    verdict: str
    reasoning: list = field(default_factory=list)


def to_dict(o):
    return asdict(o)
