from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import link_hunter
from app.availability import AvailabilityResult
from app.config import Settings
from app.dataforseo import DataForSEOResponse
from app.database import Base
from app.models import (
    Domain,
    DroppedDomain,
    Opportunity,
    ProviderQuery,
    SourceMetricSnapshot,
)


class FakeDataForSEOClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def bulk_backlink_summaries(self, targets: list[str]) -> DataForSEOResponse:
        assert targets == ["example.com"]
        return DataForSEOResponse(
            result={
                "items_count": 1,
                "items": [
                    {
                        "url": "example.com",
                        "referring_pages": 12,
                        "referring_domains": 4,
                        "referring_main_domains": 3,
                    }
                ],
            },
            task_cost_usd=0.02,
            task_id="summary-task",
        )

    def backlinks(self, target: str, limit: int = 25) -> DataForSEOResponse:
        assert target == "example.com"
        assert limit == 25
        return DataForSEOResponse(
            result={
                "items_count": 1,
                "items": [
                    {
                        "domain_from": "publisher.test",
                        "url_from": "https://publisher.test/article",
                        "url_to": "https://example.com/landing",
                        "page_from_title": "Useful commercial article",
                        "page_from_language": "en",
                        "page_from_status_code": 200,
                        "page_from_rank": 70,
                        "domain_from_rank": 75,
                        "anchor": "buy software",
                        "text_pre": "best deal",
                        "text_post": "signup tool",
                        "semantic_location": "article",
                        "dofollow": True,
                        "is_lost": False,
                        "rank": 80,
                        "backlink_spam_score": 0,
                    }
                ],
            },
            task_cost_usd=0.03,
            task_id="backlinks-task",
        )

    def bulk_traffic_estimation(self, targets: list[str]) -> DataForSEOResponse:
        assert targets == ["https://publisher.test/article"]
        return DataForSEOResponse(
            result={
                "items_count": 1,
                "items": [
                    {
                        "target": "https://publisher.test/article",
                        "metrics": {"organic": {"etv": 10_000.0, "count": 250}},
                    }
                ],
            },
            task_cost_usd=0.01,
            task_id="traffic-task",
        )


def test_provider_proof_builds_ranked_web_opportunity(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        dataforseo_login="login",
        dataforseo_password="password",
        link_hunter_enabled=True,
        link_hunter_summary_batch_size=1,
        link_hunter_proof_batch_size=1,
        link_hunter_backlinks_per_domain=25,
        link_hunter_proof_max_cost_usd=1.0,
        porkbun_api_key="porkbun-key",
        porkbun_secret_api_key="porkbun-secret",
    )

    availability_calls: list[bool] = []

    def fake_check_domain(
        domain: str,
        _: Settings,
        exact_registrar_check: bool = False,
    ) -> AvailabilityResult:
        assert domain == "example.com"
        availability_calls.append(exact_registrar_check)
        if exact_registrar_check:
            return AvailabilityResult(
                status="available",
                source="porkbun",
                rdap_status="unchecked",
                dns_status="unchecked",
                price_usd=12.0,
                premium=False,
            )
        return AvailabilityResult(
            status="likely_available",
            source="rdap_dns",
            rdap_status="not_found",
            dns_status="nxdomain",
        )

    monkeypatch.setattr(link_hunter, "DataForSEOClient", FakeDataForSEOClient)
    monkeypatch.setattr(link_hunter, "check_domain", fake_check_domain)
    monkeypatch.setattr(link_hunter, "_verify_source_link", lambda *args, **kwargs: True)

    with Session(engine) as db:
        db.add(DroppedDomain(name="example.com", source="test"))
        db.commit()

        counters = link_hunter.run_provider_proof(db, settings)

        assert counters["targets"] == 1
        assert counters["summary_screened"] == 1
        assert counters["deep_proof_target_count"] == 1
        assert counters["deep_proof_targets"] == ["example.com"]
        assert counters["summary_calls"] == 1
        assert counters["backlink_calls"] == 1
        assert counters["traffic_calls"] == 1
        assert counters["links_saved"] == 1
        assert counters["source_pages_traffic_checked"] == 1
        assert counters["source_links_verified"] == 1
        assert counters["registrar_checks"] == 1
        assert counters["errors"] == 0
        assert counters["provider_cost_usd"] == 0.06
        assert availability_calls == [False, True]

        domain = db.scalar(select(Domain).where(Domain.name == "example.com"))
        assert domain is not None
        assert domain.availability_status == "available"
        assert domain.registrar_price_usd == 12.0

        opportunity = db.scalar(select(Opportunity).where(Opportunity.domain_id == domain.id))
        assert opportunity is not None
        assert opportunity.tier == "qualified"
        assert 65 <= opportunity.score < 80
        assert opportunity.verified_live_link is True
        assert opportunity.source_page_traffic_estimate == 10_000
        assert opportunity.referring_page_count == 12
        assert opportunity.independent_site_count == 3
        assert opportunity.niche == "software"

        snapshot = db.scalar(select(SourceMetricSnapshot))
        assert snapshot is not None
        assert snapshot.organic_traffic_estimate == 10_000

        provider_queries = db.scalars(select(ProviderQuery)).all()
        assert len(provider_queries) == 3
        assert {query.status for query in provider_queries} == {"complete"}


def test_summary_screen_reranks_100_and_allows_only_five_deep_calls(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        dataforseo_login="login",
        dataforseo_password="password",
        link_hunter_enabled=True,
        link_hunter_summary_batch_size=100,
        link_hunter_proof_batch_size=5,
        link_hunter_backlinks_per_domain=25,
        link_hunter_proof_max_cost_usd=0.18,
    )

    class FunnelClient:
        summary_targets: list[str] = []
        detailed_targets: list[str] = []

        def __init__(self, _: Settings):
            pass

        def bulk_backlink_summaries(self, targets: list[str]) -> DataForSEOResponse:
            self.__class__.summary_targets = targets
            return DataForSEOResponse(
                result={
                    "items_count": len(targets),
                    "items": [
                        {
                            "url": target,
                            "rank": index,
                            "backlinks": index + 1,
                            "referring_pages": index + 1,
                            "referring_domains": index + 1,
                            "referring_main_domains": index + 1,
                        }
                        for index, target in enumerate(sorted(targets))
                    ],
                },
                task_cost_usd=0.0276,
                task_id="summary-100",
            )

        def backlinks(self, target: str, limit: int = 25) -> DataForSEOResponse:
            assert limit == 25
            self.__class__.detailed_targets.append(target)
            return DataForSEOResponse(
                result={"items_count": 0, "items": []},
                task_cost_usd=0.0249,
                task_id=f"deep-{target}",
            )

        def bulk_traffic_estimation(self, targets: list[str]) -> DataForSEOResponse:
            raise AssertionError(f"no traffic call expected for empty backlink rows: {targets}")

    def registered_domain(
        _: str, __: Settings, exact_registrar_check: bool = False
    ) -> AvailabilityResult:
        assert exact_registrar_check is False
        return AvailabilityResult(
            status="registered",
            source="rdap_dns",
            rdap_status="registered",
            dns_status="resolved",
        )

    monkeypatch.setattr(link_hunter, "DataForSEOClient", FunnelClient)
    monkeypatch.setattr(link_hunter, "check_domain", registered_domain)

    with Session(engine) as db:
        db.add_all(
            [DroppedDomain(name=f"domain-{index:03}.example", source="test") for index in range(100)]
        )
        db.commit()

        counters = link_hunter.run_provider_proof(db, settings)

        assert len(FunnelClient.summary_targets) == 100
        assert counters["summary_screened"] == 100
        assert counters["summary_domains_with_live_backlinks"] == 100
        assert counters["deep_proof_target_count"] == 5
        assert counters["backlink_calls"] == 5
        assert FunnelClient.detailed_targets == counters["deep_proof_targets"]
        assert set(FunnelClient.detailed_targets) == {
            "domain-095.example",
            "domain-096.example",
            "domain-097.example",
            "domain-098.example",
            "domain-099.example",
        }
        assert counters["provider_cost_usd"] == 0.1521
        assert counters["errors"] == 0

        summary_queries = db.scalars(
            select(ProviderQuery).where(ProviderQuery.endpoint == "bulk_backlink_summary")
        ).all()
        deep_queries = db.scalars(
            select(ProviderQuery).where(ProviderQuery.endpoint == "backlinks")
        ).all()
        assert len(summary_queries) == 100
        assert len(deep_queries) == 5
