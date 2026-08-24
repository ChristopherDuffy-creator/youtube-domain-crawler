from app.web_hunter_upgrade import (
    _summary_rescue_points,
    source_focus_category,
    traffic_first_rerank_summary_targets,
)


def test_source_focus_classifies_government_and_academic_hosts() -> None:
    assert source_focus_category("www.transport.gov.uk")[0] == "government"
    assert source_focus_category("cs.stanford.edu")[0] == "academic"
    assert source_focus_category("physics.ox.ac.uk")[0] == "academic"


def test_source_focus_classifies_editorial_and_community_sources() -> None:
    assert source_focus_category("news.ycombinator.com", source_type="hackernews")[0] == "community"
    assert source_focus_category(
        "publisher.example",
        domain_rank=75,
        semantic_location="article",
    )[0] == "editorial"


def test_cached_summary_and_source_focus_push_deep_proof_priority() -> None:
    targets = ["fresh.example", "rescue.example"]
    free_scores = {"fresh.example": 15.0, "rescue.example": 15.0}
    free_signals = {
        "fresh.example": {},
        "rescue.example": {
            "summary_rescue_points": _summary_rescue_points(
                {
                    "rank": 70.0,
                    "referring_pages": 120,
                    "referring_domains": 40,
                    "opportunity_score": 39.9,
                }
            ),
            "source_focus_bonus": 8.0,
        },
    }
    summaries = {
        "fresh.example": {
            "referring_pages": 20,
            "referring_domains": 10,
            "rank": 45.0,
        },
        "rescue.example": {
            "referring_pages": 20,
            "referring_domains": 10,
            "rank": 45.0,
        },
    }

    deep_targets, combined, _ = traffic_first_rerank_summary_targets(
        targets,
        free_scores,
        free_signals,
        summaries,
        deep_limit=1,
    )

    assert deep_targets == ["rescue.example"]
    assert combined["rescue.example"] > combined["fresh.example"]
