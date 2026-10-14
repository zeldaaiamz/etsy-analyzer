"""真实模式的证据图片 provider。
- ProductImageScreenshot：下载数据商返回的商品主图（合规、便宜）；搜索页则生成“由搜索数据渲染的摘要卡”（明确标注非截图）。
- ScreenshotApiScreenshot：调用第三方截图 API（如 ScreenshotOne/ApiFlash，或 Rainforest/ScraperAPI 自带截图功能），
  URL 模板来自 SCREENSHOT_API_URL_TEMPLATE，例如
  https://api.screenshotone.com/take?access_key={key}&url={url}&format=png&viewport_width=1280
  注意：亚马逊对自动访问会出验证码，本系统不做任何验证码绕过；截图失败时回退到商品主图。
"""
from urllib.parse import quote
from .base import ScreenshotProvider
from .mock import placeholder_svg
from ..http import request_bytes


class ProductImageScreenshot(ScreenshotProvider):
    def capture(self, kind, url, title="", image_url="", meta=None):
        if kind != "search_result" and image_url:
            try:
                data, ct = request_bytes("GET", image_url)
                return data, (ct or "image/jpeg").split(";")[0], "product_image"
            except Exception:  # noqa: BLE001
                pass
        svg = placeholder_svg(kind, title, {"URL": url, **(meta or {})}).replace(
            "MOCK 快照".encode(), "数据摘要卡".encode()).replace(
            "占位图（mock 模式生成，非真实截图）".encode(), "由 API 数据渲染，非网页截图".encode())
        return svg, "image/svg+xml", "data_card"


class ScreenshotApiScreenshot(ScreenshotProvider):
    def __init__(self, template, key):
        if not template:
            raise ValueError("SCREENSHOT_API_URL_TEMPLATE 未配置")
        self.template, self.key, self.fallback = template, key, ProductImageScreenshot()

    def capture(self, kind, url, title="", image_url="", meta=None):
        try:
            data, ct = request_bytes("GET", self.template.format(key=self.key, url=quote(url, safe="")), timeout=90)
            if ct.startswith("image/"):
                return data, ct.split(";")[0], "screenshot"
        except Exception:  # noqa: BLE001
            pass
        return self.fallback.capture(kind, url, title, image_url, meta)
