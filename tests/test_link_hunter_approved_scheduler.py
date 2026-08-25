from pathlib import Path


def test_approved_scheduler_caps_recurring_dispatches() -> None:
    scheduler = Path(".github/workflows/link-hunter-approved-scheduler.yml").read_text(encoding="utf-8")
    approval = Path(".github/link-hunter-recurring-approval.txt").read_text(encoding="utf-8").strip()

    assert approval == "APPROVE_MAX_2.16_USD_PER_DAY"
    assert 'cron: "0 */2 * * *"' in scheduler
    assert "workflow_dispatch" not in scheduler
    assert "APPROVE_MAX_2.16_USD_PER_DAY" in scheduler
    assert "$2.16/UTC day" in scheduler
    assert '"approval": "APPROVE_MAX_0.18_USD"' in scheduler
    assert "link-hunter-production-batch.yml/dispatches" in scheduler
    assert "link-hunter/recurring-scheduler" in scheduler
    assert "LINK_HUNTER_ENABLED=" not in scheduler
