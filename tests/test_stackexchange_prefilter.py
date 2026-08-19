from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Domain, DroppedDomain, ProviderQuery, SourceLink, SourceMetricSnapshot, SourcePage
from app.stackexchange import StackExchangeSearchResult
from app.stackexchange_prefilter import run_stackexchange_prefilter


class FakeStackExchangeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def search_url(
        self,
        *,
        site: str,
        domain: str,
        min_views: int = 1_000,
        page_size: int = 20,
    ) -> StackExchangeSearchResult:
        self.calls.append((site, domain, min_views, page_size))
        return StackExchangeSearchResult(
            items=(
                {
                    "question_id": 123,
                    "link": "https://stackoverflow.com/questions/123/example-question",
                    "title": "How to configure Example Tool",
                    "body": (
                        '<p>Use <a href="https://example.com/guide" rel="nofollow">Example Tool</a>. '
                        '<a href="https://notexample.com/">Not the target</a></p>'
                    ),
                    "view_count": 150_000,
                    "score": 42,
                    "answer_count": 6,
                    "is_answered": True,
                    "creation_date": 1_600_000_000,
                    "tags": ["configuration", "software"],
                },
            ),
            quota_remaining=9876,
            backoff_seconds=0,
        )


def test_prefilter_saves_only_exact_domain_links_and_caches_query() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake = FakeStackExchangeClient()

    with Session(engine) as db:
        db.add(DroppedDomain(name="example.com", source="test"))
        db.commit()

        counters = run_stackexchange_prefilter(
            db,
            batch_size=1,
            sites=("stackoverflow",),
            min_views=1_000,
            client=fake,
        )
        domain = db.scalar(select(Domain).where(Domain.name == "example.com"))
        link = db.scalar(select(SourceLink))
        page = db.scalar(select(SourcePage))
        metric = db.scalar(select(SourceMetricSnapshot))
        provider = db.scalar(select(ProviderQuery).where(ProviderQuery.provider == "stackexchange"))

        second = run_stackexchange_prefilter(
            db,
            batch_size=1,
            sites=("stackoverflow",),
            min_views=1_000,
            client=fake,
        )

    assert fake.calls == [("stackoverflow", "example.com", 1_000, 20)]
    assert counters["queries"] == 1
    assert counters["questions_matched"] == 1
    assert counters["exact_links_saved"] == 1
    assert counters["new_links"] == 1
    assert counters["domains_with_links"] == 1
    assert counters["provider_cost_usd"] == 0.0
    assert counters["quota_remaining"] == 9876
    assert second["candidates"] == 0

    assert domain is not None
    assert link is not None
    assert link.domain_id == domain.id
    assert link.target_url == "https://example.com/guide"
    assert link.anchor_text == "Example Tool"
    assert link.context_before == "How to configure Example Tool"
    assert link.dofollow is False
    assert link.provider_live is True
    assert page is not None
    assert page.url == "https://stackoverflow.com/questions/123/example-question"
    assert metric is not None
    assert metric.provider == "stackexchange"
    assert metric.raw_metrics["view_count"] == 150_000
    assert provider is not None
    assert provider.endpoint == "url_search:stackoverflow"
    assert provider.row_count == 1
    assert provider.cost_usd == 0.0
    assert provider.status == "complete"
