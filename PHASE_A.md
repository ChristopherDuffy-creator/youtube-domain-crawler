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
- Production logical backup `expandosaurus-postgres-20260819-060739.json.gz` was downloaded and verified on 2026-08-19: gzip/JSON integrity passed, all 9 declared table counts matched the actual rows, and core foreign-key references were intact.

## Web Link Hunter development state

- DataForSEO credentials are expected as `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`.
- `LINK_HUNTER_ENABLED` defaults to `false`; credentials alone cannot start provider calls.
- Provider proof is cost-capped and is not scheduled automatically.
- A preflight worst-case spend envelope rejects oversized proof settings before the first provider HTTP request.
- DataForSEO request contracts are covered by mocked HTTP tests for bulk backlink summaries, detailed live backlinks, provider errors and Google bulk traffic estimation.
- The mocked end-to-end proof covers dropped domain → backlink evidence → source-page traffic → direct exact-target link verification → availability → registrar confirmation → ranked web opportunity.
- Web-wide tables and dashboard/export surfaces are separate from YouTube while sharing the same PostgreSQL database and operational run ledger.
- Daily reporting includes a distinct Web Link Hunter section with provider spend and source evidence.
- Draft PR CI failures are non-blocking to avoid notification spam; marking the PR ready reruns the same checks as hard failures before merge.
- Current branch CI baseline: 47 tests passing and Ruff passing.

## Production gate

The production backup gate is cleared. The Link Hunter code may be merged with `LINK_HUNTER_ENABLED=false`. The first real provider run must remain a tiny controlled proof batch before any scaling or scheduling.
