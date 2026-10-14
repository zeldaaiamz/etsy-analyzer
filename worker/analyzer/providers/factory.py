from .mock import MockEtsy, MockAmazon, MockLLM, MockScreenshot


def build_providers(cfg):
    if cfg.etsy_provider == "etsy_api":
        from .etsy_api import EtsyOpenAPI
        etsy = EtsyOpenAPI(cfg.etsy_api_key)
    elif cfg.etsy_provider == "ehunt":
        from .ehunt import EHuntEtsy
        etsy = EHuntEtsy(cfg.ehunt_api_key)
    else:
        etsy = MockEtsy()
    if cfg.amazon_provider == "rainforest":
        from .rainforest import RainforestAmazon
        amazon = RainforestAmazon(cfg.rainforest_api_key)
    elif cfg.amazon_provider == "keepa":
        from .keepa import KeepaAmazon
        amazon = KeepaAmazon(cfg.keepa_api_key, cfg.keepa_per_keyword)
    elif cfg.amazon_provider == "keepamore":
        from .keepamore import KeepamoreAmazon
        amazon = KeepamoreAmazon(cfg.keepamore_api_key, cfg.keepamore_per_keyword)
    elif cfg.amazon_provider == "spapi":
        raise NotImplementedError(f"{cfg.amazon_provider} provider 待实现（见 ARCHITECTURE.md 第 8 节）")
    else:
        amazon = MockAmazon()
    if cfg.llm_provider == "openai_compat":
        from .llm_openai import OpenAICompatLLM
        llm = OpenAICompatLLM(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model)
    else:
        llm = MockLLM()
    return etsy, amazon, llm


def build_screenshot(cfg):
    if cfg.screenshot_provider == "screenshot_api":
        from .screenshot import ScreenshotApiScreenshot
        return ScreenshotApiScreenshot(cfg.screenshot_api_url_template, cfg.screenshot_api_key)
    if cfg.screenshot_provider == "product_image":
        from .screenshot import ProductImageScreenshot
        return ProductImageScreenshot()
    return MockScreenshot()
