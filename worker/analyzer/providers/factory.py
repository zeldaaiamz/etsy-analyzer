from .mock import MockEtsy, MockAmazon, MockLLM, MockScreenshot


def build_providers(cfg, cache=None, store=None):
    if cfg.etsy_provider == "etsy_api":
        from .etsy_api import EtsyOpenAPI
        etsy = EtsyOpenAPI(cfg.etsy_api_key)
    elif cfg.etsy_provider == "ehunt":
        from .ehunt import EHuntEtsy
        etsy = EHuntEtsy(cfg.ehunt_api_key, cache=cache, ttl=cfg.ehunt_cache_ttl)
    elif cfg.etsy_provider == "ehunt_web":
        from .ehunt import EHuntEtsy
        from .ehunt_web import EHuntWebEtsy
        api = EHuntEtsy(cfg.ehunt_api_key, cache=cache, ttl=cfg.ehunt_cache_ttl) if cfg.ehunt_web_prefer_api and cfg.ehunt_api_key else None
        etsy = EHuntWebEtsy(store if store is not None else getattr(cache, "store", None), cache=cache, ttl=cfg.ehunt_cache_ttl,
                            timeout=cfg.ehunt_web_timeout, stale=cfg.ehunt_web_stale, api=api)
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
        amazon = KeepamoreAmazon(cfg.keepamore_api_key, cfg.keepamore_per_keyword, cache=cache, ttl=cfg.keepamore_cache_ttl)
    elif cfg.amazon_provider == "spapi":
        raise NotImplementedError(f"{cfg.amazon_provider} provider 待实现（见 ARCHITECTURE.md 第 8 节）")
    else:
        amazon = MockAmazon()
    if cfg.llm_provider in ("openai_compat", "deepseek"):
        from .llm_openai import OpenAICompatLLM
        import os
        from .doubao_vision import from_env
        try:
            vision = from_env(os.environ)
        except Exception:  # noqa: BLE001
            vision = None
        llm = OpenAICompatLLM(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model, cache=cache,
                              ttl=getattr(cfg, "llm_cache_ttl", 86400), vision=vision)
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
