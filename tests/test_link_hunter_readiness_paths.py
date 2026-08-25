from pathlib import Path


def test_readiness_audit_covers_all_link_hunter_production_paths() -> None:
    text = Path(".github/workflows/link-hunter-readiness.yml").read_text(encoding="utf-8")

    assert "app/link_hunter.py" in text
    assert "app/link_hunter_preview.py" in text
    assert "app/templates/dashboard.html" in text
    assert ".github/workflows/controlled-link-hunter-proof.yml" in text
    assert ".github/workflows/link-hunter-production-batch.yml" in text
    assert ".github/workflows/link-hunter-post-proof-audit.yml" in text
    assert 'payload.get("link_hunter_enabled") is True' in text
    assert "Link Hunter guarded proof endpoint explicitly enabled" in text
    assert "Link Hunter paid calls currently disabled" not in text
