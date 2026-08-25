from app.config import Settings
from app.jobs import build_scheduler


def test_free_evidence_jobs_are_evenly_spaced_four_times_daily() -> None:
    scheduler = build_scheduler(Settings())

    expected = {
        "commoncrawl_prefilter": ("1,7,13,19", "17"),
        "stackexchange_prefilter": ("2,8,14,20", "27"),
        "hackernews_prefilter": ("3,9,15,21", "37"),
    }
    for job_id, (hours, minutes) in expected.items():
        job = scheduler.get_job(job_id)
        assert job is not None
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert fields["hour"] == hours
        assert fields["minute"] == minutes
