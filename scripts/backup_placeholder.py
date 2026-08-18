"""Backup entry point placeholder.

The production database currently needs a portable logical backup before any
schema-changing Link Hunter work is merged. The executable backup flow will be
added only after choosing a destination that keeps the dump outside Railway's
Postgres volume and does not expose DATABASE_URL in logs or source control.
"""


def backup_ready() -> bool:
    return False
