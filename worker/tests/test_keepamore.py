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


def _ok(units=0):
    return {"code": "0000", "data": {"x": 1}, "metadata": {"unitsConsumed": units}}


def test_transient_retry_in_place(monkeypatch):
    import ssl
    import urllib.error
    from analyzer.http import HttpError
    sleeps = []
    monkeypatch.setattr("analyzer.providers.keepamore.time.sleep", lambda s: sleeps.append(s))
    errs = [ssl.SSLError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"),
            urllib.error.URLError(ConnectionResetError(104, "reset")),
            HttpError("HTTP 502 GET https://mcp.keepamore.com/api/quota: bad gateway")]
    def fake(*a, **kw):
        if errs:
            raise errs.pop(0)
        return _ok(5)
    monkeypatch.setattr("analyzer.providers.keepamore.request_json", fake)
    k = KeepamoreAmazon("km_t")
    assert k._get("/quota") == {"x": 1}
    assert [s for s in sleeps if s in (2, 5, 10)] == [2, 5, 10]
    assert k.calls == 1 and k.units_used == 5  # 失败请求不计费，只按成功响应记账


def test_transient_gives_up_after_3_retries(monkeypatch):
    import pytest
    n = []
    monkeypatch.setattr("analyzer.providers.keepamore.time.sleep", lambda s: None)
    def fake(*a, **kw):
        n.append(1)
        raise TimeoutError("timed out")
    monkeypatch.setattr("analyzer.providers.keepamore.request_json", fake)
    with pytest.raises(TimeoutError):
        KeepamoreAmazon("km_t")._get("/quota")
    assert len(n) == 4


def test_4xx_not_retried(monkeypatch):
    import pytest
    from analyzer.http import HttpError
    n = []
    monkeypatch.setattr("analyzer.providers.keepamore.time.sleep", lambda s: None)
    def fake(*a, **kw):
        n.append(1)
        raise HttpError("HTTP 401 GET x: km_secret bad")
    monkeypatch.setattr("analyzer.providers.keepamore.request_json", fake)
    with pytest.raises(HttpError) as ei:
        KeepamoreAmazon("km_secret")._get("/quota")
    assert len(n) == 1 and "km_secret" not in str(ei.value)


def test_throttle_spacing(monkeypatch):
    import analyzer.providers.keepamore as km
    sleeps, clock = [], [100.0]
    monkeypatch.setattr(km.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(km.time, "sleep", lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    km._rl_last[0] = 0.0
    km._throttle(); km._throttle()
    assert sleeps == [km.MIN_INTERVAL]


def test_ehunt_cached_before_keepamore_failure():
    """EHunt 成功后立即写 provider_cache；Keepamore 失败导致任务重试时 EHunt 0 credit。"""
    from analyzer.providers.ehunt import EHuntEtsy
    from analyzer.models import EtsyProduct
    class C(dict):
        def get(self, k): return dict.get(self, k)
        def put(self, k, prov, v, ttl): self[k] = v
    c, fetched = C(), []
    e = EHuntEtsy("k", cache=c)
    e._fetch = lambda lid, slug=None: (fetched.append(lid), EtsyProduct(listing_id=lid, url="u", title="t"))[1]
    e.get_listing(1)
    assert "ehunt:listing:1" in c  # 写缓存发生在 get_listing 返回前，即 Keepamore 步骤之前
    e.get_listing(1)
    assert fetched == [1] and e.cache_hits == 1
