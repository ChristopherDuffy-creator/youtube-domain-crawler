from app.config import Settings
from app.jobs import build_scheduler


def test_retired_web_evidence_jobs_are_not_scheduled() -> None:
    scheduler = build_scheduler(Settings())

    for job_id in ("commoncrawl_prefilter", "stackexchange_prefilter", "hackernews_prefilter"):
        assert scheduler.get_job(job_id) is None
