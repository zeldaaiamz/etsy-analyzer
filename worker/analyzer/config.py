import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv  # 可选
    load_dotenv()
except ImportError:
    pass


@dataclass
class Config:
    mode: str = "mock"
    worker_id: str = "worker-1"
    poll_interval: float = 5.0
    max_amazon_results: int = 10
    supabase_url: str = ""
    supabase_key: str = ""
    etsy_provider: str = "mock"
    etsy_api_key: str = ""
    amazon_provider: str = "mock"
    rainforest_api_key: str = ""
    keepa_api_key: str = ""
    llm_provider: str = "mock"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    screenshot_provider: str = "mock"
    screenshot_api_url_template: str = ""
    screenshot_api_key: str = ""
    storage_bucket: str = "snapshots"
    max_snapshots_per_kind: int = 0  # 0 = 不限
    min_reviews: int = 50
    supplement_shop_reviews: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(
            mode=e("WORKER_MODE", "mock"),
            worker_id=e("WORKER_ID", "worker-1"),
            poll_interval=float(e("POLL_INTERVAL_SECONDS", "5")),
            max_amazon_results=int(e("MAX_AMAZON_RESULTS", "10")),
            supabase_url=e("SUPABASE_URL", ""),
            supabase_key=e("SUPABASE_SERVICE_ROLE_KEY", ""),
            etsy_provider=e("ETSY_PROVIDER", "mock"),
            etsy_api_key=e("ETSY_API_KEY", ""),
            amazon_provider=e("AMAZON_PROVIDER", "mock"),
            rainforest_api_key=e("RAINFOREST_API_KEY", ""),
            keepa_api_key=e("KEEPA_API_KEY", ""),
            llm_provider=e("LLM_PROVIDER", "mock"),
            llm_base_url=e("LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_api_key=e("LLM_API_KEY", ""),
            llm_model=e("LLM_MODEL", "gpt-4o-mini"),
            screenshot_provider=e("SCREENSHOT_PROVIDER", "mock"),
            screenshot_api_url_template=e("SCREENSHOT_API_URL_TEMPLATE", ""),
            screenshot_api_key=e("SCREENSHOT_API_KEY", ""),
            storage_bucket=e("STORAGE_BUCKET", "snapshots"),
            max_snapshots_per_kind=int(e("MAX_SNAPSHOTS_PER_KIND", "0")),
            min_reviews=int(e("MIN_REVIEWS", "50")),
            supplement_shop_reviews=e("SUPPLEMENT_SHOP_REVIEWS", "false").lower() in ("1", "true", "yes"),
        )
