from pathlib import Path


def test_retired_scheduler_cannot_dispatch_recurring_web_work() -> None:
    scheduler = Path(".github/workflows/link-hunter-approved-scheduler.yml").read_text(encoding="utf-8")
    approval = Path(".github/link-hunter-recurring-approval.txt").read_text(encoding="utf-8").strip()

    assert approval == "DISABLED_YOUTUBE_ONLY"
    assert "schedule:" not in scheduler
    assert "cron:" not in scheduler
    assert "workflow_dispatch" in scheduler
    assert "link-hunter-production-batch.yml/dispatches" not in scheduler
    assert "no crawler or paid-provider work was started" in scheduler
