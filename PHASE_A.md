# Expandosaurus Link Hunter — Phase A

Phase A protects and verifies the existing production crawler before the web-wide Link Hunter build.

Verified on 2026-08-18:

- Production source repository: `ChristopherDuffy-creator/youtube-domain-crawler`.
- Production head at verification: `d1a2e6ed37599562ae655a1a7c0bc55e871f4d8e`.
- Railway deployment reported active and successful.
- Railway application service reported one replica.
- Railway Postgres service reported online with a persistent volume.
- Railway deploy logs showed the 90-minute `run_discovery` scheduler job executing successfully and YouTube API requests returning HTTP 200.
- Railway managed backups/PITR were not enabled on the current plan; a portable logical backup is required before schema-changing Link Hunter work.

Safety work in this branch:

- Keep `httpx` and `httpcore` request logging at WARNING to prevent query-string credentials from appearing in Railway INFO logs.
- Add CI to run pytest and Ruff before merge.

Do not merge schema-changing work until a current database backup has been taken and verified.
