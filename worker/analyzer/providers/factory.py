from .mock import MockEtsy, MockAmazon, MockLLM


def build_providers(cfg):
    if cfg.etsy_provider == "etsy_api":
        from .etsy_api import EtsyOpenAPI
        etsy = EtsyOpenAPI(cfg.etsy_api_key)
    else:
        etsy = MockEtsy()
    if cfg.amazon_provider == "rainforest":
        from .rainforest import RainforestAmazon
        amazon = RainforestAmazon(cfg.rainforest_api_key)
    elif cfg.amazon_provider in ("keepa", "spapi"):
        raise NotImplementedError(f"{cfg.amazon_provider} provider 待实现（见 ARCHITECTURE.md 第 8 节）")
    else:
        amazon = MockAmazon()
    if cfg.llm_provider == "openai_compat":
        from .llm_openai import OpenAICompatLLM
        llm = OpenAICompatLLM(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model)
    else:
        llm = MockLLM()
    return etsy, amazon, llm
