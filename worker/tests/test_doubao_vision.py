"""PM-001：豆包视觉图片关键词。"""
from analyzer import amazon_keywords as akw
from analyzer.models import EtsyProduct
from analyzer.providers import doubao_vision as dv
from analyzer.providers.llm_openai import OpenAICompatLLM, main_image_url


def test_parse_tolerates_fences_and_rejects_garbage():
    r = dv.parse('```json\n{"item":"ceramic mug","scene":"coffee break","keywords":["coffee mug gift","a","b","c"]}\n```')
    assert r["keywords"] == ["coffee mug gift", "a"] and r["description"] == "ceramic mug; coffee break"
    assert dv.parse("no json") is None and dv.parse("[1,2]") is None and dv.parse('{"x":1}') is None and dv.parse(None) is None


def test_from_env_flag():
    assert dv.from_env({"ARK_API_KEY": "k"}) is None
    assert dv.from_env({"DOUBAO_VISION_ENABLED": "1"}) is None
    v = dv.from_env({"DOUBAO_VISION_ENABLED": "true", "ARK_API_KEY": "k"})
    assert v and v.model == dv.DEFAULT_MODEL


def test_describe_failure_returns_none(monkeypatch):
    v = dv.DoubaoVision("k")
    def boom(body):
        raise RuntimeError("HTTP 404 ModelNotOpen")
    v._post = boom
    assert v.describe("https://x/y.jpg") is None and v.failures == 1
    assert v.describe("") is None  # 无图不调用


def test_describe_counts_tokens():
    v = dv.DoubaoVision("k")
    v._post = lambda body: {"choices": [{"message": {"content": '{"item":"x","keywords":["church note"]}'}}],
                            "usage": {"prompt_tokens": 1000, "completion_tokens": 30, "total_tokens": 1030}}
    assert v.describe("https://x/y.jpg")["keywords"] == ["church note"]
    assert (v.calls, v.prompt_tokens, v.completion_tokens) == (1, 1000, 30) and v.cost_cny > 0


def test_title_unclear():
    assert akw.title_unclear("Reusable Sticker Book") is False
    assert akw.title_unclear("Cute Box") is True
    assert akw.title_unclear("Box of variety Inspirational Quote Card") is False and akw.title_unclear("Gift Set") is True
    assert akw.title_unclear("Custom Nurse Journal, Medical Physician Notebook") is False


class FakeVision:
    def __init__(self, result=None, exc=False):
        self.result, self.exc, self.seen = result, exc, []

    def describe(self, url, title=""):
        self.seen.append(url)
        if self.exc:
            raise RuntimeError("x")
        return self.result


def _llm(vision, resp=None):
    l = OpenAICompatLLM("http://x", "k", "m", vision=vision)
    l._chat = lambda s, u: (setattr(l, "last_user", u), resp or {"keywords": ["box gift"]})[1]
    return l


def test_vision_always_feeds_text_and_d3():
    v = FakeVision({"item": "retro camera magnet", "scene": "fridge", "keywords": ["camera magnet"],
                    "text_on_product": ["Record your life"], "description": "retro camera magnet; fridge"})
    l = _llm(v, {"amazon_keywords": [{"keyword": "retro phone camera magnet", "direction": 1, "source": "image"},
                                     {"keyword": "record your life", "direction": 3, "source": "image"}]})
    an = l.analyze_product(EtsyProduct(1, "Retro Film Camera Glowing Mini Sign Magnet", image_url="https://i/x.jpg"), [])
    assert v.seen == ["https://i/x.jpg"] and l.last_user["image_text"] == ["Record your life"]
    assert [(k["keyword"], k["direction"], k["source"]) for k in an.keyword_items] == [
        ("retro phone camera magnet", 1, "image"), ("record your life", 3, "image")]


def test_vision_mode_unclear_skips_clear_titles():
    v = FakeVision({"item": "x", "keywords": ["y"], "description": "x"})
    v.mode = "unclear"
    l = _llm(v)
    l.analyze_product(EtsyProduct(1, "Custom Nurse Journal, Medical Physician Notebook", image_url="https://i/x.jpg"), [])
    assert v.seen == []
    l.analyze_product(EtsyProduct(2, "Cute Box", image_url="https://i/x.jpg"), [])
    assert v.seen == ["https://i/x.jpg"]


def test_parse_text_on_product():
    r = dv.parse('{"item":"mug","text_on_product":["Record your life", 5, ""]}')
    assert r["text_on_product"] == ["Record your life"]


def test_vision_failure_falls_back_to_title_only():
    for v in (FakeVision(None), FakeVision(exc=True)):
        an = _llm(v).analyze_product(EtsyProduct(3, "Cute Box", image_url="https://i/x.jpg"), [])
        assert an.keyword_items and all(k["source"] != "image" for k in an.keyword_items)


def test_main_image_url_from_ehunt():
    assert main_image_url(EtsyProduct(1, "t", raw={"ehunt": {"image_url": "https://e/i.jpg"}})) == "https://e/i.jpg"
    assert main_image_url(EtsyProduct(1, "t")) == ""
