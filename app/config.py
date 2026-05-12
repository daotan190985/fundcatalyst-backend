"""Application configuration loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    """All app config. Override via .env file or environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "FundCatalyst VN"
    app_version: str = "0.1.0"
    debug: bool = False
    allowed_origins: list[str] = ["*"]  # tighten in production

    # Database
    database_url: str = "postgresql://fcvn:fcvn@localhost:5432/fundcatalyst"

    # Redis (cache)
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_quote: int = 60  # 1 minute for live quotes
    cache_ttl_company: int = 86400  # 1 day for company info

    # vnstock data source
    vnstock_source: str = "VCI"  # VCI, TCBS, MSN
    vnstock_max_retries: int = 3
    vnstock_retry_delay: float = 2.0

    # Tickers to track (start with VN30, expandable)
    default_tickers: list[str] = [
        "FPT", "HPG", "VCB", "VHM", "MWG", "GAS", "DGC", "CTG",
        "PNJ", "VNM", "ACB", "MBB", "BID", "TCB", "SSI", "MSN",
        "VIC", "VRE", "PLX", "POW", "VPB", "STB", "HDB", "VJC",
        "BVH", "GVR", "SAB", "VIB", "TPB", "PDR"
    ]

    # Scheduler
    enable_scheduler: bool = True
    quote_refresh_interval_min: int = 5  # during trading hours
    financials_refresh_interval_hour: int = 24


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
