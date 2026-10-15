"""Mock 实现：确定性假数据，不访问网络。结构与真实 provider 一致，便于前端演示。"""
import hashlib
import html
from ..amazon_keywords import build as build_keywords
from .base import EtsyProvider, AmazonProvider, LLMProvider, ScreenshotProvider
from ..models import EtsyProduct, ProductAnalysis, AmazonItem, Review, SearchPage
from ..scoring import estimate_monthly_sales_from_bsr
from ..audience import infer_audience
from urllib.parse import quote_plus


def _h(s, mod):
    return int(hashlib.md5(str(s).encode()).hexdigest(), 16) % mod


class MockEtsy(EtsyProvider):
    def get_listing(self, listing_id, slug=None):
        words = [w for w in (slug or "").split("-") if w] or "personalized minimalist line art print".split()
        title = " ".join(w.capitalize() for w in words)
        tags = list(dict.fromkeys([" ".join(words[i:i + 2]) for i in range(0, min(len(words), 6), 2)]))
        return EtsyProduct(listing_id=listing_id, title=f"{title} - Handmade", description=f"Handmade {title.lower()}.",
                           price=round(5 + _h(listing_id, 3000) / 100, 2), tags=tags, materials=["paper"],
                           shop_name="MockShop", shop_id=1000 + _h(listing_id, 9000),
                           image_url=f"https://placehold.co/600x600/png?text={quote_plus(title[:30])}",
                           num_favorers=_h(listing_id, 5000))

    TOTALS = [120, 23, 67, 8, 240, 51, 0]  # 不同商品评论总数差异很大

    def review_total(self, listing_id):
        return self.TOTALS[_h(listing_id, len(self.TOTALS))]

    def _gen(self, key, i):
        who = ["my daughter", "my sister", "my son's teacher", "my mom", "the kids", "my coworkers", "my wife",
               "my grandson", "myself", "our classroom", "my best friend", "the baby shower guests"]
        why = ["for a Halloween party", "as a birthday gift", "for Christmas", "to decorate the living room",
               "as a little thank-you present", "for a school event", "for our wedding favors", ""]
        how = ["Absolutely love it!", "Great quality and arrived fast.", "Cute and well made.",
               "The personalized name was perfect.", "Smaller than expected but still nice.",
               "Colors are even better in person.", "Would buy again.", "Shipping took a while."]
        k = f"{key}-{i}"
        rating = [5, 5, 5, 4, 5, 4, 3, 5, 2][_h(k + "r", 9)]
        txt = f"Bought this for {who[_h(k + 'w', len(who))]} {why[_h(k + 'y', len(why))]}. {how[_h(k + 'h', len(how))]}"
        return Review(text=" ".join(txt.split()), rating=rating, created_at=f"2026-{1 + _h(k, 9):02d}-{1 + _h(k + 'd', 27):02d}")

    def fetch_listing_reviews(self, listing_id, limit, offset):
        total = self.review_total(listing_id)
        return [self._gen(listing_id, i) for i in range(offset, min(total, offset + limit))], total

    def fetch_shop_reviews(self, shop_id, limit, offset):
        total = 300
        return [self._gen(f"shop{shop_id}", i) for i in range(offset, min(total, offset + limit))], total


class MockAmazon(AmazonProvider):
    name = "mock"

    def search(self, keyword, limit=10):
        titles = [f"{keyword.title()} Handmade Personalized", f"{keyword.title()} Gift Pack of 12",
                  f"{keyword.split()[0].title()} Decor Assorted Set", f"{keyword.split()[-1].title()} Party Supplies Bundle",
                  "Generic Kitchen Storage Container"]
        items = []
        for i, t in enumerate(titles[:limit]):
            seed = f"{keyword}-{i}"
            bsr = 500 + _h(seed, 200000)
            asin = f"B0MOCK{_h(seed, 10**4):04d}"
            items.append(AmazonItem(asin=asin, title=t, keyword=keyword, price=float(8 + _h(seed, 30)),
                                    rating=round(3.8 + _h(seed, 12) / 10, 1), review_count=_h(seed + "r", 5000),
                                    bsr=bsr, bsr_category="Home & Kitchen",
                                    est_monthly_sales=estimate_monthly_sales_from_bsr(bsr),
                                    url=f"https://www.amazon.com/dp/{asin}", provider="mock"))
        return SearchPage(keyword=keyword, url=f"https://www.amazon.com/s?k={quote_plus(keyword)}",
                          total_results=1000 + _h(keyword, 50000), items=items)


class MockLLM(LLMProvider):
    def analyze_product(self, p, reviews):
        audience, ev = infer_audience(p, reviews)
        kws = (p.tags or [p.title])[:3]
        return ProductAnalysis(product_summary=f"[mock] {p.title}", category="Home & Kitchen > Seasonal Decor",
                               target_audience=audience, keywords=kws,
                               keyword_reasoning="mock：直接取 Etsy tags 前 3 个作为亚马逊搜索词",
                               audience_evidence=ev, keyword_items=build_keywords([], p.title, fallback=kws))


COLORS = {"identical": "#c0392b", "similar": "#d68910", "search_result": "#2471a3"}
LABELS = {"identical": "同款", "similar": "类似品", "search_result": "关键词搜索页"}


def placeholder_svg(kind, title, meta):
    e = html.escape
    lines = [f"{k}: {v}" for k, v in (meta or {}).items() if v not in (None, "")][:8]
    body = "".join(f'<text x="24" y="{150 + 30 * i}" font-size="20" fill="#333">{e(str(l))[:70]}</text>'
                   for i, l in enumerate(lines))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450">'
            f'<rect width="800" height="450" fill="#fafafa" stroke="#ccc"/>'
            f'<rect width="800" height="56" fill="{COLORS[kind]}"/>'
            f'<text x="24" y="38" font-size="24" fill="#fff" font-family="sans-serif">MOCK 快照 · {LABELS[kind]}</text>'
            f'<text x="24" y="100" font-size="22" font-weight="bold" fill="#111">{e(title)[:60]}</text>{body}'
            f'<text x="24" y="430" font-size="14" fill="#999">占位图（mock 模式生成，非真实截图）</text></svg>').encode()


class MockScreenshot(ScreenshotProvider):
    def capture(self, kind, url, title="", image_url="", meta=None):
        return placeholder_svg(kind, title, {"URL": url, **(meta or {})}), "image/svg+xml", "mock_placeholder"
