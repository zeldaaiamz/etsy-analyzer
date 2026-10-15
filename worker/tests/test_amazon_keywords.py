"""PM-001 一键亚马逊搜索：关键词生成与解析。"""
import pytest
from analyzer import amazon_keywords as akw
from analyzer.models import EtsyProduct
from analyzer.providers import llm_openai
from analyzer.providers.llm_openai import OpenAICompatLLM
from analyzer.providers.mock import MockLLM

TITLE = "Church Notes Journal, Sermon Notebook for Women, Christian Gift"


def srcs(items):
    return [i["source"] for i in items]


def dirs(items):
    return [i["direction"] for i in items]


def test_build_d1_first_one_per_direction_dedup_cap():
    raw = [{"keyword": "church notes", "direction": 2, "source": "title", "basis": "同场景"},
           {"keyword": "Sermon Notes Journal for Women", "direction": 1, "source": "title"},
           {"keyword": "sermon notebook", "direction": 1},                 # 第二个 D1 → 只保留第一个
           {"keyword": "church note", "direction": 2},                     # 单复数重复
           {"keyword": "christian gift", "direction": 4},
           {"keyword": "bible study journal", "direction": 2},
           {"keyword": "church supplies for women", "direction": 5, "source": "image"},
           {"keyword": "faith gift", "direction": 4}]
    out = akw.build(raw, TITLE)
    assert len(out) == 5 and dirs(out) == [1, 2, 4, 5, 2]
    assert out[0]["keyword"] == "sermon notes journal for women" and dirs(out).count(1) == 1
    keys = [akw.dedup_key(i["keyword"]) for i in out]
    assert len(keys) == len(set(keys))
    assert "original-title" not in srcs(out)


def test_d3_slogan_must_be_verbatim_in_title_or_image_text():
    raw = [{"keyword": "retro phone camera magnet", "direction": 1, "source": "image"},
           {"keyword": "record your life", "direction": 3, "source": "image"},
           {"keyword": "made up slogan", "direction": 3, "source": "image"}]
    out = akw.build(raw, "Retro Camera Magnet", image_text=["Record your life!"])
    assert [(i["keyword"], i["direction"]) for i in out] == [("retro phone camera magnet", 1), ("record your life", 3)]
    assert [i["keyword"] for i in akw.build(raw, "Retro Camera Magnet")] == ["retro phone camera magnet"]


def test_invalid_direction_source_and_manual_from_llm():
    out = akw.build([{"keyword": "x y", "direction": 1, "source": "made-up"}, {"keyword": "no dir", "source": "title"},
                     {"keyword": "summer note", "direction": "D1", "source": "manual"},
                     {"keyword": "old phrase", "direction": 2, "source": "original-title"}, 42], "Summer Notebook")
    assert [(i["keyword"], i["direction"], i["source"]) for i in out] == [("summer note", 1, "title"), ("old phrase", 2, "title")]


def test_missing_d1_falls_back():
    out = akw.build([{"keyword": "cat stickers", "direction": 2}], "Reusable Sticker Book", fallback=["reusable cat sticker book"])
    assert out[0] == {"keyword": "reusable cat sticker book", "direction": 1, "source": "title",
                      "basis": "兜底：旧分析搜索词 / 原标题核心短语"}
    assert akw.build(None, "Reusable Sticker Book")[0]["keyword"] == "reusable sticker book"


@pytest.mark.parametrize("title,phrase", [
    ("Breath Prayer Scripture Cards: Christian Affirmations", "prayer scripture cards"),
    ("Custom Nurse Journal, Medical Physician Notebook", "nurse journal"),
    ("70th Birthday- Quilling card", "70th birthday"),
    ("Handmade Gift", ""),
])
def test_core_phrase(title, phrase):
    assert akw.core_phrase(title) == phrase


def test_parse_manual():
    old = [{"keyword": "church note", "source": "title", "basis": "b"}]
    assert akw.parse_manual(old + [{"keyword": " Church  Note "}, "bible journal"]) == \
        [{"keyword": "church note", "source": "title", "basis": "b"},
         {"keyword": "bible journal", "source": "manual", "basis": ""}]
    with pytest.raises(ValueError):
        akw.parse_manual([f"kw {i}" for i in range(6)])


def test_search_url_encoding():
    assert akw.search_url("church note & bible") == "https://www.amazon.com/s?k=church%20note%20%26%20bible"


def _llm(resp):
    l = OpenAICompatLLM("http://x", "k", "m")
    seen = {}

    def chat(s, u):
        seen["sys"], seen["user"] = s, u
        return resp
    l._chat = chat
    return l, seen


def test_llm_analysis_parses_amazon_keywords():
    l, seen = _llm({"product_summary": "s", "category": "c", "target_audience": "t", "keywords": ["church note"],
                    "amazon_keywords": [{"keyword": "sermon notes journal", "direction": 1, "source": "title", "basis": "标题"},
                                        {"keyword": "christian gift", "direction": 4, "source": "title"}]})
    an = l.analyze_product(EtsyProduct(1, TITLE), [])
    assert "D1 精准词" in seen["sys"] and "D5 细分场景词" in seen["sys"]
    assert "image_description" not in seen["user"]  # 无图片信息时不传
    assert dirs(an.keyword_items) == [1, 4]


def test_llm_existing_image_description_and_old_response_fallback():
    l, seen = _llm({"keywords": ["mtg tokens", "game tokens"]})  # 旧格式：无 amazon_keywords
    an = l.analyze_product(EtsyProduct(2, "MTG Token Set", raw={"ehunt": {"image_description": "a stack of cards"}}), [])
    assert seen["user"]["image_description"] == "a stack of cards"
    assert [(i["keyword"], i["direction"]) for i in an.keyword_items] == [("mtg tokens", 1)]


def test_mock_llm_has_keyword_items():
    an = MockLLM().analyze_product(EtsyProduct(3, "Summer Note Book", tags=["summer note"]), [])
    assert an.keyword_items and len(an.keyword_items) <= 5


def test_d3_brand_logo_dropped():
    out = akw.build([{"keyword": "retro camera sign magnet", "direction": 1},
                     {"keyword": "kodak polaroid fujifilm", "direction": 3, "source": "image"}],
                    "Retro Camera Sign Magnet", image_text=["Kodak", "Polaroid", "FUJIFILM"])
    assert [i["direction"] for i in out] == [1]
