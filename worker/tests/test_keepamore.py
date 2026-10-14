from analyzer.providers.keepamore import KeepamoreAmazon


def _prod(a, rating=True):
    cur = [-1] * 20
    cur[0], cur[3] = 899, 40618
    if rating:
        cur[16], cur[17] = 46, 169
    return {"asin": a, "title": f"Halloween Tag {a}", "monthlySold": 300, "images": [{"l": "819s.jpg"}], "stats": {"current": cur}}


def test_search_then_product(monkeypatch):
    calls = []

    def fake(method, url, headers=None, params=None, body=None, timeout=60):
        assert headers["X-API-Key"] == "km_test" and timeout >= 180
        calls.append(url)
        if "/productsearch" in url:
            return {"code": "0000", "data": {"products": [_prod(f"B0{i:08d}", False) for i in range(20)]},
                    "metadata": {"unitsConsumed": 5, "quotaHint": {"packRemaining": 995}}}
        n = url.split("asin=")[1].split("&")[0].count("%2C") + 1
        return {"code": "0000", "data": {"products": [_prod(f"B0{i:08d}") for i in range(n)]},
                "metadata": {"unitsConsumed": n, "quotaHint": {"packRemaining": 995 - n}}}
    monkeypatch.setattr("analyzer.providers.keepamore.request_json", fake)
    k = KeepamoreAmazon("km_test", per_keyword=8)
    pg = k.search("halloween tag", limit=10)
    assert len(calls) == 2 and len(pg.items) == 8 and pg.total_results == 20
    it = pg.items[0]
    assert (it.rating, it.review_count, it.est_monthly_sales, it.provider) == (4.6, 169, 300, "keepamore")
    assert it.image_url.endswith("/819s.jpg")
    assert k.units_used == 13 and k.remaining == 987 and "13 units" in k.usage_text()


def test_error_code_raises(monkeypatch):
    monkeypatch.setattr("analyzer.providers.keepamore.request_json",
                        lambda *a, **kw: {"code": "4001", "msg": "bad key", "data": None})
    import pytest
    with pytest.raises(Exception, match="4001"):
        KeepamoreAmazon("km_x").search("x")
