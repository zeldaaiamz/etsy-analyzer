"""OpenAI 兼容 Chat Completions（OpenAI/DeepSeek/通义/xAI 等通过 LLM_BASE_URL 切换）。
评论多（≥50）时采用 map-reduce：每批 BATCH 条评论抽取信号与原文引用 → 合并计数 → 最终调用生成人群结论与搜索词。"""
import hashlib
import json
import logging
from dataclasses import asdict
from .base import LLMProvider
from ..models import ProductAnalysis
from ..audience import infer_audience
from ..http import request_json
from .. import amazon_keywords as akw

BATCH = 40

MAP_PROMPT = """你在分析 Etsy 买家评论，找出“谁在买/买给谁/什么场景”。输出 JSON：
{"signals": [{"signal": "中文信号，如 为孩子购买", "count": 本批中出现的评论条数, "audience": "对应人群"}],
 "quotes": [{"idx": 评论序号, "quote": "逐字摘录该评论中的片段", "signal": "对应信号"}]}
quotes 最多 4 条，必须逐字摘录。只输出 JSON。"""

REDUCE_PROMPT = """你是亚马逊美国站选品专家。输入为 Etsy 商品信息（标题/标签/材质/描述/价格），以及从买家评论中统计出的人群信号（已合并计数，可能为空）。
严格只输出一个 JSON 对象，不要 markdown、不要多余文字：
{"product_summary": "中文一句话：这是什么产品（材质/形态/用途）",
 "category": "英文亚马逊类目路径，如 Home & Kitchen > Wall Art",
 "target_audience": "中文目标人群，简洁（≤40 字）",
 "audience_basis": "reviews 或 listing_text",
 "audience_confidence": "high | medium | low",
 "audience_reasoning": "中文：依据什么得出人群；若无评论文本，必须明确写“无评论原文，仅依据标题/标签/描述推断”",
 "keywords": ["3-5 个英文亚马逊搜索词，2-5 个单词，美国买家真实会搜的通用说法，不含品牌/店铺名/Etsy 专用词如 handmade custom listing，从最精确到较宽泛排序"],
 "keyword_reasoning": "中文：为什么选这些搜索词",
 """ + akw.PROMPT_FIELD + """}
规则：
- 除 keywords 和 category 用英文外，其它字段一律用中文。
- audience_signals 为空或 review_count=0 时：audience_basis=listing_text，audience_confidence 只能是 low 或 medium，不得声称“买家评论显示”。
- 不要编造评论内容或销量数据。
""" + akw.PROMPT_RULES


MATCH_PROMPT = """判断每个亚马逊候选商品与 Etsy 商品的关系。严格只输出 JSON：
{"results": [{"i": 候选序号, "label": "同款 | 类似品 | 无关", "reason": "中文 ≤25 字"}]}
定义：同款=同一种产品，形态/材质/用途/主要设计基本一致（可不同卖家）；类似品=同一品类、能满足同一需求的替代品；无关=品类或用途不同。
必须覆盖每一个候选，宁严勿宽。"""

CACHE_VERSION = "v1"  # 改动解析逻辑时递增，配合 prompt 哈希使旧缓存失效
log = logging.getLogger(__name__)


def _h(*parts):
    return hashlib.sha256("\x1f".join(str(x) for x in parts).encode()).hexdigest()[:16]


def image_description(p):
    """DeepSeek 无法看图：只使用流水线里已有的图片描述（目前 EHunt/Etsy 数据都不含，返回空）。"""
    raw = p.raw or {}
    for k in ("image_description", "image_analysis", "image_caption"):
        eh = raw.get("ehunt") if isinstance(raw.get("ehunt"), dict) else {}
        v = raw.get(k) or eh.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:1000]
    return ""


class OpenAICompatLLM(LLMProvider):
    def __init__(self, base_url, api_key, model, cache=None, ttl=86400):
        if not api_key:
            raise ValueError("LLM_API_KEY 未配置")
        self.url, self.key, self.model = base_url.rstrip("/"), api_key, model
        self.cache, self.ttl = cache, ttl
        self.calls, self.units_used, self.cache_hits = 0, 0, 0  # units = tokens

    def analyze_cache_key(self, p, reviews):
        return f"llm:identify:{p.listing_id}:{_h(CACHE_VERSION, self.model, MAP_PROMPT, REDUCE_PROMPT, BATCH, len(reviews))}"

    def match_cache_key(self, p, items):
        asins = sorted({it.asin for it in items[:40] if it.asin})
        return f"llm:match:{p.listing_id}:{_h(CACHE_VERSION, self.model, MATCH_PROMPT)}:{_h(*asins)}"

    def analyze_product(self, p, reviews):
        ck = self.analyze_cache_key(p, reviews) if self.cache else None
        hit = self.cache.get(ck) if ck else None
        if hit:
            self.cache_hits += 1
            log.info("LLM identify %s: cache hit (0 call)", p.listing_id)
            an = ProductAnalysis(**hit)
            an.audience_evidence = {**(an.audience_evidence or {}), "llm_cache": True}
            return an
        an = self._analyze_product(p, reviews)
        if ck and an.keywords:
            self.cache.put(ck, "deepseek", asdict(an), self.ttl)
        return an

    def _chat(self, system, user):
        d = request_json("POST", f"{self.url}/chat/completions", {"Authorization": f"Bearer {self.key}"},
                         body={"model": self.model, "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]},
                         timeout=120)
        self.calls += 1
        self.units_used += int(((d or {}).get("usage") or {}).get("total_tokens") or 0)
        return json.loads(d["choices"][0]["message"]["content"])

    def _analyze_product(self, p, reviews):
        merged, quotes = {}, []
        batches = [reviews[i:i + BATCH] for i in range(0, len(reviews), BATCH)]
        for b in batches:  # map
            j = self._chat(MAP_PROMPT, [{"idx": k, "text": r.text[:500], "rating": r.rating} for k, r in enumerate(b)])
            for s in j.get("signals", []):
                m = merged.setdefault(s.get("signal", ""), {"signal": s.get("signal", ""), "count": 0, "audience": s.get("audience", "")})
                m["count"] += int(s.get("count") or 0)
            for q in j.get("quotes", []):
                k = q.get("idx")
                if isinstance(k, int) and 0 <= k < len(b) and q.get("quote", "").strip("…. ")[:30] in b[k].text:
                    quotes.append({"quote": q["quote"], "rating": b[k].rating, "signal": q.get("signal", ""),
                                   "source": b[k].source})
        signals = sorted(merged.values(), key=lambda s: -s["count"])
        info = {"title": p.title, "description": p.description[:2000], "tags": p.tags, "materials": p.materials,
                "price": p.price, "review_count": len(reviews), "audience_signals": signals,
                "review_phrases": [r.text[:160] for r in reviews[:15]]}
        img = image_description(p)
        if img:
            info["image_description"] = img
        j = self._chat(REDUCE_PROMPT, info)  # reduce
        _, ev = infer_audience(p, reviews)  # 评论数量/来源统计用确定性代码，避免 LLM 编造
        ev.update(method=(f"LLM({self.model}) 分 {len(batches)} 批（每批≤{BATCH} 条）阅读全部 {len(reviews)} 条评论，"
                          "合并信号计数后归纳人群；引用已校验为原文") if reviews
                  else "无评论：LLM 仅依据标题/标签/描述推断（可信度较低）",
                  signals=signals or ev["signals"], evidence=quotes[:10] or ev["evidence"])
        conf = j.get("audience_confidence") or "low"
        if not reviews and conf == "high":
            conf = "low"
        ev["confidence"] = conf
        if j.get("audience_reasoning"):
            ev["llm_reasoning"] = j["audience_reasoning"]
            if not reviews:
                ev["method"] += f"（置信度 {conf}）：" + j["audience_reasoning"]
        kws = [k.strip() for k in j.get("keywords", []) if isinstance(k, str) and k.strip()]
        return ProductAnalysis(j.get("product_summary", ""), j.get("category", ""), j.get("target_audience", ""),
                               kws[:5], j.get("keyword_reasoning", ""), ev,
                               akw.build(j.get("amazon_keywords"), p.title, fallback=kws))

    def rank_similarity(self, p, items):
        """一次调用批量判断所有候选：同款/类似品/无关 + 理由。失败时回退到标题词重叠。"""
        if not items:
            return items
        ck = self.match_cache_key(p, items) if self.cache else None
        hit = self.cache.get(ck) if ck else None
        if hit and isinstance(hit.get("by_asin"), dict):
            self.cache_hits += 1
            log.info("LLM match %s: cache hit (0 call)", p.listing_id)
            by = hit["by_asin"]
            for it in items:
                r = by.get(it.asin) or {}
                it.similarity = r.get("similarity", 0.0)
                it.similarity_reason = r.get("reason", "LLM 判定无关：未返回判定，按无关处理")
            return sorted(items, key=lambda i: i.similarity or 0, reverse=True)
        cands = [{"i": k, "title": (it.title or "")[:200], "price": it.price} for k, it in enumerate(items[:40])]
        try:
            j = self._chat(MATCH_PROMPT, {"etsy": {"title": p.title, "tags": p.tags[:13], "materials": p.materials[:8],
                                                   "price": p.price, "description": (p.description or "")[:600]},
                                          "candidates": cands})
        except Exception as e:  # noqa: BLE001
            out = super().rank_similarity(p, items)
            for it in out:
                it.similarity_reason = f"[LLM 失败回退词重叠: {str(e)[:60]}] " + it.similarity_reason
            return out
        score = {"同款": 0.9, "类似品": 0.5, "无关": 0.0}
        got = {}
        for r in j.get("results", []):
            if isinstance(r.get("i"), int):
                got[r["i"]] = r
        for k, it in enumerate(items):
            r = got.get(k)
            lab = (r or {}).get("label") if (r or {}).get("label") in score else "无关"
            it.similarity = score[lab]
            it.similarity_reason = f"LLM 判定{lab}：{(r or {}).get('reason', '未返回判定，按无关处理')}"
        if ck:
            self.cache.put(ck, "deepseek", {"by_asin": {it.asin: {"similarity": it.similarity, "reason": it.similarity_reason}
                                                        for it in items if it.asin}}, self.ttl)
        return sorted(items, key=lambda i: i.similarity or 0, reverse=True)
