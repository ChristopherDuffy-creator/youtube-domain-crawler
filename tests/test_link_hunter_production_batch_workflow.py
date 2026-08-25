from pathlib import Path


def test_production_batch_uses_the_explicitly_enabled_guarded_endpoint() -> None:
    text = Path(".github/workflows/link-hunter-production-batch.yml").read_text(encoding="utf-8")

    assert "schedule:" not in text
    assert "EXPANDOSAURUS_LINK_HUNTER_AUTOMATION_APPROVED_2026" not in text
    assert "APPROVE_MAX_0.18_USD" in text
    assert 'RUN_CAP_USD: "0.18"' in text
    assert 'payload.get("link_hunter_enabled") is True' in text
    assert "variable set" not in text
    assert "LINK_HUNTER_ENABLED=" not in text
    assert "if cost > 0.18" in text
    assert "if errors > 0" in text
    assert "production batch reported item/provider errors" in text
    assert "/api/link-hunter/proof" in text
    assert 'headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]}' in text
    assert "run_in_progress" in text
    assert "committed > 2.160001" in text
    assert "Daily $2.16 provider cap reached; zero paid calls made" in text


def test_post_run_audit_covers_proof_and_production_batch() -> None:
    text = Path(".github/workflows/link-hunter-post-proof-audit.yml").read_text(encoding="utf-8")

    assert "Production Link Hunter Batch" in text
    assert 'link-hunter/production-batch' in text
    assert 'payload.get("link_hunter_enabled") is True' in text
