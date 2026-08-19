from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


CsvList = Annotated[list[str], BeforeValidator(_split_csv)]


EVERGREEN_QUERIES = [
    "wordpress tutorial for beginners",
    "shopify tutorial for beginners",
    "website design tutorial",
    "email marketing tutorial",
    "affiliate marketing tutorial",
    "search engine optimization tutorial",
    "how to start an online business",
    "small business marketing ideas",
    "real estate investing for beginners",
    "mortgage advice first time buyer",
    "personal finance for beginners",
    "tax preparation tutorial",
    "insurance explained",
    "resume writing tutorial",
    "job interview tips",
    "online course creation tutorial",
    "graphic design tutorial",
    "logo design tutorial",
    "photography tutorial for beginners",
    "wedding photography tips",
    "video editing tutorial",
    "cake decorating tutorial",
    "baking tutorial for beginners",
    "meal prep tutorial",
    "woodworking plans tutorial",
    "home renovation tutorial",
    "DIY plumbing tutorial",
    "DIY electrical tutorial",
    "car repair tutorial",
    "car detailing tutorial",
    "dog training tutorial",
    "pet grooming tutorial",
    "gardening tutorial for beginners",
    "landscaping ideas tutorial",
    "fitness program for beginners",
    "home workout program",
    "yoga for beginners",
    "guitar lessons for beginners",
    "piano lessons for beginners",
    "learn Spanish for beginners",
    "learn English online",
    "sewing tutorial for beginners",
    "hair tutorial step by step",
    "skincare routine tutorial",
    "makeup tutorial for beginners",
    "travel guide itinerary",
    "wedding planning guide",
    "event planning tutorial",
    "excel tutorial for beginners",
    "accounting tutorial for beginners",
    "coding tutorial for beginners",
    "app development tutorial",
    "3d printing tutorial",
    "music production tutorial",
    "podcast tutorial for beginners",
    "public speaking course",
    "meditation course beginners",
    "homeschool curriculum review",
    "college application advice",
    "language course review",
]


MANUAL_CHECKPOINTS = {
    "NCtFYIDEXUo": "pixels-forum.com",
    "IUI7Sn0X_7k": "cakedecoratinginstructor.com",
    "ETJ--vHaigo": "andygrabertraining.com",
    "Dt6AvJ7WZSI": "fontanaknowledge.com",
}


DEFAULT_DROPPED_DOMAIN_FEED_URLS = [
    "https://raw.githubusercontent.com/WhoisFreaks/"
    "daily-expired-and-dropped-domains/main/0-latest-free-dropped-domains.csv",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "YouTube Domain Crawler"
    environment: str = "production"
    log_level: str = "INFO"

    youtube_api_key: str = ""
    database_url: str = "sqlite:///./crawler.db"
    dashboard_password: str = "change-me"
    admin_token: str = "change-me-too"

    watchlist_monthly_views: int = Field(default=5_000, ge=0)
    qualified_monthly_views: int = Field(default=20_000, ge=1)
    priority_monthly_views: int = Field(default=100_000, ge=1)
    target_qualified_domains: int = Field(default=100, ge=1)

    discovery_interval_minutes: int = Field(default=90, ge=15)
    search_calls_per_run: int = Field(default=3, ge=1, le=10)
    published_before_years: int = Field(default=3, ge=1, le=15)
    availability_batch_size: int = Field(default=100, ge=1, le=500)
    scheduler_enabled: bool = True

    porkbun_api_key: str = ""
    porkbun_secret_api_key: str = ""
    max_ordinary_registration_usd: float = Field(default=50.0, ge=1)
    porkbun_min_interval_seconds: float = Field(default=10.5, ge=0)

    resend_api_key: str = ""
    alert_email: str = "info@expandosaurus.com"
    alert_from: str = "Domain Crawler <crawler@expandosaurus.com>"

    # Web-wide Expandosaurus Link Hunter. Credentials can be staged in Railway
    # before this feature flag is enabled; no DataForSEO calls happen while false.
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_base_url: str = "https://api.dataforseo.com/v3"
    dataforseo_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    link_hunter_enabled: bool = False
    link_hunter_proof_batch_size: int = Field(default=5, ge=1, le=25)
    link_hunter_backlinks_per_domain: int = Field(default=25, ge=1, le=100)
    link_hunter_proof_max_cost_usd: float = Field(default=0.50, ge=0.05, le=5.0)
    link_hunter_verify_timeout_seconds: float = Field(default=10.0, ge=3.0, le=30.0)

    dropped_domain_feed_urls: CsvList = Field(
        default_factory=lambda: list(DEFAULT_DROPPED_DOMAIN_FEED_URLS)
    )

    legacy_dropped_checked: int = 120
    legacy_videos_checked: int = 214
    legacy_domains_checked: int = 124

    @property
    def registrar_enabled(self) -> bool:
        return bool(self.porkbun_api_key and self.porkbun_secret_api_key)

    @property
    def email_enabled(self) -> bool:
        return bool(self.resend_api_key and self.alert_email and self.alert_from)

    @property
    def dataforseo_enabled(self) -> bool:
        return bool(self.dataforseo_login and self.dataforseo_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
