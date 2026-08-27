from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import jobs
from app.config import Settings
from app.database import Base
from app.jobs import JOB_FUNCTIONS, build_scheduler, run_hackernews_prefilter_job


def test_hackernews_prefilter_is_retained_but_not_active() -> None:
    scheduler = build_scheduler(Settings())
    job_ids = {job.id for job in scheduler.get_jobs()}

    assert "hackernews_prefilter" not in JOB_FUNCTIONS
    assert "initial_hackernews_prefilter" not in job_ids
    assert "hackernews_prefilter" not in job_ids
    assert callable(run_hackernews_prefilter_job)


def test_hackernews_job_uses_the_bounded_throughput_settings(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured: dict[str, int] = {}

    monkeypatch.setattr(jobs, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: Settings(hackernews_prefilter_batch_size=25, hackernews_prefilter_hits_per_page=50),
    )
    monkeypatch.setattr(
        jobs,
        "run_hackernews_prefilter_batch",
        lambda _db, **kwargs: captured.update(kwargs)
        or {"queries": 0, "search_hits": 0, "errors": 0},
    )

    run_hackernews_prefilter_job()

    assert captured == {"batch_size": 25, "hits_per_page": 50}
