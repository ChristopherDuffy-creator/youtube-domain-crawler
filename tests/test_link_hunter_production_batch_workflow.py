from pathlib import Path


def test_production_batch_is_dormant_capped_and_self_disabling() -> None:
    text = Path(".github/workflows/link-hunter-production-batch.yml").read_text(encoding="utf-8")

    assert "schedule:" not in text
    assert "EXPANDOSAURUS_LINK_HUNTER_AUTOMATION_APPROVED_2026" not in text
    assert "APPROVE_MAX_0.18_USD" in text
    assert 'RUN_CAP_USD: "0.18"' in text
    assert 'LINK_HUNTER_PROOF_MAX_COST_USD="$RUN_CAP_USD"' in text
    assert "LINK_HUNTER_DAILY_MAX_COST_USD=2.16" in text
    assert "LINK_HUNTER_SUMMARY_BATCH_SIZE=100" in text
    assert "LINK_HUNTER_PROOF_BATCH_SIZE=5" in text
    assert "LINK_HUNTER_BACKLINKS_PER_DOMAIN=25" in text
    assert "LINK_HUNTER_ENABLED=true" in text
    assert "LINK_HUNTER_ENABLED=false" in text
    assert "trap cleanup EXIT" in text
    assert "if cost > 0.18" in text
    assert "if errors > 0" in text
    assert "production batch reported item/provider errors" in text
    assert "No winner-queue work queued; zero paid calls made" in text
    assert "daily_budget_exhausted" in text
    assert '"$PUBLIC_URL/login"' in text
    assert '--cookie-jar "$preview_cookie_jar"' in text
    assert '--cookie "$preview_cookie_jar"' in text
    assert '-u "admin:$dashboard_password"' not in text
    assert "/admin/link-hunter/proof-preview" in text
    assert "/api/link-hunter/proof" in text
    assert "summary_count <= 100" in text
    assert "deep_count <= 5" in text
    assert "daily_cap == 2.16" in text
    assert "daily_committed > 2.160001" in text
    assert "Daily $2.16 provider cap reached; zero paid calls made" in text


def test_production_batch_has_direct_post_run_safety_audit() -> None:
    text = Path(".github/workflows/link-hunter-production-batch.yml").read_text(encoding="utf-8")

    assert "Audit production safe state after cleanup" in text
    assert 'payload.get("database") == "ok"' in text
    assert 'payload.get("dataforseo_configured") is True' in text
    assert 'payload.get("link_hunter_enabled") is False' in text
    assert '"context": "link-hunter/post-run-audit"' in text
    assert "Batch succeeded and production is healthy with paid calls disabled" in text


def test_post_run_audit_covers_proof_and_production_batch() -> None:
    text = Path(".github/workflows/link-hunter-post-proof-audit.yml").read_text(encoding="utf-8")

    assert "Controlled Link Hunter Proof" in text
    assert "Production Link Hunter Batch" in text
    assert 'link-hunter/paid-proof' in text
    assert 'link-hunter/proof-disabled' in text
    assert 'link-hunter/production-batch' in text
    assert 'link-hunter/production-disabled' in text
    assert 'payload.get("link_hunter_enabled") is False' in text
