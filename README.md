# YouTube Domain Crawler

An always-on crawler that finds exact external domains in older YouTube video descriptions, checks whether those domains can be registered normally, measures real recent view traffic, and ranks the best opportunities.

This project is configured for Christy's final rules:

- **Watchlist:** 5,000+ projected monthly views.
- **Qualified:** 20,000+ views measured over a real 27–35 day window.
- **Priority:** 100,000+ verified monthly views.
- **Target before buying:** 100 qualified/priority domains.
- Personal, creator and brand names are not automatically removed.
- Social sites, link shorteners, affiliate redirectors, registered domains, aftermarket inventory and expensive registry-premium domains do not qualify.
- Email reports go to `info@expandosaurus.com` once email delivery is connected.

## What it does

### Route 1 — YouTube first

The crawler rotates through evergreen, commercially useful searches, but treats search results as seeds rather than the final inventory. It stores each promising channel, resolves that channel's uploads playlist, resumes through playlist pages, fetches video metadata in batches of 50, and permanently indexes every useful external URL. High-yield channels are prioritised and low-yield channels naturally fall behind.

### Route 2 — dropped domains first

The crawler automatically downloads WhoisFreaks' public daily feed of 10,000 recently dropped domains. It first matches the entire list against the permanent local YouTube outbound-domain index; only unmatched/high-priority names need search calls. An exact domain must actually appear in the full description; a loose search result does not count. Feed ingestion and local matching are batched so a full daily file does not create thousands of individual database round trips.

### Traffic verification

YouTube supplies a cumulative view count, not “views in the last month.” The crawler therefore takes its own snapshots. Only videos with useful outbound links enter the refresh queue: fast-growing/high-exposure videos refresh frequently, slower videos back off, and repeatedly stagnant videos can fall to a 30-day interval. Early numbers are clearly marked **projected**. A candidate cannot become Qualified or Priority until the crawler has a genuine 27–35 day measurement window.

### Registration verification

The free first layer uses RDAP plus DNS and labels a clean result **likely available**. Final qualification requires Porkbun's live registrar check to confirm that it is available for ordinary registration now. Registry-premium names over the configured price ceiling are kept out of the qualified list.

Porkbun's default limit is one availability check every 10 seconds, so the crawler uses RDAP/DNS first and sends only traffic-qualified possibilities to Porkbun. Exact calls are automatically spaced apart to respect that limit.

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

### Web Link Hunter paid capacity

The approved controller has twelve evenly spaced UTC slots per day. Every slot can cheaply screen up to 100 fresh unchecked domains and rerank them before at most five domains receive detailed backlink, source-traffic, link-verification and availability proof. The conservative envelope remains $0.18/run, with a database-backed hard reservation ledger capped at $2.16/UTC day. A full twelve-run day screens up to 1,200 fresh domains and deep-proofs up to 60 for a modeled maximum of $2.1492; empty queues and exhausted budgets skip without provider calls. The free/cached ranking pool covers the 10,000-name daily dropped-domain feed so later slots do not starve after the first 1,000 checks.

Only the approved scheduler owns recurring dispatch. The production batch workflow has no second cron route, preventing duplicate paid runs. Paid calls are disabled again after every slot.

### YouTube free-quota capacity

Current official YouTube quota documentation was rechecked on 19 August 2026. `search.list` has a separate default bucket of 100 calls/day; `channels.list`, `playlistItems.list`, and `videos.list` cost 1 unit per call. Inventory and video requests are batched/pages of up to 50.

The default schedule retains about 48 search seeds/day and runs 12 upload-playlist pages every 30 minutes. That creates a theoretical ceiling of about 28,800 newly discovered video IDs/day before deduplication, while using roughly 1,200 daily units for playlist plus video-detail calls. Adaptive statistics refresh is capped at 2,500 due linked videos per six-hour run (50 batched calls). These are safe starting limits, not a promise that every channel contains 28,800 new public videos.

Scale controls:

- `YOUTUBE_CHANNEL_PAGES_PER_RUN` (default 12)
- `YOUTUBE_CHANNEL_PAGE_BURST` (default 3 per channel/run)
- `YOUTUBE_CHANNEL_FANOUT_INTERVAL_MINUTES` (default 30)
- `YOUTUBE_CHANNEL_RECRAWL_HOURS` (default 24)
- `YOUTUBE_VIEW_REFRESH_BATCH_SIZE` (default 2,500)
- `YOUTUBE_VIEW_REFRESH_INTERVAL_HOURS` (default 6)

The dashboard shows:

- progress toward 100 very good domains;
- qualified, priority, watchlist and pending candidates;
- verified versus projected 30-day views;
- exact registration status and price;
- candidate score, best linked video, lifetime views and repeat-link counts;
- cumulative new and manual-test counts;
- the latest job history;
- CSV download of the useful results.

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

Official references: [YouTube API quota](https://developers.google.com/youtube/v3/determine_quota_cost), [YouTube videos.list](https://developers.google.com/youtube/v3/docs/videos/list), [Porkbun API](https://porkbun.com/api/json/v3/documentation), and [Resend Python email guide](https://resend.com/docs/send-with-python).
