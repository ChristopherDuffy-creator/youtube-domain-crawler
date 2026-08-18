from scripts.backup_placeholder import backup_ready


def test_schema_changes_remain_gated_until_backup_is_ready():
    assert backup_ready() is False
