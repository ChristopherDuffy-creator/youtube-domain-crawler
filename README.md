# YouTube Domain Crawler

An always-on crawler that finds exact external domains in older YouTube video descriptions, checks whether those domains can be registered normally, measures real recent view traffic, and ranks the best opportunities.

This project is configured for Christy's final rules:

- **Watchlist:** Porkbun-confirmed ordinary registrations with 20,000+ current long-form views/month before the Day 3 review.
- **3 Day Results:** 20,000+ candidates after the first comparison.
- **7+ Day Results:** 20,000+ candidates after the full week; only this view assigns Qualified or Priority.
- **10k–20k Value Plays:** the protected lower band, shown only after a completed Day 7 review.
- **Qualified:** 50,000+ Day 7 views/month, Buy Score 65+, stable traffic and exact ordinary-registration confirmation.
- **Priority:** 100,000+ Day 7 views/month, Buy Score 75+, stable traffic and exact ordinary-registration confirmation.
- **Display safety:** counter spikes and figures over 1,000,000/month stay in the ledger but out of the opportunity views.
- Ranked YouTube rows have two final actions: **Bought** moves the full score/value snapshot into a dedicated bought-domain ledger and removes the name from every acquisition queue; **Delete** permanently removes its linked records and stores only a one-way fingerprint so crawlers cannot add it back.
- **Target before buying:** 100 qualified/priority domains.
- Personal, creator and brand names are not automatically removed.
- Social sites, link shorteners, affiliate redirectors, registered domains, aftermarket inventory and expensive registry-premium domains do not qualify.
- Email reports go to `info@expandosaurus.com` once email delivery is connected.

## What it does

### Public affiliate guides

The same deployment serves three host-specific public buying guides at
`craftsheaven.club`, `satvic.yoga`, and `teamgerardiperformance.com`. Each site
includes disclosed affiliate recommendations, a consent-based email sign-up,
and a contact form. Subscriber and enquiry records are stored in PostgreSQL and
can be exported from the protected dashboard; contact notifications are sent to
`info@expandosaurus.com` when Resend is configured.

Railway production tracks the GitHub `main` branch and deploys new pushes
automatically.

### Route 1 — YouTube first

The crawler rotates through evergreen, commercially useful searches, but treats search results as seeds rather than the final inventory. It stores each promising channel, resolves that channel's uploads playlist, resumes through playlist pages, fetches video metadata in batches of 50, and permanently indexes every useful external URL. High-yield channels are prioritised and low-yield channels naturally fall behind.

### Route 2 — dropped domains first

The crawler automatically downloads WhoisFreaks' public daily feed of 10,000 recently dropped domains. It first matches the entire list against the permanent local YouTube outbound-domain index; only unmatched/high-priority names need search calls. An exact domain must actually appear in the full description; a loose search result does not count. Feed ingestion and local matching are batched so a full daily file does not create thousands of individual database round trips.

### Traffic verification

YouTube supplies a cumulative view count, not “views in the last month.” The crawler therefore takes immutable daily snapshots and converts actual observed growth into a conservative monthly run-rate. A candidate enters the buying review only after Porkbun confirms that it is available for an ordinary registration; that confirmation starts a fresh Day 0 clock, followed by Day 3 and Day 7 rechecks. Unknown, likely-available, conflicting, premium and registered names remain in the permanent evidence ledger but cannot enter any review tab. Adaptive refresh can run more often, but it is never allowed to skip either checkpoint. The dashboard shows all three figures and their change rather than presenting one projection without context.

Projection safety is fail-closed. Once two independent intervals exist, the internal pace uses the lower median interval velocity and quarantines isolated counter jumps while preserving every raw snapshot for audit. Provisional buy score, expected clicks and potential monthly value remain visible from the starting estimate, with explicit confidence penalties at Start and Day 3. A purchase ceiling appears only after a stable Day 7 result. Videos of 180 seconds or less remain in the permanent evidence ledger but are excluded from all buying tiers because their description traffic is not assumed clickable.

### Registration verification

The free first layer uses RDAP plus DNS and labels a clean result **likely available**. Final qualification requires Porkbun's live registrar check to confirm that it is available for ordinary registration now. Registry-premium names over the configured price ceiling are kept out of the qualified list.

Porkbun's default limit is one availability check every 10 seconds. The crawler checks 10k+ traffic candidates directly with Porkbun, automatically spaces those exact calls, and runs this priority queue hourly. RDAP/DNS remains the free first pass for the much wider discovery ledger. Availability checks are read-only and never purchase a domain.

## Permanent checkpoint

The database is the cumulative ledger. It stores every channel inventory checkpoint, video, domain, raw URL, adaptive refresh state, view snapshot, availability check, dropped name, search page and run result. Restarts resume from this database rather than beginning again.

The known manual-test checkpoint is included:

- 120 dropped domains checked.
- 214 videos checked.
- 124 unique external domains checked.
- Known exact hits: `pixels-forum.com`, `cakedecoratinginstructor.com`, `andygrabertraining.com`, and `fontanaknowledge.com`, with their YouTube video IDs.

These known videos are seeded once, then only refreshed for changed traffic, link or registration status.

## Railway deployment — beginner version

Do not put passwords or API keys into GitHub files.

1. Create a **private** GitHub repository called `youtube-domain-crawler`.
2. Upload this project's files to that repository.
3. In Railway, choose **GitHub Repository** and select `youtube-domain-crawler`.
4. Inside the Railway project, add a **PostgreSQL** database.
5. In the crawler service's **Variables**, add the following as sealed/private values:

   - `YOUTUBE_API_KEY` — the key already created in Google Cloud.
   - `DATABASE_URL` — reference the Railway PostgreSQL `DATABASE_URL` variable.
   - `DASHBOARD_PASSWORD` — a new long password used only to open the results dashboard.
   - `ADMIN_TOKEN` — another new long random value.
   - `ALERT_EMAIL=info@expandosaurus.com`

6. Railway builds from the included `Dockerfile`. Its health check is `/health`.
7. Generate a Railway public domain for the crawler service. Open it with username `admin` and the dashboard password from step 5.

The crawler runs as one process deliberately. Do not increase it to multiple replicas because that would duplicate scheduled jobs and consume extra YouTube quota.

## Two final connections after the crawler is online

### Porkbun — exact availability

Create a Porkbun account and create an API key pair restricted to read/check use. Put the two values directly into Railway Variables:

- `PORKBUN_API_KEY`
- `PORKBUN_SECRET_API_KEY`

The crawler only calls the read-only availability endpoint. It contains no domain-purchase code.

### Resend — emails

Create a Resend account, verify a sending domain, and place these in Railway Variables:

- `RESEND_API_KEY`
- `ALERT_FROM=Domain Crawler <crawler@expandosaurus.com>`
- `ALERT_EMAIL=info@expandosaurus.com`

The dashboard works even before Resend is connected.

## Capacity and quota

The Web Link Hunter has been retired. Its scheduled jobs, recurring GitHub dispatch and paid proof endpoint are disabled. Historical Web records remain untouched in PostgreSQL so removing the crawler does not destroy previously collected data.

### YouTube free-quota capacity

Current official YouTube quota documentation was rechecked on 20 August 2026. `search.list` has a separate default bucket of 100 calls/day; `channels.list`, `playlistItems.list`, and `videos.list` cost 1 unit per call in the 10,000-unit general bucket. The June 2026 `videos.batchGetStats` method has its own 10,000-unit bucket. Every inventory, metadata and statistics request carries up to 50 IDs.

The application now enforces conservative Pacific-day ledgers in PostgreSQL before making each request: at most 96 searches, 9,000 general units, 8,000 fan-out units and 9,000 granular statistics units. The default seed schedule is three `search.list` calls every 60 minutes (72/day), leaving capacity for the daily dropped-domain search and restart headroom. This preserves 4 search calls, 1,000 general units and 1,000 statistics units as safety headroom even if jobs overlap or restart.

The default schedule can scan up to 100 upload-playlist pages every 30 minutes, with dynamic bursts of 12 pages for hot channels, four for warm channels and one for cold/dormant channels. At the 8,000-unit fan-out ceiling, the theoretical maximum is roughly 4,000 full playlist/detail page pairs or 200,000 newly discovered video IDs per day before deduplication. Discovery search pages are also prefiltered against the permanent video index before metadata is fetched, so known videos advance their cursor without repeated description parsing, link extraction or database writes. Adaptive statistics refresh can cover up to 50,000 due linked videos per six-hour run and is protected by its separate bucket, whose hard theoretical ceiling is 450,000 video-stat refreshes/day. Real output depends on channel inventory, public-video availability, deduplication and yield; these are ceilings, not promises.

Five permanent YouTube intelligence upgrades sit behind that capacity:

- database-enforced quota allocation with a safety reserve;
- search-as-seed channel fan-out with resumable pagination and yield-based hot/warm/cold allocation;
- a permanent exact outbound-domain index plus acquisition-facing exposure, click, revenue, purchase-ceiling and monetization signals;
- instant dropped-domain joins in either arrival order, retained as permanent match proof;
- adaptive view refresh using the separate granular statistics bucket, with guaranteed Start/Day 3/Day 7 comparisons and an additional 30-day verification state.

Scale controls:

- `YOUTUBE_CHANNEL_PAGES_PER_RUN` (default 100)
- `YOUTUBE_CHANNEL_PAGE_BURST` (maximum default burst 12 per hot channel/run)
- `YOUTUBE_CHANNEL_FANOUT_INTERVAL_MINUTES` (default 30)
- `YOUTUBE_CHANNEL_RECRAWL_HOURS` (default 24)
- `YOUTUBE_VIEW_REFRESH_BATCH_SIZE` (default 50,000)
- `YOUTUBE_VIEW_REFRESH_INTERVAL_HOURS` (default 6)
- `YOUTUBE_SEARCH_DAILY_LIMIT` (default 96)
- `YOUTUBE_DATA_DAILY_LIMIT` (default 9,000)
- `YOUTUBE_FANOUT_DAILY_DATA_LIMIT` (default 8,000)
- `YOUTUBE_STATS_DAILY_LIMIT` (default 9,000)

The mobile-first dashboard shows:

- four tabs only: Watchlist, 3 Day Results, 7+ Day Results and completed 10k–20k Value Plays;
- Start, Day 3 and Day 7 monthly run-rates together on every opportunity card;
- Buy Score, potential monthly value, suggested purchase ceiling and final ranking status;
- exact registration status, best linked video and compact evidence details;
- permanent Delete and Bought actions;
- one small crawler-health line, with job history kept inside collapsed Crawler details;
- CSV export and a logical PostgreSQL backup.

## Daily email report

The daily email now reports the actual previous 24 hours rather than only showing the number of hits. It includes:

- searches run, videos returned and new videos, domains and exact links saved;
- fresh dropped names loaded, names searched on YouTube and exact matches;
- view snapshots and availability checks completed;
- the current pending pipeline and what each leading candidate is waiting for;
- availability totals, longest traffic-observation window and cumulative ledger totals;
- failed jobs and partial item-level errors with useful error text.

## Dropped-domain feeds

The public WhoisFreaks `0-latest-free-dropped-domains.csv` feed is enabled by default and refreshed every day. No Railway variable is needed. It is a free subset with a one-day delay; the provider states that it contains 10,000 expired and dropped names per day.

To replace it or add more sources, set comma-separated feed URLs in `DROPPED_DOMAIN_FEED_URLS`. You can also paste/upload TXT or CSV lists through the dashboard. Files may contain other CSV columns; valid domains are extracted and permanently deduplicated.

## Local test commands

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest
ruff check app tests
```

Start locally without background jobs:

```bash
export SCHEDULER_ENABLED=false
export DASHBOARD_PASSWORD=test-password
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` and use username `admin`.

## Important expectation

The software automates the search and removes false positives, but it cannot guarantee that 100 exceptional domains exist or remain unregistered. Availability can change at any moment, so the crawler rechecks promising names and the registration page should always be checked again immediately before buying.

Official references: [YouTube API quota](https://developers.google.com/youtube/v3/determine_quota_cost), [YouTube videos.list](https://developers.google.com/youtube/v3/docs/videos/list), [YouTube videos.batchGetStats](https://developers.google.com/youtube/v3/docs/videos/batchGetStats), [Porkbun API](https://porkbun.com/api/json/v3/documentation), and [Resend Python email guide](https://resend.com/docs/send-with-python).
