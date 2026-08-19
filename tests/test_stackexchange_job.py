from app.config import Settings
from app.jobs import JOB_FUNCTIONS, build_scheduler, run_stackexchange_prefilter_job


def test_stackexchange_prefilter_is_registered_for_manual_and_scheduled_runs() -> None:
    scheduler = build_scheduler(Settings())
    job_ids = {job.id for job in scheduler.get_jobs()}

    assert JOB_FUNCTIONS["stackexchange_prefilter"] is run_stackexchange_prefilter_job
    assert "initial_stackexchange_prefilter" in job_ids
    assert "stackexchange_prefilter" in job_ids
