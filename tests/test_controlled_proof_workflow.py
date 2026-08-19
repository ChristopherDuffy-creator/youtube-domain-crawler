from pathlib import Path


def test_controlled_proof_workflow_is_manual_capped_and_self_disabling() -> None:
    text = Path(".github/workflows/controlled-link-hunter-proof.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "APPROVE_MAX_0.18_USD" in text
    assert "LINK_HUNTER_PROOF_MAX_COST_USD=0.18" in text
    assert "LINK_HUNTER_PROOF_BATCH_SIZE=5" in text
    assert "LINK_HUNTER_BACKLINKS_PER_DOMAIN=25" in text
    assert "LINK_HUNTER_ENABLED=true" in text
    assert "LINK_HUNTER_ENABLED=false" in text
    assert "trap cleanup EXIT" in text
    assert "provider_cost_usd" in text
    assert "if cost > 0.18" in text
    assert "schedule:" not in text
    assert "\n  push:" not in text
