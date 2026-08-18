# Database backup requirement before Link Hunter schema changes

The current Railway plan does not provide managed backups/PITR for this Postgres service. Before any schema-changing Link Hunter migration is merged, create and verify a portable logical backup of the production database.

Preferred method: PostgreSQL `pg_dump` in custom format (`-Fc`) with no owner/ACL metadata, stored outside the Railway database volume, then verify the dump can be listed/read by `pg_restore -l`.

Do not commit `DATABASE_URL`, passwords, API keys, or dump files to GitHub.
