from datetime import UTC, datetime, timedelta
from threading import Barrier

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import link_hunter
from app.config import Settings
from app.database import Base
from app.jobs import (
    JOB_FUNCTIONS,
    build_scheduler,
    run_web_free_screening_job,
    run_web_link_refresh_job,
)
from app.link_hunter_preview import select_provider_summary_targets
from app.models import (
    BacklinkSummary,
    Domain,
    DroppedDomain,
    FetchVerification,
    LinkObservation,
    Opportunity,
    OpportunityEconomics,
    SourceLink,
    SourcePage,
    SourceSite,
    WebScreening,
)
from app.web_intelligence import (
    EconomicProjection,
    assess_dropped_domain,
    backfill_existing_web_intelligence,
    project_opportunity_economics,
    save_opportunity_economics,
    screen_dropped_domains,
)


def test_free_screen_blocks_obvious_brands_and_keeps_commercial_names() -> None:
    brand = assess_dropped_domain("envato.com")
    commercial = assess_dropped_domain("home-repair-deals.com")
    registered = assess_dropped_domain("excellentcourse.com", "registered")

    assert brand.status == "blocked"
    assert "obvious_protected_brand" in brand.risk_reasons
    assert commercial.status == "eligible"
    assert commercial.monetization_hint == "affiliate_landing"
    assert registered.status == "blocked"


def test_free_screening_is_scheduled_and_manually_addressable() -> None:
    scheduler = build_scheduler(Settings())
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert "web_free_screening" in job_ids
    assert "web_link_refresh" in job_ids
    assert JOB_FUNCTIONS["web_free_screening"] is run_web_free_screening_job
    assert JOB_FUNCTIONS["web_link_refresh"] is run_web_link_refresh_job


def test_free_screening_is_permanent_and_resumes_after_last_processed_drop() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                DroppedDomain(name="envato.com", source="test"),
                DroppedDomain(name="excellentcourse.com", source="test"),
                DroppedDomain(name="repair-service.com", source="test"),
            ]
        )
        db.commit()

        first = screen_dropped_domains(db, batch_size=2)
        second = screen_dropped_domains(db, batch_size=2)
        third = screen_dropped_domains(db, batch_size=2)

        assert first["screened"] == 2
        assert second["screened"] == 1
        assert third["screened"] == 0
        assert len(db.scalars(select(WebScreening)).all()) == 3


def test_paid_summary_selector_uses_free_quality_and_omits_blocked_brands() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                DroppedDomain(name="envato.com", source="test"),
                DroppedDomain(name="long-name-12345.xyz", source="test"),
                DroppedDomain(name="excellentcourse.com", source="test"),
            ]
        )
        db.commit()
        screen_dropped_domains(db, batch_size=10)

        targets = select_provider_summary_targets(
            db,
            Settings(link_hunter_summary_batch_size=2),
        )

        assert targets[0] == "excellentcourse.com"
        assert "envato.com" not in targets


def test_profit_projection_rewards_verified_commercial_evidence() -> None:
    domain = Domain(
        id=1,
        name="usefulsoftware.com",
        availability_status="available",
        registrar_price_usd=12,
    )
    opportunity = Opportunity(
        domain_id=1,
        independent_site_count=8,
        referring_page_count=30,
        commercial_intent=1.0,
        niche="software",
    )
    links = [
        SourceLink(
            source_page_id=1,
            domain_id=1,
            target_url="https://usefulsoftware.com/download",
            anchor_text="Download the software",
            semantic_location="article",
            provider_rank=80,
            spam_score=0,
        )
    ]

    projection = project_opportunity_economics(
        opportunity,
        domain,
        links,
        traffic=50_000,
        verified=True,
        evidence_score=85,
        clickability_score=90,
    )

    assert projection.expected_clicks_monthly >= 500
    assert projection.monthly_revenue_low_usd > 0
    assert projection.monthly_revenue_high_usd > projection.monthly_revenue_low_usd
    assert projection.max_purchase_price_usd <= 500
    assert projection.estimated_payback_months is not None
    assert projection.monetization_route == "affiliate_landing"
    assert "not_an_ordinary_registration" not in projection.safety_flags


def test_profit_projection_applies_traffic_first_gates_without_bootstrap_patch() -> None:
    domain = Domain(id=1, name="usefulsoftware.com", availability_status="available")
    opportunity = Opportunity(
        domain_id=1,
        independent_site_count=8,
        commercial_intent=1.0,
        niche="software",
    )
    links = [
        SourceLink(
            source_page_id=1,
            domain_id=1,
            target_url="https://usefulsoftware.com/download",
            anchor_text="Download the software",
            semantic_location="article",
            spam_score=0,
        )
    ]

    unverified = project_opportunity_economics(
        opportunity,
        domain,
        links,
        traffic=50_000,
        verified=False,
        evidence_score=100,
        clickability_score=100,
    )
    no_traffic = project_opportunity_economics(
        opportunity,
        domain,
        links,
        traffic=0,
        verified=True,
        evidence_score=100,
        clickability_score=100,
    )

    assert unverified.buy_score <= 39.9
    assert no_traffic.buy_score <= 24.9


def test_historical_web_evidence_is_backfilled_without_provider_calls() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        domain = Domain(name="historicaltool.com", availability_status="available")
        db.add(domain)
        db.flush()
        db.add(
            Opportunity(
                domain_id=domain.id,
                score=72,
                referring_page_count=18,
                independent_site_count=7,
                source_page_traffic_estimate=5_000,
                commercial_intent=0.8,
                verified_live_link=True,
                niche="software",
            )
        )
        db.commit()

        counters = backfill_existing_web_intelligence(db)

        assert counters == {"summaries_backfilled": 1, "money_cases_backfilled": 1}
        assert db.scalar(select(BacklinkSummary)).referring_pages == 18
        assert db.scalar(select(OpportunityEconomics)).monthly_revenue_high_usd > 0
        assert backfill_existing_web_intelligence(db) == {
            "summaries_backfilled": 0,
            "money_cases_backfilled": 0,
        }


def test_direct_verification_records_clickability_and_survival(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    html = (
        '<main><p>Recommended resource</p><a href="https://example.com/offer">'
        "Buy the software now</a></main>"
    )
    calls = 0

    def fake_fetch(url: str, timeout: float) -> tuple[int, str, bytes, str]:
        nonlocal calls
        calls += 1
        return 200, url, html.encode(), html

    monkeypatch.setattr(link_hunter, "_fetch_public_page", fake_fetch)

    with Session(engine) as db:
        domain = Domain(name="example.com")
        site = SourceSite(hostname="publisher.example")
        db.add_all([domain, site])
        db.flush()
        page = SourcePage(site_id=site.id, url="https://publisher.example/article")
        db.add(page)
        db.flush()
        link = SourceLink(
            source_page_id=page.id,
            domain_id=domain.id,
            target_url="https://example.com/offer",
            first_seen_at=datetime.now(UTC) - timedelta(days=10),
        )
        db.add(link)
        db.commit()

        assert link_hunter._verify_source_link(db, link, domain.name, 5, cache_hours=24)
        assert link_hunter._verify_source_link(db, link, domain.name, 5, cache_hours=24)

        observations = db.scalars(select(LinkObservation)).all()
        assert calls == 1
        assert len(observations) == 1
        assert observations[0].clickable is True
        assert observations[0].clickability_score >= 80
        assert observations[0].semantic_location == "main"
        assert observations[0].survival_days >= 9.9


def test_due_link_refresh_extends_survival_history(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    html = '<article><a href="https://example.com/offer">Visit offer</a></article>'
    monkeypatch.setattr(
        link_hunter,
        "_fetch_public_page",
        lambda url, timeout: (200, url, html.encode(), html),
    )
    settings = Settings(link_hunter_verification_cache_hours=24)

    with Session(engine) as db:
        domain = Domain(name="example.com", availability_status="available")
        site = SourceSite(hostname="publisher.example")
        db.add_all([domain, site])
        db.flush()
        page = SourcePage(site_id=site.id, url="https://publisher.example/article")
        db.add(page)
        db.flush()
        link = SourceLink(
            source_page_id=page.id,
            domain_id=domain.id,
            target_url="https://example.com/offer",
            first_seen_at=datetime.now(UTC) - timedelta(days=15),
        )
        db.add(link)
        db.flush()
        db.add(
            Opportunity(
                domain_id=domain.id,
                best_source_page_id=page.id,
                source_page_traffic_estimate=2_000,
                commercial_intent=0.5,
            )
        )
        db.add(
            FetchVerification(
                source_link_id=link.id,
                fetched_at=datetime.now(UTC) - timedelta(days=2),
                link_present=True,
            )
        )
        db.commit()

        counters = link_hunter.refresh_web_link_observations(db, settings, batch_size=10)

        assert counters == {
            "due": 1,
            "refreshed": 1,
            "verified": 1,
            "missing": 0,
            "errors": 0,
        }
        observations = db.scalars(select(LinkObservation)).all()
        assert len(observations) == 1
        assert observations[0].survival_days >= 14.9
        assert db.scalar(select(OpportunityEconomics)) is not None


def test_due_link_refresh_fetches_multiple_pages_concurrently(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    barrier = Barrier(3)
    calls: list[str] = []

    def fake_fetch(url: str, timeout: float) -> tuple[int, str, bytes, str]:
        calls.append(url)
        barrier.wait(timeout=1)
        suffix = url.rsplit("/", 1)[-1]
        html = f'<article><a href="https://example{suffix}.com/offer">Visit offer</a></article>'
        return 200, url, html.encode(), html

    monkeypatch.setattr(link_hunter, "_fetch_public_page", fake_fetch)
    settings = Settings(
        link_hunter_verification_cache_hours=24,
        link_hunter_link_refresh_workers=3,
    )

    with Session(engine) as db:
        for number in range(1, 4):
            domain = Domain(name=f"example{number}.com", availability_status="available")
            site = SourceSite(hostname=f"publisher{number}.example")
            db.add_all([domain, site])
            db.flush()
            page = SourcePage(site_id=site.id, url=f"https://publisher.example/{number}")
            db.add(page)
            db.flush()
            link = SourceLink(
                source_page_id=page.id,
                domain_id=domain.id,
                target_url=f"https://example{number}.com/offer",
                first_seen_at=datetime.now(UTC) - timedelta(days=15),
            )
            db.add(link)
            db.flush()
            db.add(
                Opportunity(
                    domain_id=domain.id,
                    best_source_page_id=page.id,
                    source_page_traffic_estimate=2_000,
                    commercial_intent=0.5,
                )
            )
            db.add(
                FetchVerification(
                    source_link_id=link.id,
                    fetched_at=datetime.now(UTC) - timedelta(days=2),
                    link_present=True,
                )
            )
        db.commit()

        counters = link_hunter.refresh_web_link_observations(db, settings, batch_size=10)

        assert counters == {
            "due": 3,
            "refreshed": 3,
            "verified": 3,
            "missing": 0,
            "errors": 0,
        }
        assert len(calls) == 3
        assert len(db.scalars(select(LinkObservation)).all()) == 3


def test_opportunity_economics_upsert_keeps_one_row_per_domain() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    first_projection = EconomicProjection(
        buy_score=44.0,
        expected_clicks_monthly=12,
        monthly_revenue_low_usd=3.0,
        monthly_revenue_high_usd=9.0,
        max_purchase_price_usd=20.0,
        estimated_payback_months=3.0,
        confidence=0.4,
        risk_score=18.0,
        monetization_route="content_restore",
        rationale=["initial"],
        safety_flags=[],
    )
    updated_projection = EconomicProjection(
        buy_score=77.0,
        expected_clicks_monthly=80,
        monthly_revenue_low_usd=20.0,
        monthly_revenue_high_usd=60.0,
        max_purchase_price_usd=100.0,
        estimated_payback_months=2.0,
        confidence=0.8,
        risk_score=8.0,
        monetization_route="affiliate_landing",
        rationale=["updated"],
        safety_flags=[],
    )

    with Session(engine) as db:
        domain = Domain(name="upsert-safe.example")
        db.add(domain)
        db.commit()

        save_opportunity_economics(db, domain, first_projection)
        db.flush()
        save_opportunity_economics(db, domain, updated_projection)
        db.commit()

        rows = db.scalars(
            select(OpportunityEconomics).where(OpportunityEconomics.domain_id == domain.id)
        ).all()

    assert len(rows) == 1
    assert rows[0].buy_score == 77.0
    assert rows[0].monthly_revenue_high_usd == 60.0
    assert rows[0].monetization_route == "affiliate_landing"
