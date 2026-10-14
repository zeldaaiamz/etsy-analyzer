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
    image_url: str = ""
    num_favorers: Optional[int] = None
    raw: dict = field(default_factory=dict)


@dataclass
class ProductAnalysis:
    product_summary: str
    category: str
    target_audience: str
    keywords: list


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
    raw: dict = field(default_factory=dict)


@dataclass
class Assessment:
    competition_level: str  # low/medium/high
    demand_level: str
    opportunity_score: int  # 0-100
    verdict: str


def to_dict(o):
    return asdict(o)
