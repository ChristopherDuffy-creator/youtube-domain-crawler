from app.config import Settings
from app.jobs import JOB_FUNCTIONS, build_scheduler, run_stackexchange_prefilter_job


def test_stackexchange_prefilter_is_retained_but_not_active() -> None:
    scheduler = build_scheduler(Settings())
    job_ids = {job.id for job in scheduler.get_jobs()}

    assert "stackexchange_prefilter" not in JOB_FUNCTIONS
    assert "initial_stackexchange_prefilter" not in job_ids
    assert "stackexchange_prefilter" not in job_ids
    assert callable(run_stackexchange_prefilter_job)
