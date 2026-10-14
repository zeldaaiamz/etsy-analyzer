"""OpenAI 兼容 Chat Completions（OpenAI/DeepSeek/通义/xAI 等通过 LLM_BASE_URL 切换）。
评论多（≥50）时采用 map-reduce：每批 BATCH 条评论抽取信号与原文引用 → 合并计数 → 最终调用生成人群结论与搜索词。"""
import json
from .base import LLMProvider
from ..models import ProductAnalysis
from ..audience import infer_audience
from ..http import request_json

BATCH = 40

MAP_PROMPT = """你在分析 Etsy 买家评论，找出“谁在买/买给谁/什么场景”。输出 JSON：
{"signals": [{"signal": "中文信号，如 为孩子购买", "count": 本批中出现的评论条数, "audience": "对应人群"}],
 "quotes": [{"idx": 评论序号, "quote": "逐字摘录该评论中的片段", "signal": "对应信号"}]}
quotes 最多 4 条，必须逐字摘录。只输出 JSON。"""

REDUCE_PROMPT = """你是亚马逊美国站选品专家。输入为 Etsy 商品信息，以及从买家评论中统计出的人群信号（已合并计数）。输出 JSON：
{"product_summary": "中文一句话说明是什么产品", "category": "英文亚马逊类目路径",
 "target_audience": "中文目标人群（简洁，依据信号计数）",
 "keywords": ["3-6 个美国买家会在亚马逊搜索的英文关键词"],
 "keyword_reasoning": "中文：为什么选这些搜索词"}
若没有评论信号，人群只能依据标题/标签/描述推断。只输出 JSON。"""


class OpenAICompatLLM(LLMProvider):
    def __init__(self, base_url, api_key, model):
        if not api_key:
            raise ValueError("LLM_API_KEY 未配置")
        self.url, self.key, self.model = base_url.rstrip("/"), api_key, model

    def _chat(self, system, user):
        d = request_json("POST", f"{self.url}/chat/completions", {"Authorization": f"Bearer {self.key}"},
                         body={"model": self.model, "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]},
                         timeout=120)
        return json.loads(d["choices"][0]["message"]["content"])

    def analyze_product(self, p, reviews):
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
                "price": p.price, "review_count": len(reviews), "audience_signals": signals}
        j = self._chat(REDUCE_PROMPT, info)  # reduce
        _, ev = infer_audience(p, reviews)  # 评论数量/来源统计用确定性代码，避免 LLM 编造
        ev.update(method=(f"LLM({self.model}) 分 {len(batches)} 批（每批≤{BATCH} 条）阅读全部 {len(reviews)} 条评论，"
                          "合并信号计数后归纳人群；引用已校验为原文") if reviews
                  else "无评论：LLM 仅依据标题/标签/描述推断（可信度较低）",
                  signals=signals or ev["signals"], evidence=quotes[:10] or ev["evidence"])
        return ProductAnalysis(j.get("product_summary", ""), j.get("category", ""), j.get("target_audience", ""),
                               list(j.get("keywords", []))[:6], j.get("keyword_reasoning", ""), ev)
