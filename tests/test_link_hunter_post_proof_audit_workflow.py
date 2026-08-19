from pathlib import Path


def test_post_proof_audit_is_automatic_and_zero_spend() -> None:
    text = Path(".github/workflows/link-hunter-post-proof-audit.yml").read_text(encoding="utf-8")

    assert "workflow_run:" in text
    assert "Controlled Link Hunter Proof" in text
    assert 'link-hunter/paid-proof' in text
    assert 'link-hunter/proof-disabled' in text
    assert 'link-hunter/post-proof-audit' in text
    assert 'payload.get("database") == "ok"' in text
    assert 'payload.get("dataforseo_configured") is True' in text
    assert 'payload.get("link_hunter_enabled") is False' in text
    assert "LINK_HUNTER_ENABLED=true" not in text
    assert "/api/link-hunter/proof" not in text
    assert "RAILWAY_TOKEN" not in text
