from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import _load_web_evidence_rows
from app.models import (
    Domain,
    FetchVerification,
    Opportunity,
    SourceLink,
    SourcePage,
    SourceSite,
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
        db.add(verification)
        db.commit()

        rows = _load_web_evidence_rows(db, limit=100)

        assert len(rows) == 1
        row_opportunity, row_domain, row_page, row_site, row_link, row_verification = rows[0]
        assert row_opportunity.id == opportunity.id
        assert row_domain.name == "example.com"
        assert row_page is not None and row_page.url == page.url
        assert row_site is not None and row_site.hostname == "publisher.test"
        assert row_link is not None and row_link.anchor_text == "buy software"
        assert row_verification is not None and row_verification.link_present is True
