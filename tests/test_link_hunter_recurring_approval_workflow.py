from pathlib import Path


def test_recurring_approval_enabler_is_one_shot_and_exact() -> None:
    text = Path(".github/workflows/enable-link-hunter-recurring.yml").read_text(encoding="utf-8")

    assert "EXPANDOSAURUS_LINK_HUNTER_AUTOMATION_APPROVED_2026" in text
    assert "APPROVE_MAX_0.36_USD_PER_DAY" in text
    assert "Enable Link Hunter recurring max $0.36/day" in text
    assert "/actions/variables/${VARIABLE_NAME}" in text
    assert "/actions/variables\"" in text
    assert "link-hunter/recurring-approved" in text
    assert "workflow_dispatch" not in text
