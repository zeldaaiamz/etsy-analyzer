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
