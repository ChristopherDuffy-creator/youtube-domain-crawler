from pathlib import Path


def test_controlled_proof_workflow_is_manual_capped_and_self_disabling() -> None:
    text = Path(".github/workflows/controlled-link-hunter-proof.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "APPROVE_MAX_0.18_USD" in text
    assert "LINK_HUNTER_PROOF_MAX_COST_USD=0.18" in text
    assert "LINK_HUNTER_SUMMARY_BATCH_SIZE=100" in text
    assert "LINK_HUNTER_PROOF_BATCH_SIZE=5" in text
    assert "LINK_HUNTER_BACKLINKS_PER_DOMAIN=25" in text
    assert "LINK_HUNTER_ENABLED=true" in text
    assert "LINK_HUNTER_ENABLED=false" in text
    assert "trap cleanup EXIT" in text
    assert "provider_cost_usd" in text
    assert "if cost > 0.18" in text
    assert "if errors > 0" in text
    assert "schedule:" not in text
    assert "\n  push:" not in text


def test_controlled_proof_uses_zero_cost_production_preview_before_activation() -> None:
    text = Path(".github/workflows/controlled-link-hunter-proof.yml").read_text(encoding="utf-8")

    preflight = text.index("/admin/link-hunter/proof-preview")
    activation = text.index("LINK_HUNTER_ENABLED=true")

    assert preflight < activation
    assert 'payload.get(\'provider_calls_made\') == 0' in text
    assert "preview.get('paid_requests_made') == 0" in text
    assert "preview.get('ready_for_controlled_proof') is True" in text
    assert "int(preview.get('target_count') or 0) == summary_count" in text
    assert "0 < summary_count <= 100" in text
    assert "0 < deep_count <= 5" in text
    assert "cost <= 0.18" in text
    assert "preview.get('credentials_exposed') is False" in text
    assert 'fail "preflight-http"' in text
    assert 'fail "preflight-preview"' in text
    assert "DATABASE_PUBLIC_URL" not in text
    assert "--service Postgres" not in text
