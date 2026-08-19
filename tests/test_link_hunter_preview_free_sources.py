from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.link_hunter_preview import build_provider_proof_preview
from app.models import Domain, DroppedDomain, ProviderQuery, SourceLink, SourcePage, SourceSite


def test_exact_free_source_link_outranks_commoncrawl_history_and_recency() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    settings = Settings(link_hunter_proof_batch_size=3)

    with Session(engine) as db:
        exact = Domain(name="exact.example")
        site = SourceSite(hostname="stackoverflow.com", source_type="stackexchange")
        db.add_all([exact, site])
        db.flush()
        page = SourcePage(
            site_id=site.id,
            url="https://stackoverflow.com/questions/1/example",
            title="Example question",
        )
        db.add(page)
        db.flush()
        db.add(
            SourceLink(
                source_page_id=page.id,
                domain_id=exact.id,
                target_url="https://exact.example/guide",
                provider_live=True,
            )
        )
        db.add_all(
            [
                DroppedDomain(name="newest.example", source="test", first_seen_at=now),
                DroppedDomain(
                    name="historical.example", source="test", first_seen_at=now - timedelta(minutes=1)
                ),
                DroppedDomain(
                    name="exact.example", source="test", first_seen_at=now - timedelta(minutes=2)
                ),
                ProviderQuery(
                    provider="commoncrawl",
                    endpoint="url_index",
                    target="historical.example",
                    status="complete",
                    row_count=2,
                ),
            ]
        )
        db.commit()

        preview = build_provider_proof_preview(db, settings)

    assert preview["targets"] == ["exact.example", "historical.example", "newest.example"]
    assert preview["free_exact_link_targets"] == ["exact.example"]
    assert preview["target_free_exact_links"] == {
        "exact.example": 1,
        "historical.example": 0,
        "newest.example": 0,
    }
