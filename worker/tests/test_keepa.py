from analyzer.providers.keepa import parse_product, KeepaAmazon


def _p():
    cur = [-1] * 20
    cur[0], cur[3], cur[16], cur[17], cur[18] = 1299, 4521, 46, 812, 1199
    return {"asin": "B0TEST1234", "title": "Halloween Basket Tags", "monthlySold": 300,
            "images": [{"l": "71abc.jpg"}], "stats": {"current": cur},
            "categoryTree": [{"catId": 1, "name": "Home & Kitchen"}], "salesRankReference": 1}


def test_parse_product():
    it = parse_product(_p(), "halloween tag")
    assert (it.price, it.bsr, it.rating, it.review_count) == (11.99, 4521, 4.6, 812)
    assert it.est_monthly_sales == 300 and it.raw["sales_source"] == "monthlySold"
    assert it.image_url == "https://m.media-amazon.com/images/I/71abc.jpg" and it.bsr_category == "Home & Kitchen"


def test_parse_product_fallback_bsr_estimate():
    p = _p(); p.pop("monthlySold"); p.pop("images"); p["imagesCSV"] = "a.jpg,b.jpg"
    it = parse_product(p)
    assert it.raw["sales_source"] == "bsr_estimate" and it.est_monthly_sales and it.image_url.endswith("/a.jpg")


def test_search_batches_and_counts_tokens(monkeypatch):
    calls = []

    def fake(method, url, headers=None, params=None, body=None, timeout=60):
        calls.append(url)
        if "/search" in url:
            return {"asinList": [f"B0{i:08d}" for i in range(30)], "totalResults": 30, "tokensConsumed": 10, "tokensLeft": 90}
        return {"products": [dict(_p(), asin=a) for a in url.split("asin=")[1].split("&")[0].split("%2C")],
                "tokensConsumed": 5, "tokensLeft": 85, "refillRate": 5}
    monkeypatch.setattr("analyzer.providers.keepa.request_json", fake)
    k = KeepaAmazon("k", per_keyword=5)
    pg = k.search("halloween tag", limit=10)
    assert len(pg.items) == 5 and len(calls) == 2 and k.tokens_used == 15 and k.refill_rate == 5
