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


def test_build_dedupes_caps_and_keeps_one_original_title():
    raw = [{"keyword": "Church Note", "source": "title", "basis": "标题 Church Notes"},
           {"keyword": "church notes", "source": "title"},          # 复数重复
           {"keyword": "note church", "source": "review"},          # 词序重复
           {"keyword": "sermon notebook", "source": "original-title"},
           {"keyword": "notes journal", "source": "original-title"},  # 第二个 original-title 丢弃
           {"keyword": "bible study journal", "source": "review"},
           {"keyword": "prayer journal", "source": "image"},
           {"keyword": "christian journal", "source": "title"},
           {"keyword": "faith gift", "source": "title"}]
    out = akw.build(raw, TITLE)
    assert len(out) == 5
    assert srcs(out).count("original-title") == 1 and out[-1]["keyword"] == "sermon notebook"
    keys = [akw.dedup_key(i["keyword"]) for i in out]
    assert len(keys) == len(set(keys))
    assert out[0] == {"keyword": "church note", "source": "title", "basis": "标题 Church Notes"}


def test_original_title_must_be_verbatim_else_fallback():
    out = akw.build([{"keyword": "praise book", "source": "original-title"}], TITLE)
    o = [i for i in out if i["source"] == "original-title"]
    assert len(o) == 1 and f" {o[0]['keyword']} " in f" {akw.norm(TITLE)} "


def test_invalid_sources_and_manual_from_llm():
    out = akw.build([{"keyword": "x y", "source": "made-up"}, {"keyword": "summer note", "source": "manual"},
                     "plain string kw", 42, {"keyword": "", "source": "title"}], "Summer Notebook")
    assert {"keyword": "summer note", "source": "title", "basis": ""} in out  # LLM 不能产出 manual
    assert all(i["source"] in akw.SOURCES and i["source"] != "manual" for i in out)
    assert "x y" not in [i["keyword"] for i in out] and "plain string kw" in [i["keyword"] for i in out]


def test_fallback_old_keywords_and_original_dedup():
    out = akw.build(None, "Reusable Sticker Book", fallback=["reusable sticker book", "cat stickers", "Cat Sticker"])
    assert [(i["keyword"], i["source"]) for i in out] == [("cat stickers", "title"), ("reusable sticker book", "original-title")]


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
                    "amazon_keywords": [{"keyword": "church note", "source": "title", "basis": "标题"},
                                        {"keyword": "sermon notebook", "source": "original-title"}]})
    an = l.analyze_product(EtsyProduct(1, TITLE), [])
    assert "amazon_keywords" in seen["sys"] and "original-title" in seen["sys"]
    assert "image_description" not in seen["user"]  # 无图片描述时不传
    assert srcs(an.keyword_items) == ["title", "original-title"]


def test_llm_uses_existing_image_description_and_old_response_fallback():
    l, seen = _llm({"keywords": ["mtg tokens", "game tokens"]})  # 旧格式：无 amazon_keywords
    an = l.analyze_product(EtsyProduct(2, "MTG Token Set", raw={"ehunt": {"image_description": "a stack of cards"}}), [])
    assert seen["user"]["image_description"] == "a stack of cards"
    # 原标题短语 "mtg token" 与 "mtg tokens" 近似重复 → 只保留 original-title 那条
    assert [(i["keyword"], i["source"]) for i in an.keyword_items] == [("game tokens", "title"), ("mtg token", "original-title")]
    assert srcs(an.keyword_items).count("original-title") == 1


def test_mock_llm_has_keyword_items():
    an = MockLLM().analyze_product(EtsyProduct(3, "Summer Note Book", tags=["summer note"]), [])
    assert an.keyword_items and len(an.keyword_items) <= 5
