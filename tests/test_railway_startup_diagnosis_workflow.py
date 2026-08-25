from pathlib import Path


def test_startup_diagnosis_workflow_is_read_only_and_path_gated() -> None:
    text = Path(".github/workflows/railway-startup-diagnosis.yml").read_text(encoding="utf-8")

    assert "railway-startup-diagnosis.yml" in text
    assert "logs" in text
    assert "--latest" in text
    assert "--deployment" in text
    assert "variable set" not in text
    assert " railway up" not in text
    assert "/api/link-hunter/proof" not in text
