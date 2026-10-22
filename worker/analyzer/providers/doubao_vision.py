"""PM-001：豆包视觉（火山方舟 Ark，OpenAI 兼容）看 Etsy 主图 → 物品/场景描述 + 1-2 个图片关键词（source=image）。
失败返回 None，调用方回退到仅标题；失败原因记在 last_error（kind/message），仅主图的任务据此决定重试或提示。

图片由 worker 自己下载（超时 + 大小限制）后以 base64 data URL 发给 ARK：ARK 从国内拉 i.etsystatic.com 常超时
（HTTP 400 InvalidParameter "Timeout while downloading url"）。下载失败才退回把 URL 交给 ARK 去拉。"""
import base64
import json
import logging
import os
import re
import time
import urllib.request
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


IMG_TIMEOUT = float(os.environ.get("DOUBAO_IMAGE_TIMEOUT_SECONDS", "8"))
IMG_MAX_BYTES = int(os.environ.get("DOUBAO_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))  # ARK base64 单图上限约 10MB
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

ARK_FORMATS = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/bmp")  # 不发 avif/heic（ARK 报 UnsupportedImageFormat）

# 失败分类（kind）：pipeline 据此给出真实原因；只有 auth / billing 才提示检查配置或充值
IMAGE_TIMEOUT, IMAGE_DOWNLOAD, AUTH, BILLING, RATE, OTHER = "image_timeout", "image_download", "auth", "billing", "rate", "other"
RETRYABLE_KINDS = (IMAGE_TIMEOUT, IMAGE_DOWNLOAD, RATE)
MESSAGES = {IMAGE_TIMEOUT: "豆包读取图片超时", IMAGE_DOWNLOAD: "图片下载失败", AUTH: "豆包 ARK 鉴权失败（检查 ARK_API_KEY / 模型开通）",
            BILLING: "豆包 ARK 余额不足或欠费（请充值）", RATE: "豆包 ARK 限流", OTHER: "豆包看图失败"}


def classify(err):
    """把 ARK / 下载异常归类。ARK 拉图超时是 HTTP 400 InvalidParameter "Timeout while downloading url"。"""
    m = str(err or "")
    low = m.lower()
    if "timeout while downloading" in low or "timed out" in low or "timeout" in low:
        return IMAGE_TIMEOUT
    if re.search(r"downloading url|download.{0,20}(fail|error)|invalid image|invalid base64|unsupportedimageformat|image.{0,20}(fail|error)", low):
        return IMAGE_DOWNLOAD
    if re.search(r"HTTP 40[13]\b", m) and re.search(r"overdue|arrear|balance|insufficient|欠费|余额", low):
        return BILLING
    if re.search(r"HTTP 40[13]\b", m) or re.search(r"authenticationerror|invalid.?api.?key|unauthorized|permission|not activated|accountoverdue", low):
        return BILLING if "overdue" in low else AUTH
    if re.search(r"HTTP 402\b|insufficient.?(balance|quota)|overdue", low):
        return BILLING
    if re.search(r"HTTP 429\b|rate.?limit|too many requests", low):
        return RATE
    return OTHER


def fetch_image(url, timeout=IMG_TIMEOUT, max_bytes=IMG_MAX_BYTES):
    """下载图片 → data:<mime>;base64,...；超时、非图片、超限都抛异常（调用方退回 URL）。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "image/jpeg,image/png,image/webp;q=0.9,*/*;q=0.5"})
    deadline = time.monotonic() + timeout  # 总耗时上限（socket 超时只管单次读）
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            raise ValueError(f"not an image: {ctype or 'unknown'}")
        if ctype not in ARK_FORMATS:
            raise ValueError(f"unsupported image format for ARK: {ctype}")
        n = int(r.headers.get("Content-Length") or 0)
        if n > max_bytes:
            raise ValueError(f"image too large: {n} bytes")
        chunks, got = [], 0
        while got <= max_bytes:
            if time.monotonic() > deadline:
                raise TimeoutError(f"image download timed out after {timeout:.0f}s")
            c = r.read(64 * 1024)
            if not c:
                break
            chunks.append(c); got += len(c)
        data = b"".join(chunks)
    if len(data) > max_bytes:
        raise ValueError(f"image too large: >{max_bytes} bytes")
    if not data:
        raise ValueError("empty image")
    return f"data:{ctype};base64,{base64.b64encode(data).decode()}"


class DoubaoVision:
    name = "doubao"
    mode = "always"  # always：每个产品都看图（找 D3 文案）；unclear：仅标题不清楚时

    def __init__(self, api_key, model=DEFAULT_MODEL, base=ARK_BASE, timeout=60):
        if not api_key:
            raise ValueError("ARK_API_KEY 未配置")
        self.key, self.model, self.base, self.timeout = api_key, model, base.rstrip("/"), timeout
        self.calls, self.units_used, self.cache_hits, self.prompt_tokens, self.completion_tokens, self.failures = 0, 0, 0, 0, 0, 0
        self.last_error = None  # {"kind", "message", "detail"}：最近一次 describe 失败的原因；成功时清空
        self.fetch = fetch_image

    @property
    def cost_cny(self):
        return round((self.prompt_tokens * PRICE_IN + self.completion_tokens * PRICE_OUT) / 1e6, 4)

    def _post(self, body):
        return request_json("POST", f"{self.base}/chat/completions", {"Authorization": f"Bearer {self.key}"},
                            body=body, timeout=self.timeout)

    def _body(self, img, title):
        return {"model": self.model, "max_tokens": 300, "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": img}},
                    {"type": "text", "text": PROMPT + f"\nTitle: {str(title)[:200]}"}]}]}

    def _fail(self, kind, detail):
        self.failures += 1
        self.last_error = {"kind": kind, "message": MESSAGES.get(kind, MESSAGES[OTHER]), "detail": str(detail)[:300]}
        log.warning("doubao vision failed (%s): %s", kind, str(detail)[:200])
        return None

    def describe(self, image_url, title=""):
        self.last_error = None
        if not image_url or not re.match(r"^https?://", image_url):
            return None
        t0 = time.monotonic()
        try:  # 1) 自己下载 → base64；失败退回 URL（由 ARK 去拉）
            img, how = self.fetch(image_url), "base64"
        except Exception as e:  # noqa: BLE001
            log.info("doubao vision: own download failed (%s), fall back to URL", str(e)[:160])
            img, how, dl_err = image_url, "url", e
        else:
            dl_err = None
        t1 = time.monotonic()
        try:
            try:
                d = self._post(self._body(img, title))
            except Exception as e:  # noqa: BLE001
                if how != "base64" or classify(e) != IMAGE_DOWNLOAD:
                    raise
                log.info("doubao vision: ARK rejected base64 image (%s), retry with URL", str(e)[:160])
                img, how = image_url, "url"
                d = self._post(self._body(img, title))
        except Exception as e:  # noqa: BLE001
            kind = classify(e)
            if how == "url" and kind == OTHER and dl_err is not None:  # ARK 原因不明时，用自己下载失败的原因
                kind = IMAGE_TIMEOUT if classify(dl_err) == IMAGE_TIMEOUT else IMAGE_DOWNLOAD
            return self._fail(kind, f"{how}: {e}" + (f" | own download: {dl_err}" if dl_err else ""))
        self.calls += 1
        u = (d or {}).get("usage") or {}
        self.prompt_tokens += int(u.get("prompt_tokens") or 0)
        self.completion_tokens += int(u.get("completion_tokens") or 0)
        self.units_used += int(u.get("total_tokens") or 0)
        log.info("doubao vision ok via %s: download %.2fs, ark %.2fs", how, t1 - t0, time.monotonic() - t1)
        try:
            out = parse(d["choices"][0]["message"]["content"])
        except Exception as e:  # noqa: BLE001
            return self._fail(OTHER, f"bad response: {e}")
        if out is None:
            return self._fail(OTHER, "unparseable vision output")
        return out


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
