from app.config import Settings
from app.jobs import JOB_FUNCTIONS, build_scheduler, run_hackernews_prefilter_job


def test_hackernews_prefilter_is_registered_for_manual_and_scheduled_runs() -> None:
    scheduler = build_scheduler(Settings())
    job_ids = {job.id for job in scheduler.get_jobs()}

    assert JOB_FUNCTIONS["hackernews_prefilter"] is run_hackernews_prefilter_job
    assert "initial_hackernews_prefilter" in job_ids
    assert "hackernews_prefilter" in job_ids
