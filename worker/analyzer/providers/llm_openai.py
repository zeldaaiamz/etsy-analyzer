"""OpenAI 兼容 Chat Completions（OpenAI/DeepSeek/通义/xAI 等通过 LLM_BASE_URL 切换）。"""
import json
from .base import LLMProvider
from ..models import ProductAnalysis
from ..audience import infer_audience
from ..http import request_json

PROMPT = """你是亚马逊美国站选品专家。根据 Etsy 商品信息和买家评论输出 JSON：
{"product_summary": "中文一句话说明是什么产品", "category": "英文亚马逊类目路径",
 "target_audience": "中文目标人群（简洁）",
 "audience_signals": [{"signal": "中文信号，如 为孩子购买", "count": 次数, "audience": "对应人群"}],
 "audience_quotes": [{"quote": "原文评论片段（必须逐字引用输入中的评论）", "rating": 星级, "signal": "对应信号"}],
 "keywords": ["3-6 个美国买家会在亚马逊搜索的英文关键词"],
 "keyword_reasoning": "中文：为什么选这些搜索词"}
若没有评论，人群只能依据标题/标签/描述推断，audience_quotes 置空。只输出 JSON。"""


class OpenAICompatLLM(LLMProvider):
    def __init__(self, base_url, api_key, model):
        if not api_key:
            raise ValueError("LLM_API_KEY 未配置")
        self.url, self.key, self.model = base_url.rstrip("/"), api_key, model

    def analyze_product(self, p, reviews):
        info = {"title": p.title, "description": p.description[:2000], "tags": p.tags,
                "materials": p.materials, "price": p.price,
                "reviews": [{"text": r.text[:400], "rating": r.rating} for r in reviews[:40]]}
        d = request_json("POST", f"{self.url}/chat/completions",
                         {"Authorization": f"Bearer {self.key}"},
                         body={"model": self.model, "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": PROMPT},
                                            {"role": "user", "content": json.dumps(info, ensure_ascii=False)}]})
        j = json.loads(d["choices"][0]["message"]["content"])
        _, ev = infer_audience(p, reviews)  # 计数/来源用确定性规则，避免 LLM 编造数量
        texts = " ".join(r.text for r in reviews)
        quotes = [q for q in j.get("audience_quotes", []) if q.get("quote", "").strip("…. ")[:30] in texts]
        ev.update(method=("LLM(%s) 阅读 %d 条评论归纳人群；引用已校验为原文" % (self.model, len(reviews))) if reviews
                  else "无评论：LLM 仅依据标题/标签/描述推断（可信度较低）",
                  signals=j.get("audience_signals") or ev["signals"], evidence=quotes or ev["evidence"])
        return ProductAnalysis(j.get("product_summary", ""), j.get("category", ""),
                               j.get("target_audience", ""), list(j.get("keywords", []))[:6],
                               j.get("keyword_reasoning", ""), ev)
