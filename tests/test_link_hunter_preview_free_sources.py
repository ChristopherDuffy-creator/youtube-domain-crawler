from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.link_hunter_preview import build_provider_proof_preview
from app.models import (
    Candidate,
    Domain,
    DroppedDomain,
    FetchVerification,
    ProviderQuery,
    SourceLink,
    SourcePage,
    SourceSite,
)


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
    assert preview["selection_strategy"] == "free_preproof_score"
    assert preview["target_free_scores"]["exact.example"] > preview["target_free_scores"][
        "historical.example"
    ]
    assert preview["free_exact_link_targets"] == ["exact.example"]
    assert preview["target_free_exact_links"] == {
        "exact.example": 1,
        "historical.example": 0,
        "newest.example": 0,
    }


def test_verified_links_and_independent_sites_drive_free_ranking() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    settings = Settings(link_hunter_proof_batch_size=2)

    with Session(engine) as db:
        strong = Domain(name="strong.example", availability_status="likely_available")
        db.add(strong)
        db.flush()
        for index, hostname in enumerate(("one.example.org", "two.example.org", "three.example.org")):
            site = SourceSite(hostname=hostname, source_type="web")
            db.add(site)
            db.flush()
            page = SourcePage(site_id=site.id, url=f"https://{hostname}/resource")
            db.add(page)
            db.flush()
            link = SourceLink(
                source_page_id=page.id,
                domain_id=strong.id,
                target_url="https://strong.example/useful",
                provider_live=True,
            )
            db.add(link)
            db.flush()
            if index == 0:
                db.add(FetchVerification(source_link_id=link.id, link_present=True, http_status=200))

        db.add_all(
            [
                DroppedDomain(name="newer.example", source="test", first_seen_at=now),
                DroppedDomain(
                    name="strong.example", source="test", first_seen_at=now - timedelta(days=2)
                ),
            ]
        )
        db.commit()
        preview = build_provider_proof_preview(db, settings)

    assert preview["targets"][0] == "strong.example"
    signals = preview["target_free_rank_signals"]["strong.example"]
    assert signals["verified_links"] == 1
    assert signals["independent_sites"] == 3
    assert signals["exact_links"] == 3
    assert preview["target_free_scores"]["strong.example"] > 0


def test_youtube_overlap_is_a_free_ranking_signal() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    settings = Settings(link_hunter_proof_batch_size=2)

    with Session(engine) as db:
        overlap = Domain(name="overlap.example")
        db.add(overlap)
        db.flush()
        db.add(
            Candidate(
                domain_id=overlap.id,
                monthly_views=100_000,
                video_count=2,
                link_count=3,
                score=80.0,
            )
        )
        db.add_all(
            [
                DroppedDomain(name="newer.example", source="test", first_seen_at=now),
                DroppedDomain(
                    name="overlap.example", source="test", first_seen_at=now - timedelta(days=3)
                ),
            ]
        )
        db.commit()
        preview = build_provider_proof_preview(db, settings)

    assert preview["targets"][0] == "overlap.example"
    assert preview["target_free_rank_signals"]["overlap.example"]["youtube_monthly_views"] == 100_000


def test_known_registered_or_premium_domains_are_not_paid_proof_targets() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    settings = Settings(link_hunter_proof_batch_size=3)

    with Session(engine) as db:
        db.add_all(
            [
                Domain(name="registered.example", availability_status="registered"),
                Domain(name="premium.example", availability_status="premium"),
                Domain(name="eligible.example", availability_status="likely_available"),
                DroppedDomain(name="registered.example", source="test", first_seen_at=now),
                DroppedDomain(
                    name="premium.example", source="test", first_seen_at=now - timedelta(minutes=1)
                ),
                DroppedDomain(
                    name="eligible.example", source="test", first_seen_at=now - timedelta(minutes=2)
                ),
            ]
        )
        db.commit()
        preview = build_provider_proof_preview(db, settings)

    assert preview["targets"] == ["eligible.example"]
    assert preview["known_unavailable_targets_skipped"] == 2
