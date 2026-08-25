from pathlib import Path


def test_controlled_proof_workflow_dispatches_the_single_guarded_path() -> None:
    text = Path(".github/workflows/controlled-link-hunter-proof.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "APPROVE_MAX_0.18_USD" in text
    assert '"approval": "APPROVE_MAX_0.18_USD"' in text
    assert "link-hunter-production-batch.yml/dispatches" in text
    assert "LINK_HUNTER_ENABLED=" not in text
    assert "variable set" not in text
    assert "schedule:" not in text
    assert "\n  push:" not in text


def test_controlled_proof_does_not_make_provider_or_railway_calls() -> None:
    text = Path(".github/workflows/controlled-link-hunter-proof.yml").read_text(encoding="utf-8")

    assert "/api/link-hunter/proof" not in text
    assert "RAILWAY_TOKEN" not in text
    assert "npx -y @railway/cli" not in text
    assert "schedule:" not in text
