from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.hackernews import HackerNewsSearchResult
from app.hackernews_prefilter import run_hackernews_prefilter
from app.models import Domain, DroppedDomain, ProviderQuery, SourceLink, SourceMetricSnapshot, SourcePage


class FakeHNClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search_domain(self, domain: str, *, hits_per_page: int = 50) -> HackerNewsSearchResult:
        self.calls.append((domain, hits_per_page))
        return HackerNewsSearchResult(
            hits=(
                {
                    "objectID": "100",
                    "title": "Launch: Example Tool",
                    "url": "https://example.com/launch",
                    "story_text": None,
                    "comment_text": None,
                    "points": 80,
                    "num_comments": 24,
                    "created_at_i": 1_600_000_000,
                    "_tags": ["story"],
                },
                {
                    "objectID": "101",
                    "story_id": 100,
                    "story_title": "Launch: Example Tool",
                    "comment_text": (
                        '<p>Docs: <a href="https://docs.example.com/guide" rel="nofollow">guide</a>. '
                        '<a href="https://notexample.com/">lookalike</a></p>'
                    ),
                    "points": 5,
                    "num_comments": 0,
                    "created_at_i": 1_600_000_100,
                    "_tags": ["comment"],
                },
            ),
            total_hits=2,
        )


def test_hn_prefilter_saves_only_exact_target_hosts_and_is_cached() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake = FakeHNClient()

    with Session(engine) as db:
        db.add(DroppedDomain(name="example.com", source="test"))
        db.commit()

        counters = run_hackernews_prefilter(db, batch_size=1, client=fake)
        domain = db.scalar(select(Domain).where(Domain.name == "example.com"))
        links = db.scalars(select(SourceLink).order_by(SourceLink.target_url)).all()
        pages = db.scalars(select(SourcePage).order_by(SourcePage.url)).all()
        metrics = db.scalars(select(SourceMetricSnapshot).order_by(SourceMetricSnapshot.id)).all()
        provider = db.scalar(select(ProviderQuery).where(ProviderQuery.provider == "hackernews"))

        saved_values = {
            "domain_id": domain.id if domain else None,
            "targets": [link.target_url for link in links],
            "dofollow": {link.target_url: link.dofollow for link in links},
            "ranks": {link.target_url: link.provider_rank for link in links},
            "pages": [page.url for page in pages],
            "metric_providers": [metric.provider for metric in metrics],
            "metric_points": [metric.raw_metrics["points"] for metric in metrics],
            "provider_endpoint": provider.endpoint if provider else None,
            "provider_rows": provider.row_count if provider else None,
            "provider_cost": provider.cost_usd if provider else None,
            "provider_status": provider.status if provider else None,
        }
        second = run_hackernews_prefilter(db, batch_size=1, client=fake)

    assert fake.calls == [("example.com", 50)]
    assert counters["queries"] == 1
    assert counters["search_hits"] == 2
    assert counters["items_with_exact_links"] == 2
    assert counters["exact_links_saved"] == 2
    assert counters["new_links"] == 2
    assert counters["domains_with_links"] == 1
    assert counters["provider_cost_usd"] == 0.0
    assert second["candidates"] == 0

    assert saved_values["domain_id"] is not None
    assert saved_values["targets"] == [
        "https://docs.example.com/guide",
        "https://example.com/launch",
    ]
    assert "https://notexample.com/" not in saved_values["targets"]
    assert saved_values["dofollow"]["https://docs.example.com/guide"] is False
    assert saved_values["ranks"]["https://example.com/launch"] > 0
    assert saved_values["pages"] == [
        "https://news.ycombinator.com/item?id=100",
        "https://news.ycombinator.com/item?id=101",
    ]
    assert saved_values["metric_providers"] == ["hackernews", "hackernews"]
    assert saved_values["metric_points"] == [80, 5]
    assert saved_values["provider_endpoint"] == "domain_search"
    assert saved_values["provider_rows"] == 2
    assert saved_values["provider_cost"] == 0.0
    assert saved_values["provider_status"] == "complete"
