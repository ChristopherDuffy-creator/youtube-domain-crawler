from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    channel_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel_title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifetime_views: Mapped[int] = mapped_column(BigInteger, default=0)
    discovery_query: Mapped[str] = mapped_column(Text, default="")
    discovery_route: Mapped[str] = mapped_column(String(32), default="youtube_first")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    links: Mapped[list[VideoDomain]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[ViewSnapshot]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    suffix: Mapped[str] = mapped_column(String(100), default="")
    excluded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    availability_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    availability_source: Mapped[str] = mapped_column(String(32), default="")
    rdap_status: Mapped[str] = mapped_column(String(32), default="unchecked")
    dns_status: Mapped[str] = mapped_column(String(32), default="unchecked")
    http_status: Mapped[str] = mapped_column(String(32), default="unchecked")
    registrar_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    premium: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    check_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    video_links: Mapped[list[VideoDomain]] = relationship(
        back_populates="domain", cascade="all, delete-orphan"
    )
    candidate: Mapped[Candidate | None] = relationship(
        back_populates="domain", uselist=False, cascade="all, delete-orphan"
    )


class VideoDomain(Base):
    __tablename__ = "video_domains"
    __table_args__ = (
        UniqueConstraint("video_id", "domain_id", "raw_url", name="uq_video_domain_raw"),
        Index("ix_video_domains_domain_video", "domain_id", "video_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    raw_url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text)
    description_position: Mapped[float] = mapped_column(Float, default=1.0)
    context: Mapped[str] = mapped_column(Text, default="")
    has_cta: Mapped[bool] = mapped_column(Boolean, default=False)
    clickable: Mapped[bool] = mapped_column(Boolean, default=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    video: Mapped[Video] = relationship(back_populates="links")
    domain: Mapped[Domain] = relationship(back_populates="video_links")


class ViewSnapshot(Base):
    __tablename__ = "view_snapshots"
    __table_args__ = (
        UniqueConstraint("video_id", "capture_date", name="uq_video_snapshot_day"),
        Index("ix_view_snapshots_video_time", "video_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    capture_date: Mapped[date] = mapped_column(Date, default=lambda: utcnow().date())
    view_count: Mapped[int] = mapped_column(BigInteger)

    video: Mapped[Video] = relationship(back_populates="snapshots")


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), unique=True, index=True
    )
    tier: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    monthly_views: Mapped[int] = mapped_column(BigInteger, default=0)
    verified_30d: Mapped[bool] = mapped_column(Boolean, default=False)
    observation_days: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    video_count: Mapped[int] = mapped_column(Integer, default=0)
    link_count: Mapped[int] = mapped_column(Integer, default=0)
    best_video_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notified_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    domain: Mapped[Domain] = relationship(back_populates="candidate")


class SearchState(Base):
    __tablename__ = "search_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(Text, unique=True)
    page_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class YouTubeChannel(Base):
    __tablename__ = "youtube_channels"

    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    uploads_playlist_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    next_page_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    inventory_complete: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    seed_count: Mapped[int] = mapped_column(Integer, default=1)
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    videos_seen: Mapped[int] = mapped_column(Integer, default=0)
    videos_indexed: Mapped[int] = mapped_column(Integer, default=0)
    videos_with_external_links: Mapped[int] = mapped_column(Integer, default=0)
    external_links_found: Mapped[int] = mapped_column(Integer, default=0)
    yield_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seeded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class VideoRefreshState(Base):
    __tablename__ = "video_refresh_states"

    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    next_refresh_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    last_view_count: Mapped[int] = mapped_column(BigInteger, default=0)
    consecutive_low_growth: Mapped[int] = mapped_column(Integer, default=0)


class YouTubeQuotaLedger(Base):
    """Database-enforced daily quota guard shared by every YouTube job."""

    __tablename__ = "youtube_quota_ledgers"

    quota_date: Mapped[date] = mapped_column(Date, primary_key=True)
    search_calls: Mapped[int] = mapped_column(Integer, default=0)
    data_units: Mapped[int] = mapped_column(Integer, default=0)
    fanout_data_units: Mapped[int] = mapped_column(Integer, default=0)
    stats_units: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class YouTubeChannelIntelligence(Base):
    """Adaptive crawl allocation kept beside the permanent channel ledger."""

    __tablename__ = "youtube_channel_intelligence"
    __table_args__ = (
        Index("ix_youtube_channel_intel_queue", "tier", "next_crawl_at", "ema_yield"),
    )

    channel_id: Mapped[str] = mapped_column(
        ForeignKey("youtube_channels.channel_id", ondelete="CASCADE"), primary_key=True
    )
    tier: Mapped[str] = mapped_column(String(16), default="unrated", index=True)
    ema_yield: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    marginal_yield: Mapped[float] = mapped_column(Float, default=0.0)
    expected_links_per_page: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_empty_pages: Mapped[int] = mapped_column(Integer, default=0)
    pages_without_new_video: Mapped[int] = mapped_column(Integer, default=0)
    recommended_burst: Mapped[int] = mapped_column(Integer, default=1)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    next_crawl_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class YouTubeDomainSignal(Base):
    """Permanent acquisition-facing aggregate of exact YouTube link evidence."""

    __tablename__ = "youtube_domain_signals"

    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), primary_key=True
    )
    active_video_count: Mapped[int] = mapped_column(Integer, default=0)
    active_link_count: Mapped[int] = mapped_column(Integer, default=0)
    channel_count: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_linked_video_views: Mapped[int] = mapped_column(BigInteger, default=0)
    monthly_linked_video_exposure: Mapped[int] = mapped_column(BigInteger, default=0)
    observation_days: Mapped[float] = mapped_column(Float, default=0.0)
    traffic_confidence: Mapped[str] = mapped_column(String(24), default="collecting", index=True)
    measured_15d: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    verified_30d: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cta_rate: Mapped[float] = mapped_column(Float, default=0.0)
    clickable_rate: Mapped[float] = mapped_column(Float, default=0.0)
    expected_clicks_monthly: Mapped[int] = mapped_column(BigInteger, default=0)
    monthly_revenue_low_usd: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_revenue_high_usd: Mapped[float] = mapped_column(Float, default=0.0)
    max_purchase_price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    buy_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    monetization_route: Mapped[str] = mapped_column(String(64), default="content_restore")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DroppedDomain(Base):
    __tablename__ = "dropped_domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source: Mapped[str] = mapped_column(Text, default="manual")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    youtube_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    matched_existing_index: Mapped[bool] = mapped_column(Boolean, default=False)


class DroppedDomainMatch(Base):
    """Permanent proof that a dropped name matched the local YouTube index."""

    __tablename__ = "dropped_domain_matches"
    __table_args__ = (
        UniqueConstraint("dropped_domain_id", "domain_id", name="uq_dropped_domain_match"),
        Index("ix_dropped_domain_matches_refreshed", "refreshed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dropped_domain_id: Mapped[int] = mapped_column(
        ForeignKey("dropped_domains.id", ondelete="CASCADE"), index=True
    )
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True
    )
    active_video_count: Mapped[int] = mapped_column(Integer, default=0)
    active_link_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunLog(Base):
    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppCheckpoint(Base):
    __tablename__ = "app_checkpoints"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DashboardDecision(Base):
    __tablename__ = "dashboard_decisions"
    __table_args__ = (
        UniqueConstraint("system", "domain_id", name="uq_dashboard_decision_system_domain"),
        Index("ix_dashboard_decision_system_status", "system", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(16), index=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Web-wide Expandosaurus Link Hunter ------------------------------------
# These tables intentionally sit beside the YouTube-specific tables. They let
# the dashboard keep the two acquisition routes visually separate while both
# routes can ultimately rank the same Domain records.


class SourceSite(Base):
    __tablename__ = "source_sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="web", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourcePage(Base):
    __tablename__ = "source_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("source_sites.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    domain_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceLink(Base):
    __tablename__ = "source_links"
    __table_args__ = (
        UniqueConstraint("source_page_id", "domain_id", "target_url", name="uq_source_link_target"),
        Index("ix_source_links_domain_page", "domain_id", "source_page_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_page_id: Mapped[int] = mapped_column(
        ForeignKey("source_pages.id", ondelete="CASCADE"), index=True
    )
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    target_url: Mapped[str] = mapped_column(Text)
    anchor_text: Mapped[str] = mapped_column(Text, default="")
    context_before: Mapped[str] = mapped_column(Text, default="")
    context_after: Mapped[str] = mapped_column(Text, default="")
    semantic_location: Mapped[str] = mapped_column(String(64), default="")
    dofollow: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_live: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    provider_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    spam_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceMetricSnapshot(Base):
    __tablename__ = "source_metric_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source_page_id", "provider", "capture_date", name="uq_source_metric_day"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_page_id: Mapped[int] = mapped_column(
        ForeignKey("source_pages.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="dataforseo", index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    capture_date: Mapped[date] = mapped_column(Date, default=lambda: utcnow().date())
    organic_traffic_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    domain_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    referring_domains: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProviderQuery(Base):
    __tablename__ = "provider_queries"
    __table_args__ = (
        Index("ix_provider_queries_target_endpoint", "target", "endpoint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), default="dataforseo", index=True)
    endpoint: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(255), index=True)
    provider_task_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProviderDailyBudget(Base):
    __tablename__ = "provider_daily_budgets"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    spend_date: Mapped[date] = mapped_column(Date, primary_key=True)
    spent_microusd: Mapped[int] = mapped_column(Integer, default=0)
    reserved_microusd: Mapped[int] = mapped_column(Integer, default=0)
    completed_runs: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FetchVerification(Base):
    __tablename__ = "fetch_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_link_id: Mapped[int] = mapped_column(
        ForeignKey("source_links.id", ondelete="CASCADE"), unique=True, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str] = mapped_column(Text, default="")
    link_present: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), unique=True, index=True
    )
    tier: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    best_source_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_pages.id", ondelete="SET NULL"), nullable=True
    )
    source_page_traffic_estimate: Mapped[int] = mapped_column(Integer, default=0)
    referring_page_count: Mapped[int] = mapped_column(Integer, default=0)
    independent_site_count: Mapped[int] = mapped_column(Integer, default=0)
    link_strength: Mapped[float] = mapped_column(Float, default=0.0)
    commercial_intent: Mapped[float] = mapped_column(Float, default=0.0)
    verified_live_link: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    niche: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebScreening(Base):
    """Permanent zero-cost screening result for an ingested dropped name."""

    __tablename__ = "web_screenings"
    __table_args__ = (
        UniqueConstraint("dropped_domain_id", name="uq_web_screening_drop"),
        Index("ix_web_screenings_status_quality", "status", "quality_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dropped_domain_id: Mapped[int] = mapped_column(
        ForeignKey("dropped_domains.id", ondelete="CASCADE"), unique=True, index=True
    )
    domain_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(24), default="eligible", index=True)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    risk_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    monetization_hint: Mapped[str] = mapped_column(String(64), default="content_restore")
    signals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    screened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BacklinkSummary(Base):
    """Reusable aggregate evidence for every paid bulk-summary target."""

    __tablename__ = "backlink_summaries"
    __table_args__ = (
        UniqueConstraint("domain_id", "provider", name="uq_backlink_summary_provider"),
        Index("ix_backlink_summaries_rank_pages", "rank", "referring_pages"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="dataforseo", index=True)
    backlinks: Mapped[int] = mapped_column(Integer, default=0)
    referring_pages: Mapped[int] = mapped_column(Integer, default=0)
    referring_domains: Mapped[int] = mapped_column(Integer, default=0)
    referring_main_domains: Mapped[int] = mapped_column(Integer, default=0)
    rank: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    raw_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class LinkObservation(Base):
    """Append-only direct observations used to measure link survival."""

    __tablename__ = "link_observations"
    __table_args__ = (
        Index("ix_link_observations_link_time", "source_link_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_link_id: Mapped[int] = mapped_column(
        ForeignKey("source_links.id", ondelete="CASCADE"), index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str] = mapped_column(Text, default="")
    link_present: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    clickable: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    clickability_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    semantic_location: Mapped[str] = mapped_column(String(32), default="unknown")
    anchor_text: Mapped[str] = mapped_column(Text, default="")
    nofollow: Mapped[bool] = mapped_column(Boolean, default=False)
    survival_days: Mapped[float] = mapped_column(Float, default=0.0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class OpportunityEconomics(Base):
    """Conservative acquisition and monetisation case; never an auto-buy instruction."""

    __tablename__ = "opportunity_economics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), unique=True, index=True
    )
    buy_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    expected_clicks_monthly: Mapped[int] = mapped_column(Integer, default=0)
    monthly_revenue_low_usd: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_revenue_high_usd: Mapped[float] = mapped_column(Float, default=0.0)
    max_purchase_price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_payback_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    monetization_route: Mapped[str] = mapped_column(String(64), default="content_restore")
    rationale: Mapped[list[str]] = mapped_column(JSON, default=list)
    safety_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
