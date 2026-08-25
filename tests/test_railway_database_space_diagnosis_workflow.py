from pathlib import Path


def test_database_space_diagnosis_is_path_gated_and_read_only() -> None:
    text = Path(
        ".github/workflows/railway-database-space-diagnosis.yml"
    ).read_text(encoding="utf-8")

    assert "railway-database-space-diagnosis.yml" in text
    assert "SET TRANSACTION READ ONLY" in text
    assert "pg_total_relation_size" in text
    assert "pg_database_size" in text
    assert "variable set" not in text
    assert "/api/link-hunter/proof" not in text
