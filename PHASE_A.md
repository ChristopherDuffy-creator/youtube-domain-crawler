# Expandosaurus Link Hunter — Phase A / B checkpoint

Production remains protected while the web-wide system is developed on a separate branch.

## Verified production baseline

- Production source repository: `ChristopherDuffy-creator/youtube-domain-crawler`.
- Railway production uses one application replica and one Postgres replica.
- Railway deploy logs showed the 90-minute `run_discovery` scheduler job executing successfully and YouTube API requests returning HTTP 200.
- PostgreSQL is persistent. Railway-managed backups were not enabled when first inspected.
- A logical database backup endpoint with a tested restore path has been added to the production source baseline.
- `httpx` and `httpcore` INFO request logging is suppressed so query-string API credentials are not printed.
- A Railway CLI deployment fallback is now on `main` for cases where Railway's GitHub source-snapshot stage fails.

## Web Link Hunter development state

- DataForSEO credentials are expected as `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`.
- `LINK_HUNTER_ENABLED` defaults to `false`; credentials alone cannot start provider calls.
- Provider proof is cost-capped and is not scheduled automatically.
- DataForSEO request contracts are covered by mocked HTTP tests for bulk backlink summaries, detailed live backlinks, provider errors and Google bulk traffic estimation.
- The mocked end-to-end proof covers dropped domain → backlink evidence → source-page traffic → direct link verification → availability → registrar confirmation → ranked web opportunity.
- Web-wide tables and dashboard/export surfaces are separate from YouTube while sharing the same PostgreSQL database and operational run ledger.

## Production gate

Do not merge the Link Hunter into production until a fresh production database backup has been successfully downloaded and verified. The first real provider run should remain a tiny controlled proof batch before any scaling.
