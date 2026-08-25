from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import jobs
from app.config import Settings
from app.database import Base
from app.jobs import JOB_FUNCTIONS, build_scheduler, run_commoncrawl_prefilter_job


def test_commoncrawl_prefilter_is_registered_for_manual_and_scheduled_runs() -> None:
    scheduler = build_scheduler(Settings())
    job_ids = {job.id for job in scheduler.get_jobs()}

    assert JOB_FUNCTIONS["commoncrawl_prefilter"] is run_commoncrawl_prefilter_job
    assert "initial_commoncrawl_prefilter" in job_ids
    assert "commoncrawl_prefilter" in job_ids


def test_commoncrawl_job_uses_the_bounded_throughput_settings(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured: dict[str, int] = {}

    monkeypatch.setattr(jobs, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: Settings(commoncrawl_prefilter_batch_size=25, commoncrawl_prefilter_index_count=2),
    )
    monkeypatch.setattr(
        jobs,
        "run_commoncrawl_prefilter_batch",
        lambda _db, **kwargs: captured.update(kwargs)
        or {"checked": 0, "with_capture": 0, "without_capture": 0, "errors": 0},
    )

    run_commoncrawl_prefilter_job()

    assert captured == {"batch_size": 25, "index_count": 2}
