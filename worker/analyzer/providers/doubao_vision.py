"""PM-001：豆包视觉（火山方舟 Ark，OpenAI 兼容）看 Etsy 主图 → 物品/场景描述 + 1-2 个图片关键词（source=image）。
失败一律返回 None，调用方回退到仅标题，绝不中断分析。"""
import json
import logging
import re
from ..http import request_json

log = logging.getLogger(__name__)
ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seed-2-1-turbo-260628"  # 本账号已开通的视觉模型（vision-pro / seed-1-6-vision 已下线或未开通）
# 价格（元 / 百万 tokens，按方舟控制台标价核对后可改）：仅用于报告估算
PRICE_IN, PRICE_OUT = 3.0, 15.0  # doubao-seed-2.1-turbo 标准价（2026-10 官网）

PROMPT = """You are an Amazon US product researcher. Look at this Etsy product's main image (title given for context).
Return ONLY a JSON object:
{"item": "the most specific name of the physical item incl. its core theme, <=12 words, e.g. retro phone camera fridge magnet",
 "scene": "usage scene / occasion / audience suggested by the image, <=15 words",
 "text_on_product": ["slogans / taglines / phrases printed ON the product itself, copied verbatim (max 3); [] if none or unreadable"],
 "keywords": ["1-2 English Amazon search phrases, 2-5 words, based on what is visible"]}
Do not invent text that is not visible. No brand names; no words like handmade/custom/etsy."""


class DoubaoVision:
    name = "doubao"
    mode = "always"  # always：每个产品都看图（找 D3 文案）；unclear：仅标题不清楚时

    def __init__(self, api_key, model=DEFAULT_MODEL, base=ARK_BASE, timeout=60):
        if not api_key:
            raise ValueError("ARK_API_KEY 未配置")
        self.key, self.model, self.base, self.timeout = api_key, model, base.rstrip("/"), timeout
        self.calls, self.units_used, self.cache_hits, self.prompt_tokens, self.completion_tokens, self.failures = 0, 0, 0, 0, 0, 0

    @property
    def cost_cny(self):
        return round((self.prompt_tokens * PRICE_IN + self.completion_tokens * PRICE_OUT) / 1e6, 4)

    def _post(self, body):
        return request_json("POST", f"{self.base}/chat/completions", {"Authorization": f"Bearer {self.key}"},
                            body=body, timeout=self.timeout)

    def describe(self, image_url, title=""):
        if not image_url or not re.match(r"^https?://", image_url):
            return None
        body = {"model": self.model, "max_tokens": 300, "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": PROMPT + f"\nTitle: {str(title)[:200]}"}]}]}
        try:
            d = self._post(body)
            self.calls += 1
            u = (d or {}).get("usage") or {}
            self.prompt_tokens += int(u.get("prompt_tokens") or 0)
            self.completion_tokens += int(u.get("completion_tokens") or 0)
            self.units_used += int(u.get("total_tokens") or 0)
            return parse(d["choices"][0]["message"]["content"])
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            log.warning("doubao vision failed: %s", str(e)[:200])
            return None


def parse(text):
    """容忍 ```json 包裹 / 前后多余文字；结构不对返回 None。"""
    if not isinstance(text, str):
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    kws = [k.strip() for k in (j.get("keywords") or []) if isinstance(k, str) and k.strip()][:2]
    txt = [t.strip()[:120] for t in (j.get("text_on_product") or []) if isinstance(t, str) and t.strip()][:3]
    item, scene = str(j.get("item") or "").strip()[:120], str(j.get("scene") or "").strip()[:120]
    if not (item or kws or txt):
        return None
    return {"item": item, "scene": scene, "keywords": kws, "text_on_product": txt,
            "description": "; ".join(x for x in (item, scene) if x)}


def from_env(env):
    """DOUBAO_VISION_ENABLED=1 且有 ARK_API_KEY 才启用；否则 None（仅标题）。"""
    if str(env.get("DOUBAO_VISION_ENABLED", "0")).strip().lower() not in ("1", "true", "yes", "on"):
        return None
    if not env.get("ARK_API_KEY"):
        return None
    v = DoubaoVision(env["ARK_API_KEY"], env.get("DOUBAO_VISION_MODEL") or DEFAULT_MODEL, env.get("ARK_BASE_URL") or ARK_BASE)
    v.mode = "unclear" if str(env.get("DOUBAO_VISION_MODE", "always")).lower() == "unclear" else "always"
    return v
