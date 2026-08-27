from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.data_hygiene import enforce_candidate_signal_consistency
from app.database import Base
from app.models import Candidate, Domain, YouTubeDomainSignal


def _ranked(
    db: Session,
    name: str,
    *,
    tier: str,
    stage: str,
    buy_score: float,
    exposure: int,
    buy_ready: bool,
) -> Candidate:
    domain = Domain(name=name, availability_status="available")
    db.add(domain)
    db.flush()
    candidate = Candidate(
        domain_id=domain.id,
        tier=tier,
        evaluation_stage=stage,
        buy_ready=buy_ready,
    )
    signal = YouTubeDomainSignal(
        domain_id=domain.id,
        click_eligible_exposure=exposure,
        buy_score=buy_score,
        spike_video_count=0,
        model_version=4,
    )
    db.add_all([candidate, signal])
    return candidate


def test_qualified_and_priority_require_final_day7_score_and_stability() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        early = _ranked(
            db,
            "early.example",
            tier="qualified",
            stage="day3",
            buy_score=80,
            exposure=80_000,
            buy_ready=False,
        )
        weak_score = _ranked(
            db,
            "weak-score.example",
            tier="qualified",
            stage="day7",
            buy_score=64.9,
            exposure=80_000,
            buy_ready=True,
        )
        qualified = _ranked(
            db,
            "qualified.example",
            tier="qualified",
            stage="day7",
            buy_score=70,
            exposure=80_000,
            buy_ready=True,
        )
        weak_priority = _ranked(
            db,
            "weak-priority.example",
            tier="priority",
            stage="day7",
            buy_score=74.9,
            exposure=150_000,
            buy_ready=True,
        )
        priority = _ranked(
            db,
            "priority.example",
            tier="priority",
            stage="day7",
            buy_score=80,
            exposure=150_000,
            buy_ready=True,
        )
        db.commit()

        enforce_candidate_signal_consistency(db, Settings())

        assert early.tier == "pending"
        assert weak_score.tier == "pending"
        assert qualified.tier == "qualified"
        assert weak_priority.tier == "pending"
        assert priority.tier == "priority"
