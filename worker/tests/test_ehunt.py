from unittest import mock
from analyzer.providers import ehunt
from analyzer.providers.ehunt import EHuntEtsy, EHuntQuotaError, upgrade_image
from analyzer.http import HttpError

RESP = {"code": 200, "message": "ok", "quota": {"used_today": 3, "remaining_today": 197}, "data": {"list": [{
    "title": "Boo Tag", "price": "13.67", "tags": "a, b ,c", "store_name": "S", "favorites": 312, "reviews": 1,
    "sales_weekly": 0, "sales_total": 5, "logo_url": "https://i.etsystatic.com/x/il_120xN.1_ab.jpg",
    "product_url": "https://www.etsy.com/listing/42/boo"}]}}


def test_map():
    with mock.patch.object(ehunt, "request_json", return_value=RESP):
        e = EHuntEtsy("k")
        p = e.get_listing(42)
    assert p.title == "Boo Tag" and p.price == 13.67 and p.tags == ["a", "b", "c"] and p.num_favorers == 312
    assert p.image_url.endswith("il_570xN.1_ab.jpg") and p.raw["ehunt"]["sales_total"] == 5
    assert e.remaining == 197 and e.fetch_listing_reviews(42, 100, 0) == ([], 1)


def test_429():
    with mock.patch.object(ehunt, "request_json", side_effect=HttpError("HTTP 429 POST x: k today quota exhausted")):
        try:
            EHuntEtsy("k").get_listing(42)
            assert False
        except EHuntQuotaError as ex:
            assert "429" in str(ex)


def test_upgrade():
    assert upgrade_image("a/il_75x75.9.jpg") == "a/il_570xN.9.jpg"
