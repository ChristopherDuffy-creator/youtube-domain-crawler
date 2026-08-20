from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import _load_web_evidence_rows
from app.models import (
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


def test_dashboard_row_includes_source_and_fetch_evidence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        domain = Domain(name="example.com", availability_status="available")
        site = SourceSite(hostname="publisher.test")
        db.add_all([domain, site])
        db.flush()

        page = SourcePage(site_id=site.id, url="https://publisher.test/article", title="Article")
        db.add(page)
        db.flush()

        link = SourceLink(
            source_page_id=page.id,
            domain_id=domain.id,
            target_url="https://example.com/offer",
            anchor_text="buy software",
            context_before="best deal",
            context_after="signup now",
            dofollow=True,
            provider_live=True,
            provider_rank=75,
        )
        opportunity = Opportunity(
            domain_id=domain.id,
            tier="qualified",
            score=72,
            best_source_page_id=page.id,
            source_page_traffic_estimate=10_000,
            referring_page_count=12,
            independent_site_count=3,
            link_strength=75,
            verified_live_link=True,
        )
        db.add_all([link, opportunity])
        db.flush()

        verification = FetchVerification(
            source_link_id=link.id,
            http_status=200,
            final_url=page.url,
            link_present=True,
        )
        economics = OpportunityEconomics(
            domain_id=domain.id,
            buy_score=72,
            expected_clicks_monthly=125,
            monthly_revenue_low_usd=25,
            monthly_revenue_high_usd=90,
            max_purchase_price_usd=50,
            confidence=0.8,
            monetization_route="affiliate_landing",
        )
        observation = LinkObservation(
            source_link_id=link.id,
            http_status=200,
            final_url=page.url,
            link_present=True,
            clickable=True,
            clickability_score=82,
            semantic_location="article",
            survival_days=12,
        )
        db.add_all([verification, economics, observation])
        db.commit()

        rows = _load_web_evidence_rows(db, limit=100)

        assert len(rows) == 1
        (
            row_opportunity,
            row_domain,
            row_page,
            row_site,
            row_link,
            row_verification,
            row_economics,
            row_observation,
        ) = rows[0]
        assert row_opportunity.id == opportunity.id
        assert row_domain.name == "example.com"
        assert row_page is not None and row_page.url == page.url
        assert row_site is not None and row_site.hostname == "publisher.test"
        assert row_link is not None and row_link.anchor_text == "buy software"
        assert row_verification is not None and row_verification.link_present is True
        assert row_economics is not None and row_economics.expected_clicks_monthly == 125
        assert row_observation is not None and row_observation.clickability_score == 82


def test_dashboard_web_rows_can_be_filtered_by_tier() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        qualified_domain = Domain(name="qualified.example")
        pending_domain = Domain(name="pending.example")
        db.add_all([qualified_domain, pending_domain])
        db.flush()
        db.add_all(
            [
                Opportunity(domain_id=qualified_domain.id, tier="qualified", score=70),
                Opportunity(domain_id=pending_domain.id, tier="pending", score=20),
            ]
        )
        db.commit()

        rows = _load_web_evidence_rows(db, tier="qualified")

        assert len(rows) == 1
        assert rows[0][0].tier == "qualified"
        assert rows[0][1].name == "qualified.example"


def test_dashboard_omits_locally_blocked_obvious_businesses() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        domain = Domain(name="envato.com", availability_status="unknown")
        drop = DroppedDomain(name="envato.com", source="test")
        db.add_all([domain, drop])
        db.flush()
        db.add(Opportunity(domain_id=domain.id, tier="pending", score=50))
        db.add(
            WebScreening(
                dropped_domain_id=drop.id,
                domain_name=drop.name,
                status="blocked",
                risk_score=95,
                risk_reasons=["obvious_protected_brand"],
            )
        )
        db.commit()

        assert _load_web_evidence_rows(db, limit=100) == []


def test_dashboard_web_rows_can_be_filtered_since_the_last_visit() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 18, 0, tzinfo=UTC)

    with Session(engine) as db:
        new_domain = Domain(name="new.example")
        old_domain = Domain(name="old.example")
        db.add_all([new_domain, old_domain])
        db.flush()
        db.add_all(
            [
                Opportunity(
                    domain_id=new_domain.id,
                    tier="pending",
                    updated_at=now - timedelta(minutes=5),
                ),
                Opportunity(
                    domain_id=old_domain.id,
                    tier="pending",
                    updated_at=now - timedelta(days=2),
                ),
            ]
        )
        db.commit()

        rows = _load_web_evidence_rows(
            db,
            tier="new",
            new_since=now - timedelta(hours=1),
        )

        assert [row[1].name for row in rows] == ["new.example"]
