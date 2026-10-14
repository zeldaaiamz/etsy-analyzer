"""OpenAI 兼容 Chat Completions（OpenAI/DeepSeek/通义/xAI 等通过 LLM_BASE_URL 切换）。"""
import json
from .base import LLMProvider
from ..models import ProductAnalysis
from ..http import request_json

PROMPT = """你是亚马逊美国站选品专家。根据 Etsy 商品信息输出 JSON：
{"product_summary": "中文一句话说明是什么产品", "category": "英文亚马逊类目路径",
 "target_audience": "中文目标人群", "keywords": ["3-6 个美国买家会在亚马逊搜索的英文关键词"]}
只输出 JSON。"""


class OpenAICompatLLM(LLMProvider):
    def __init__(self, base_url, api_key, model):
        if not api_key:
            raise ValueError("LLM_API_KEY 未配置")
        self.url, self.key, self.model = base_url.rstrip("/"), api_key, model

    def analyze_product(self, p):
        info = {"title": p.title, "description": p.description[:2000], "tags": p.tags,
                "materials": p.materials, "price": p.price}
        d = request_json("POST", f"{self.url}/chat/completions",
                         {"Authorization": f"Bearer {self.key}"},
                         body={"model": self.model, "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": PROMPT},
                                            {"role": "user", "content": json.dumps(info, ensure_ascii=False)}]})
        j = json.loads(d["choices"][0]["message"]["content"])
        return ProductAnalysis(j.get("product_summary", ""), j.get("category", ""),
                               j.get("target_audience", ""), list(j.get("keywords", []))[:6])
