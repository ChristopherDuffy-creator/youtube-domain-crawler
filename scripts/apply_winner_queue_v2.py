from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def replace_regex(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected one regex match, found {count}")
    return updated


# ---------------------------------------------------------------------------
# DataForSEO cost estimator: allow deep proof from an already-paid cached
# summary without requiring a fresh summary request in the same run.
# ---------------------------------------------------------------------------
path = "app/dataforseo.py"
text = read(path)
old = '''    if summary_domain_count <= 0:\n        if deep_domain_count != 0:\n            raise ValueError("deep_domain_count must be zero when no domains are screened")\n        return 0.0\n    if summary_domain_count > MAX_BULK_SUMMARY_DOMAINS:\n        raise ValueError(f"proof cost estimate supports at most {MAX_BULK_SUMMARY_DOMAINS} domains")\n    if deep_domain_count < 0 or deep_domain_count > summary_domain_count:\n        raise ValueError("deep_domain_count must be between 0 and summary_domain_count")\n    if backlinks_per_domain <= 0 or backlinks_per_domain > 1000:\n        raise ValueError("backlinks_per_domain must be between 1 and 1000")\n\n    summary_cost = BACKLINK_REQUEST_USD + BACKLINK_ROW_USD * summary_domain_count\n'''
new = '''    if summary_domain_count < 0:\n        raise ValueError("summary_domain_count cannot be negative")\n    if summary_domain_count > MAX_BULK_SUMMARY_DOMAINS:\n        raise ValueError(f"proof cost estimate supports at most {MAX_BULK_SUMMARY_DOMAINS} domains")\n    if deep_domain_count < 0:\n        raise ValueError("deep_domain_count cannot be negative")\n    if backlinks_per_domain <= 0 or backlinks_per_domain > 1000:\n        raise ValueError("backlinks_per_domain must be between 1 and 1000")\n    if summary_domain_count == 0 and deep_domain_count == 0:\n        return 0.0\n\n    # A deep-proof target may already have a permanent BacklinkSummary from an\n    # earlier batch. In that case we deliberately pay no new summary cost and\n    # spend the run only on the strongest cached winner candidates.\n    summary_cost = (\n        BACKLINK_REQUEST_USD + BACKLINK_ROW_USD * summary_domain_count\n        if summary_domain_count\n        else 0.0\n    )\n'''
text = replace_once(text, old, new, "dataforseo cached-summary estimator")
write(path, text)


# ---------------------------------------------------------------------------
# Permanent winner queue. Every paid bulk summary stays eligible for later
# deep proof until a completed detailed-backlinks call exists for that domain.
# This fixes the old 100-screen / 5-deep behavior where the other 95 could be
# permanently stranded as pending.
# ---------------------------------------------------------------------------
path = "app/link_hunter_preview.py"
text = read(path)
text = replace_once(
    text,
    '''from app.models import (\n    Candidate,\n    Domain,''',
    '''from app.models import (\n    BacklinkSummary,\n    Candidate,\n    Domain,''',
    "preview BacklinkSummary import",
)
text = replace_once(
    text,
    '''_BLOCKED_AVAILABILITY = {"registered", "aftermarket", "premium"}''',
    '''_BLOCKED_AVAILABILITY = {"registered", "aftermarket", "premium", "reserved"}''',
    "preview blocked availability",
)
anchor = '''def _commoncrawl_signals(db: Session) -> dict[str, int]:\n'''
insert = '''def _dataforseo_deep_checked_targets(db: Session) -> set[str]:\n    """Domains whose expensive detailed backlink proof already completed."""\n    return set(\n        db.scalars(\n            select(ProviderQuery.target).where(\n                ProviderQuery.provider == "dataforseo",\n                ProviderQuery.endpoint == "backlinks",\n                ProviderQuery.status == "complete",\n            )\n        ).all()\n    )\n\n\n'''
text = replace_once(text, anchor, insert + anchor, "preview deep checked helper")

anchor = '''def _rank_free_candidates(\n'''
insert = '''def _free_signal_row(name: str, context: dict[str, Any]) -> dict[str, int | float | str]:\n    yt = context["youtube"].get(name, {})\n    free_screen = context["screening"].get(name, {})\n    return {\n        "exact_links": int(context["exact_links"].get(name, 0) or 0),\n        "independent_sites": int(context["independent_sites"].get(name, 0) or 0),\n        "verified_links": int(context["verified_links"].get(name, 0) or 0),\n        "commoncrawl_hits": int(context["commoncrawl"].get(name, 0) or 0),\n        "youtube_monthly_views": int(yt.get("monthly_views", 0) or 0),\n        "youtube_video_count": int(yt.get("video_count", 0) or 0),\n        "youtube_link_count": int(yt.get("link_count", 0) or 0),\n        "availability": str(context["availability"].get(name, "unknown")),\n        "screening_status": str(free_screen.get("status", "unscreened")),\n        "screening_quality": float(free_screen.get("quality_score", 0.0) or 0.0),\n        "screening_risk": float(free_screen.get("risk_score", 0.0) or 0.0),\n    }\n\n\ndef _free_score_for_name(name: str, context: dict[str, Any]) -> tuple[float, dict[str, int | float | str]]:\n    row = _free_signal_row(name, context)\n    score = _free_preproof_score(\n        exact_links=int(row["exact_links"]),\n        independent_sites=int(row["independent_sites"]),\n        verified_links=int(row["verified_links"]),\n        commoncrawl_hits=int(row["commoncrawl_hits"]),\n        youtube_monthly_views=int(row["youtube_monthly_views"]),\n        youtube_video_count=int(row["youtube_video_count"]),\n        youtube_link_count=int(row["youtube_link_count"]),\n        availability_status=str(row["availability"]),\n        screening_quality=float(row["screening_quality"]),\n        screening_risk=float(row["screening_risk"]),\n    )\n    return score, row\n\n\n'''
text = replace_once(text, anchor, insert + anchor, "preview free row helpers")

anchor = '''def select_provider_proof_targets(db: Session, settings: Settings) -> list[str]:\n'''
insert = '''def _summary_record_payload(summary: BacklinkSummary) -> dict[str, Any]:\n    payload = dict(summary.raw_summary or {})\n    payload.setdefault("backlinks", int(summary.backlinks or 0))\n    payload.setdefault("referring_pages", int(summary.referring_pages or 0))\n    payload.setdefault("referring_domains", int(summary.referring_domains or 0))\n    payload.setdefault("referring_main_domains", int(summary.referring_main_domains or 0))\n    payload.setdefault("rank", float(summary.rank or 0.0))\n    return payload\n\n\ndef select_cached_deep_proof_targets_with_ranking(\n    db: Session,\n    settings: Settings,\n    *,\n    limit: int | None = None,\n    context: dict[str, Any] | None = None,\n) -> tuple[\n    list[str],\n    dict[str, float],\n    dict[str, float],\n    dict[str, float],\n    dict[str, dict[str, int | float | str]],\n]:\n    """Rank every cached live summary that has never received detailed proof.\n\n    This is the permanent winner queue: a name is not discarded merely because\n    it failed to make the top five in the same batch that first summarised it.\n    """\n    rank_context = context or _free_rank_context(db)\n    already_deep = _dataforseo_deep_checked_targets(db)\n    rows = db.execute(\n        select(BacklinkSummary, Domain)\n        .join(Domain, Domain.id == BacklinkSummary.domain_id)\n        .where(\n            BacklinkSummary.provider == "dataforseo",\n            BacklinkSummary.referring_pages > 0,\n        )\n    ).all()\n    if not rows:\n        return [], {}, {}, {}, {}\n\n    candidate_names = [domain.name for _, domain in rows if domain.name not in already_deep]\n    blocked_screening = (\n        set(\n            db.scalars(\n                select(WebScreening.domain_name).where(\n                    WebScreening.status == "blocked",\n                    WebScreening.domain_name.in_(candidate_names),\n                )\n            ).all()\n        )\n        if candidate_names\n        else set()\n    )\n\n    combined_scores: dict[str, float] = {}\n    summary_scores: dict[str, float] = {}\n    free_scores: dict[str, float] = {}\n    free_signals: dict[str, dict[str, int | float | str]] = {}\n    sort_rows: list[tuple[str, float, float, float, int, int]] = []\n    for summary, domain in rows:\n        name = domain.name\n        if name in already_deep or name in blocked_screening:\n            continue\n        if domain.availability_status in _BLOCKED_AVAILABILITY:\n            continue\n        free_score, signal = _free_score_for_name(name, rank_context)\n        summary_score = _summary_signal_score(_summary_record_payload(summary))\n        combined = round(free_score + summary_score, 2)\n        free_scores[name] = free_score\n        free_signals[name] = signal\n        summary_scores[name] = summary_score\n        combined_scores[name] = combined\n        sort_rows.append(\n            (\n                name,\n                combined,\n                free_score,\n                summary_score,\n                int(summary.referring_main_domains or summary.referring_domains or 0),\n                int(summary.referring_pages or 0),\n            )\n        )\n\n    sort_rows.sort(\n        key=lambda row: (-row[1], -row[2], -row[3], -row[4], -row[5], row[0])\n    )\n    ordered = [row[0] for row in sort_rows]\n    if limit is not None:\n        ordered = ordered[: max(0, limit)]\n    return ordered, combined_scores, summary_scores, free_scores, free_signals\n\n\n'''
text = replace_once(text, anchor, insert + anchor, "preview permanent winner queue")

new_readiness = '''def _proof_readiness(\n    *,\n    settings: Settings,\n    targets: list[str],\n    work_available: bool,\n    estimated_max_cost_usd: float,\n    free_positive_count: int,\n    daily_budget: dict[str, float | int | str],\n) -> dict[str, Any]:\n    """Return zero-cost activation diagnostics without exposing secrets."""\n    blockers: list[str] = []\n    warnings: list[str] = []\n    if not settings.dataforseo_enabled:\n        blockers.append("dataforseo_credentials_not_configured")\n    if not work_available:\n        blockers.append("no_queued_work")\n    if estimated_max_cost_usd > settings.link_hunter_proof_max_cost_usd:\n        blockers.append("estimated_cost_exceeds_configured_cap")\n    # The database reservation holds the full configured per-run envelope.\n    # Stop before Railway paid mode is enabled when that reservation cannot fit.\n    if (\n        work_available\n        and float(daily_budget.get("remaining_usd") or 0.0) + 1e-9\n        < settings.link_hunter_proof_max_cost_usd\n    ):\n        blockers.append("daily_budget_exhausted")\n    if settings.link_hunter_enabled:\n        warnings.append("link_hunter_already_enabled")\n    if targets and free_positive_count == 0:\n        warnings.append("no_free_positive_signal_in_new_summary_batch")\n    return {\n        "ready_for_controlled_proof": not blockers,\n        "activation_blockers": blockers,\n        "activation_warnings": warnings,\n        "requires_explicit_spend_approval": True,\n        "credentials_present": settings.dataforseo_enabled,\n        "credentials_exposed": False,\n    }\n\n\n'''
text = replace_regex(
    text,
    r"def _proof_readiness\(.*?\n\n(?=def _has_meaningful_free_signal)",
    new_readiness,
    "preview readiness",
)

new_preview = '''def build_provider_proof_preview(db: Session, settings: Settings) -> dict[str, Any]:\n    """Describe the next provider proof without making any network/provider calls."""\n    targets, scores, signals, blocked_count, context = (\n        _select_provider_summary_targets_with_ranking(db, settings)\n    )\n    cached_targets, cached_combined, _, _, _ = (\n        select_cached_deep_proof_targets_with_ranking(\n            db,\n            settings,\n            context=context,\n        )\n    )\n    commoncrawl: dict[str, int] = context["commoncrawl"]\n    exact_links: dict[str, int] = context["exact_links"]\n\n    # Preview a global queue: cached paid summaries compete with newly queued\n    # names. Fresh names only have free evidence until the bulk summary returns.\n    provisional_scores = dict(cached_combined)\n    for target in targets:\n        provisional_scores[target] = float(scores.get(target, 0.0))\n    provisional_pool = list(dict.fromkeys([*cached_targets, *targets]))\n    provisional_pool.sort(\n        key=lambda target: (-provisional_scores.get(target, 0.0), target)\n    )\n    deep_target_count = min(len(provisional_pool), settings.link_hunter_proof_batch_size)\n    provisional_deep_targets = provisional_pool[:deep_target_count]\n    estimated_max_cost = estimate_provider_proof_max_cost_usd(\n        len(targets), deep_target_count, settings.link_hunter_backlinks_per_domain\n    )\n    max_source_pages = deep_target_count * settings.link_hunter_backlinks_per_domain\n    target_cc = {target: commoncrawl.get(target) for target in targets}\n    target_exact = {target: exact_links.get(target, 0) for target in targets}\n    free_positive_count = sum(\n        1 for target in targets if _has_meaningful_free_signal(signals.get(target, {}))\n    )\n    daily_budget = provider_daily_budget_snapshot(db, settings)\n    work_available_count = len(targets) + len(cached_targets)\n\n    return {\n        "targets": targets,\n        "target_count": len(targets),\n        "summary_targets": targets,\n        "summary_target_count": len(targets),\n        "summary_targets_sample": targets[:10],\n        "cached_deep_target_count": len(cached_targets),\n        "cached_deep_targets_sample": cached_targets[:10],\n        "work_available_count": work_available_count,\n        "deep_proof_target_count": deep_target_count,\n        "provisional_deep_targets": provisional_deep_targets,\n        "selection_strategy": "free_preproof_score",\n        "deep_selection_strategy": "global_cached_summary_winner_queue",\n        "target_free_scores": {target: scores.get(target, 0.0) for target in targets},\n        "target_free_rank_signals": {target: signals.get(target, {}) for target in targets},\n        "known_unavailable_targets_skipped": blocked_count,\n        "backlinks_per_domain": settings.link_hunter_backlinks_per_domain,\n        "max_source_pages": max_source_pages,\n        "estimated_max_cost_usd": estimated_max_cost,\n        "configured_cost_cap_usd": settings.link_hunter_proof_max_cost_usd,\n        "daily_cost_cap_usd": settings.link_hunter_daily_max_cost_usd,\n        "daily_budget": daily_budget,\n        "within_cost_cap": estimated_max_cost <= settings.link_hunter_proof_max_cost_usd,\n        "dataforseo_configured": settings.dataforseo_enabled,\n        "link_hunter_enabled": settings.link_hunter_enabled,\n        "paid_requests_made": 0,\n        "free_exact_link_domain_count": len(exact_links),\n        "free_exact_link_targets": [target for target in targets if exact_links.get(target, 0) > 0],\n        "target_free_exact_links": target_exact,\n        "commoncrawl_signal_count": len(commoncrawl),\n        "commoncrawl_positive_count": sum(1 for value in commoncrawl.values() if value > 0),\n        "commoncrawl_positive_targets": [\n            target for target in targets if (commoncrawl.get(target) or 0) > 0\n        ],\n        "target_commoncrawl_hits": target_cc,\n        **_proof_readiness(\n            settings=settings,\n            targets=targets,\n            work_available=bool(work_available_count),\n            estimated_max_cost_usd=estimated_max_cost,\n            free_positive_count=free_positive_count,\n            daily_budget=daily_budget,\n        ),\n    }\n'''
text = replace_regex(
    text,
    r"def build_provider_proof_preview\(.*\Z",
    new_preview,
    "preview build function",
)
write(path, text)


# ---------------------------------------------------------------------------
# Production proof: first pay for new cheap summaries (if any), then globally
# rerank ALL cached summaries that have not been deep-proved. Free DNS screening
# fills the five paid slots with survivors instead of wasting a slot.
# ---------------------------------------------------------------------------
path = "app/link_hunter.py"
text = read(path)
text = replace_once(
    text,
    '''from app.link_hunter_preview import (\n    rerank_summary_screen_targets,\n    select_provider_summary_targets_with_ranking,\n)''',
    '''from app.link_hunter_preview import (\n    rerank_summary_screen_targets,\n    select_cached_deep_proof_targets_with_ranking,\n    select_provider_summary_targets_with_ranking,\n)''',
    "link hunter winner selector import",
)

new_run = r'''def _cached_summary_payload(db: Session, target: str) -> dict[str, Any]:
    row = db.execute(
        select(BacklinkSummary, Domain)
        .join(Domain, Domain.id == BacklinkSummary.domain_id)
        .where(
            Domain.name == target,
            BacklinkSummary.provider == "dataforseo",
        )
        .limit(1)
    ).first()
    if row is None:
        return {}
    summary, _ = row
    payload = dict(summary.raw_summary or {})
    payload.setdefault("backlinks", int(summary.backlinks or 0))
    payload.setdefault("referring_pages", int(summary.referring_pages or 0))
    payload.setdefault("referring_domains", int(summary.referring_domains or 0))
    payload.setdefault("referring_main_domains", int(summary.referring_main_domains or 0))
    payload.setdefault("rank", float(summary.rank or 0.0))
    return payload


def run_provider_proof(db: Session, settings: Settings) -> dict[str, Any]:
    """Run the cost-capped provider funnel with a permanent global winner queue.

    New names receive the cheap 100-domain bulk summary. Every positive summary
    remains eligible for later detailed proof until it has actually received a
    completed backlinks call, so strong candidates can never be stranded simply
    because four stronger names happened to be in their original batch.
    """
    if not settings.link_hunter_enabled:
        raise DataForSEOError("Link Hunter feature flag is disabled")
    if not settings.dataforseo_enabled:
        raise DataForSEOError("DataForSEO credentials are not configured")

    targets, free_scores, free_signals, _, _ = select_provider_summary_targets_with_ranking(
        db, settings
    )
    counters: dict[str, Any] = {
        "targets": len(targets),
        "summary_targets": len(targets),
        "free_dns_screened": 0,
        "free_dns_blocked": 0,
        "winner_queue_candidates": 0,
        "winner_queue_dns_screened": 0,
        "winner_queue_dns_blocked": 0,
        "summary_screened": 0,
        "summary_domains_with_live_backlinks": 0,
        "deep_proof_target_count": 0,
        "deep_proof_targets": [],
        "summary_calls": 0,
        "backlink_calls": 0,
        "traffic_calls": 0,
        "availability_checks": 0,
        "registrar_checks": 0,
        "registered_or_unavailable": 0,
        "domains_with_live_backlinks": 0,
        "links_saved": 0,
        "source_pages_traffic_checked": 0,
        "source_links_verified": 0,
        "provider_cost_usd": 0.0,
        "cost_cap_hit": False,
        "errors": 0,
        "error_details": [],
    }

    client = DataForSEOClient(settings)
    domain_batches: list[tuple[Domain, Opportunity, list[SourceLink]]] = []
    summary_map: dict[str, dict[str, Any]] = {}

    # Stage 1: only names never summarised before incur the cheap bulk call.
    if targets:
        original_target_count = len(targets)
        targets, dns_blocked = _dns_prefilter_targets(db, targets)
        counters["free_dns_screened"] = original_target_count
        counters["free_dns_blocked"] = dns_blocked
        counters["summary_targets"] = len(targets)
        counters["registered_or_unavailable"] = dns_blocked

    if targets:
        try:
            summary_response = _bulk_provider_call(
                db,
                endpoint="bulk_backlink_summary",
                targets=targets,
                callback=lambda: client.bulk_backlink_summaries(targets),
            )
            counters["summary_calls"] = 1
            counters["provider_cost_usd"] += summary_response.task_cost_usd
            summary_map = {
                _normalize_host(str(item.get("url") or "")): item
                for item in summary_response.result.get("items") or []
            }
            counters["summary_screened"] = len(targets)
            counters["summary_domains_with_live_backlinks"] = sum(
                1
                for target in targets
                if int(summary_map.get(_normalize_host(target), {}).get("referring_pages") or 0) > 0
            )
        except Exception as exc:
            counters["errors"] += 1
            counters["error_details"].append(f"bulk summary: {exc}")
            counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
            return counters

        normalized_summaries = {
            target: summary_map.get(_normalize_host(target), {}) for target in targets
        }
        _, combined_scores, _ = rerank_summary_screen_targets(
            targets,
            free_scores,
            free_signals,
            normalized_summaries,
            0,
        )
        for target in targets:
            domain = _get_or_create_domain(db, target)
            _save_summary_opportunity(
                db,
                domain,
                normalized_summaries.get(target, {}),
                combined_scores.get(target, 0.0),
            )
        db.commit()

    # Stage 2: globally rerank every positive cached summary that has never had
    # detailed proof. The queue survives across batches and restarts.
    winner_targets, winner_scores, winner_summary_scores, winner_free_scores, _ = (
        select_cached_deep_proof_targets_with_ranking(db, settings)
    )
    counters["winner_queue_candidates"] = len(winner_targets)
    if not winner_targets:
        counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
        return counters

    # DNS is free. Look ahead far enough to keep all five paid slots full even
    # when high-ranked cached names have since been registered.
    deep_targets: list[str] = []
    cursor = 0
    dns_candidate_cap = min(len(winner_targets), max(100, settings.link_hunter_proof_batch_size * 20))
    while len(deep_targets) < settings.link_hunter_proof_batch_size and cursor < dns_candidate_cap:
        window_size = min(25, dns_candidate_cap - cursor)
        window = winner_targets[cursor : cursor + window_size]
        cursor += len(window)
        if not window:
            break
        survivors, blocked = _dns_prefilter_targets(db, window)
        counters["winner_queue_dns_screened"] += len(window)
        counters["winner_queue_dns_blocked"] += blocked
        counters["registered_or_unavailable"] += blocked
        for target in survivors:
            if target not in deep_targets:
                deep_targets.append(target)
            if len(deep_targets) >= settings.link_hunter_proof_batch_size:
                break

    deep_targets = deep_targets[: settings.link_hunter_proof_batch_size]
    counters["deep_proof_target_count"] = len(deep_targets)
    counters["deep_proof_targets"] = deep_targets
    counters["deep_proof_scores"] = {
        target: {
            "cached_free_preproof": winner_free_scores.get(target, 0.0),
            "cached_bulk_summary": winner_summary_scores.get(target, 0.0),
            "global_combined": winner_scores.get(target, 0.0),
        }
        for target in deep_targets
    }
    if not deep_targets:
        counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
        return counters

    for target in deep_targets:
        if counters["provider_cost_usd"] >= settings.link_hunter_proof_max_cost_usd:
            counters["cost_cap_hit"] = True
            break
        summary = _cached_summary_payload(db, target)
        if int(summary.get("referring_pages") or 0) <= 0:
            continue
        counters["domains_with_live_backlinks"] += 1
        try:
            backlink_response = _provider_call(
                db,
                endpoint="backlinks",
                target=target,
                callback=lambda target=target: client.backlinks(
                    target, settings.link_hunter_backlinks_per_domain
                ),
            )
            counters["backlink_calls"] += 1
            counters["provider_cost_usd"] += backlink_response.task_cost_usd

            domain = _get_or_create_domain(db, target)
            saved_links: list[SourceLink] = []
            for item in backlink_response.result.get("items") or []:
                if item.get("is_lost"):
                    continue
                link = _save_backlink(db, domain, item)
                if link is not None:
                    saved_links.append(link)
            opportunity = _save_opportunity(db, domain, summary, saved_links)
            db.commit()
            counters["links_saved"] += len(saved_links)

            availability = check_domain(target, settings, exact_registrar_check=False)
            _apply_availability(domain, availability)
            counters["availability_checks"] += 1
            if availability.status == "registered":
                counters["registered_or_unavailable"] += 1
                _score_opportunity(
                    opportunity,
                    domain,
                    saved_links,
                    traffic=0,
                    verified=False,
                    db=db,
                )
                db.commit()
                continue

            domain_batches.append((domain, opportunity, saved_links))
            db.commit()
        except Exception as exc:
            db.rollback()
            counters["errors"] += 1
            if len(counters["error_details"]) < 5:
                counters["error_details"].append(f"{target}: {exc}")

    page_urls = sorted(
        {
            page.url
            for _, _, links in domain_batches
            for link in links
            if (page := db.get(SourcePage, link.source_page_id)) is not None
        }
    )
    traffic_map: dict[str, int] = {}
    if page_urls and counters["provider_cost_usd"] < settings.link_hunter_proof_max_cost_usd:
        try:
            traffic_response = _provider_call(
                db,
                endpoint="bulk_traffic_estimation",
                target=f"{len(page_urls)} source pages",
                callback=lambda: client.bulk_traffic_estimation(page_urls),
            )
            counters["traffic_calls"] = 1
            counters["provider_cost_usd"] += traffic_response.task_cost_usd
            traffic_items = traffic_response.result.get("items") or []
            for item in traffic_items:
                key = _canonical_url(str(item.get("target") or ""))
                if key:
                    metrics = item.get("metrics") or {}
                    organic = metrics.get("organic") or {}
                    traffic_map[key] = max(0, int(round(float(organic.get("etv") or 0.0))))
            for page_url in page_urls:
                page = db.scalar(select(SourcePage).where(SourcePage.url == page_url))
                if page is None:
                    continue
                matching_item = next(
                    (
                        item
                        for item in traffic_items
                        if _canonical_url(str(item.get("target") or ""))
                        == _canonical_url(page_url)
                    ),
                    {"metrics": {}},
                )
                _save_metric_snapshot(db, page, matching_item)
            db.commit()
            counters["source_pages_traffic_checked"] = len(page_urls)
        except Exception as exc:
            db.rollback()
            counters["errors"] += 1
            if len(counters["error_details"]) < 5:
                counters["error_details"].append(f"traffic: {exc}")
    elif page_urls:
        counters["cost_cap_hit"] = True

    for domain, opportunity, links in domain_batches:
        if not links:
            _score_opportunity(opportunity, domain, links, 0, False, db=db)
            db.commit()
            continue

        def _best_link_key(link: SourceLink) -> tuple[int, float]:
            page = db.get(SourcePage, link.source_page_id)
            page_url = page.url if page is not None else ""
            return (
                traffic_map.get(_canonical_url(page_url), 0),
                float(link.provider_rank or 0.0),
            )

        best_link = max(links, key=_best_link_key)
        best_page = db.get(SourcePage, best_link.source_page_id)
        best_traffic = traffic_map.get(_canonical_url(best_page.url if best_page else ""), 0)
        opportunity.best_source_page_id = best_link.source_page_id
        verified = _verify_source_link(
            db,
            best_link,
            domain.name,
            settings.link_hunter_verify_timeout_seconds,
            settings.link_hunter_verification_cache_hours,
        )
        if verified:
            counters["source_links_verified"] += 1
        latest_observation = db.scalar(
            select(LinkObservation)
            .where(LinkObservation.source_link_id == best_link.id)
            .order_by(LinkObservation.observed_at.desc())
            .limit(1)
        )
        clickability_score = float(
            latest_observation.clickability_score if latest_observation is not None else 0.0
        )
        _score_opportunity(
            opportunity,
            domain,
            links,
            best_traffic,
            verified,
            db=db,
            clickability_score=clickability_score,
        )

        if (
            verified
            and opportunity.score >= 45
            and domain.availability_status in {"likely_available", "conflicting", "unknown"}
            and settings.registrar_enabled
        ):
            exact_availability = check_domain(domain.name, settings, exact_registrar_check=True)
            _apply_availability(domain, exact_availability)
            counters["registrar_checks"] += 1
            _score_opportunity(
                opportunity,
                domain,
                links,
                best_traffic,
                verified,
                db=db,
                clickability_score=clickability_score,
            )
        db.commit()

    counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
    return counters


'''
text = replace_regex(
    text,
    r"def run_provider_proof\(.*?\n\n(?=def run_provider_proof_job)",
    new_run,
    "link hunter global winner proof",
)
write(path, text)


# ---------------------------------------------------------------------------
# Run the guarded controller every 15 minutes. The DB ledger still refuses any
# reservation that could cross $2.16/day; the preview now detects that before
# paid mode is toggled on Railway.
# ---------------------------------------------------------------------------
path = ".github/workflows/link-hunter-approved-scheduler.yml"
text = read(path)
text = replace_once(
    text,
    '''    - cron: "43 0,2,4,6,8,10,12,14,16,18,20,22 * * *"''',
    '''    - cron: "*/15 * * * *"''',
    "15-minute approved scheduler",
)
text = text.replace(
    'description="Dispatched guarded batch; caps $0.18/run and $2.16/UTC day"',
    'description="Dispatched winner queue; caps $0.18/run and $2.16/UTC day"',
)
write(path, text)


# ---------------------------------------------------------------------------
# Production workflow understands cached-only work and exits cleanly at the
# daily cap before enabling LINK_HUNTER_ENABLED.
# ---------------------------------------------------------------------------
path = ".github/workflows/link-hunter-production-batch.yml"
text = read(path)
old = '''          cached_count = int(preview.get('cached_deep_target_count') or 0)'''
# Older main does not have this line; patch the existing preview parser directly.
if old not in text:
    text = replace_once(
        text,
        '''          deep_count = int(preview.get('deep_proof_target_count') or 0)\n          daily_cap = float(preview.get('daily_cost_cap_usd') or 0.0)''',
        '''          deep_count = int(preview.get('deep_proof_target_count') or 0)\n          cached_count = int(preview.get('cached_deep_target_count') or 0)\n          work_count = int(preview.get('work_available_count') or 0)\n          daily_cap = float(preview.get('daily_cost_cap_usd') or 0.0)''',
        "production preview cached counters",
    )
text = replace_once(
    text,
    '''              and 0 <= deep_count <= 5\n              and deep_count <= summary_count\n              and cost <= 0.18''',
    '''              and 0 <= deep_count <= 5\n              and deep_count <= work_count\n              and cost <= 0.18''',
    "production preview global deep guard",
)
text = replace_once(
    text,
    '''              'deep_count': deep_count,\n              'cost': cost,''',
    '''              'deep_count': deep_count,\n              'cached_count': cached_count,\n              'work_count': work_count,\n              'cost': cost,''',
    "production preview output counters",
)
text = replace_once(
    text,
    '''          target_count="$(PREVIEW="$preview_summary" python -c 'import json,os; print(int(json.loads(os.environ["PREVIEW"])["target_count"]))')"\n          if [ "$target_count" -eq 0 ]; then\n            finish "success" "No unchecked targets queued; zero paid calls made"\n          fi\n\n          PREVIEW="$preview_summary" python - <<'PY'\n          import json\n          import os\n          payload = json.loads(os.environ['PREVIEW'])\n          assert payload['ready'] is True\n          PY\n          if [ $? -ne 0 ]; then\n            finish "error" "Preview has activation blockers; zero paid calls made"\n          fi''',
    '''          work_count="$(PREVIEW="$preview_summary" python -c 'import json,os; print(int(json.loads(os.environ["PREVIEW"])["work_count"]))')"\n          if [ "$work_count" -eq 0 ]; then\n            finish "success" "No winner-queue work queued; zero paid calls made"\n          fi\n\n          daily_exhausted="$(PREVIEW="$preview_summary" python -c 'import json,os; p=json.loads(os.environ["PREVIEW"]); print("yes" if "daily_budget_exhausted" in p.get("blockers", []) else "no")')"\n          if [ "$daily_exhausted" = "yes" ]; then\n            finish "success" "Daily $2.16 cap reached; winner queue paused until UTC reset"\n          fi\n\n          PREVIEW="$preview_summary" python - <<'PY'\n          import json\n          import os\n          payload = json.loads(os.environ['PREVIEW'])\n          assert payload['ready'] is True\n          PY\n          if [ $? -ne 0 ]; then\n            finish "error" "Preview has activation blockers; zero paid calls made"\n          fi''',
    "production zero-work and daily-cap exit",
)
write(path, text)


# ---------------------------------------------------------------------------
# Dashboard: visible revenue categories and accurate continuous-queue wording.
# Bands intentionally use the high side of the existing modelled range for
# triage, so a $1-$6 case is kept as a small keeper instead of discarded.
# ---------------------------------------------------------------------------
path = "app/templates/dashboard.html"
text = read(path)
macro = '''{% macro revenue_band(high) -%}\n  {% set value = high or 0 %}\n  {% if value >= 50 %}<span class="pill priority">Acquisition priority · $50+/mo</span>\n  {% elif value >= 15 %}<span class="pill qualified">Good earner · $15–$50/mo</span>\n  {% elif value >= 5 %}<span class="pill watchlist">Small keeper · $5–$15/mo</span>\n  {% else %}<span class="pill pending">Micro · under $5/mo</span>{% endif %}\n{%- endmacro %}\n'''
text = replace_once(text, '<!doctype html>\n', macro + '<!doctype html>\n', "dashboard revenue macro")
text = replace_once(
    text,
    '''      <article><span>{{ (next_web_run|dashboard_time).strftime('%H:%M') }}</span><small>Next Web run · {{ (next_web_run|dashboard_time).strftime('%d %b') }} · Prague time</small></article>''',
    '''      <article><span>15 min</span><small>Winner queue cadence · budget-aware continuous processing</small></article>''',
    "dashboard winner cadence",
)
text = replace_once(
    text,
    '''{% if signal %}<strong>{{ "{:,}".format(signal.expected_clicks_monthly) }} expected clicks/mo</strong><small>${{ "%.0f"|format(signal.monthly_revenue_low_usd) }}–${{ "%.0f"|format(signal.monthly_revenue_high_usd) }} modelled revenue · ceiling ${{ "%.0f"|format(signal.max_purchase_price_usd) }} · {{ signal.monetization_route.replace('_', ' ') }}</small>{% else %}''',
    '''{% if signal %}{{ revenue_band(signal.monthly_revenue_high_usd) }}<strong>{{ "{:,}".format(signal.expected_clicks_monthly) }} expected clicks/mo</strong><small>${{ "%.0f"|format(signal.monthly_revenue_low_usd) }}–${{ "%.0f"|format(signal.monthly_revenue_high_usd) }} modelled revenue · ceiling ${{ "%.0f"|format(signal.max_purchase_price_usd) }} · {{ signal.monetization_route.replace('_', ' ') }}</small>{% else %}''',
    "dashboard youtube revenue band",
)
text = replace_once(
    text,
    '''          <p>Permanent web intelligence: free screening → cached backlink evidence → direct link survival → predicted clicks/revenue → human purchase decision. The system never buys automatically.</p>''',
    '''          <p>Permanent web intelligence: free screening → cached backlink evidence → global winner queue → direct link survival → predicted clicks/revenue → human purchase decision. The system never buys automatically.</p>\n          <p><strong>Revenue bands:</strong> Micro under $5 · Small keeper $5–$15 · Good earner $15–$50 · Acquisition priority $50+/month. Bands use the high side of the modelled range for triage, not guaranteed revenue.</p>''',
    "dashboard web revenue legend",
)
text = replace_once(
    text,
    '''                {% if economics %}\n                  <strong>${{ "%.0f"|format(economics.monthly_revenue_low_usd) }}–${{ "%.0f"|format(economics.monthly_revenue_high_usd) }}/mo</strong>''',
    '''                {% if economics %}\n                  {{ revenue_band(economics.monthly_revenue_high_usd) }}\n                  <strong>${{ "%.0f"|format(economics.monthly_revenue_low_usd) }}–${{ "%.0f"|format(economics.monthly_revenue_high_usd) }}/mo</strong>''',
    "dashboard web revenue band",
)
text = replace_once(
    text,
    '''            <strong>Provider ready; safe/off between guarded batches.</strong> The approved twelve-slot controller runs every two hours, enables one capped batch, then automatically disables paid calls and checks production health. The database ledger enforces $0.18/run and $2.16/UTC day.''',
    '''            <strong>Provider ready; safe/off between guarded batches.</strong> The approved winner controller checks every 15 minutes, processes the highest-ranked cached/new candidates first, then automatically disables paid calls and checks production health. The database ledger still enforces $0.18/run and $2.16/UTC day; once the daily reservation cannot fit, the zero-cost preview pauses paid work until reset.''',
    "dashboard controller wording",
)
text = replace_once(
    text,
    '''            {% if proof_preview.target_count %}\n              {{ proof_preview.summary_target_count }} domain{% if proof_preview.summary_target_count != 1 %}s{% endif %} in one cheap summary screen → up to {{ proof_preview.deep_proof_target_count }} deep proofs → up to {{ proof_preview.max_source_pages }} source pages. Conservative maximum spend ${{ "%.4f"|format(proof_preview.estimated_max_cost_usd) }} against a ${{ "%.2f"|format(proof_preview.configured_cost_cap_usd) }} cap.\n              <small>UTC daily ledger: ${{ "%.4f"|format(proof_preview.daily_budget.spent_usd) }} spent + ${{ "%.4f"|format(proof_preview.daily_budget.reserved_usd) }} reserved · ${{ "%.4f"|format(proof_preview.daily_budget.remaining_usd) }} remaining of ${{ "%.2f"|format(proof_preview.daily_budget.limit_usd) }}.</small>\n              <small>Provisional deep-proof leaders: {{ proof_preview.provisional_deep_targets|join(', ') }}. The paid bulk summary reranks these before any detailed backlink call.</small>\n              <small>First summary-screen targets: {{ proof_preview.summary_targets_sample|join(', ') }}{% if proof_preview.summary_target_count > proof_preview.summary_targets_sample|length %}, …{% endif %}</small>\n              <small>{% if proof_preview.within_cost_cap %}✓ preflight is inside the configured cap{% else %}✗ preflight exceeds the cap and the paid proof will refuse to start{% endif %} · paid requests made by this preview: {{ proof_preview.paid_requests_made }}</small>\n            {% else %}\n              No unchecked dropped-domain targets are currently queued for the proof.\n            {% endif %}''',
    '''            {% if proof_preview.work_available_count %}\n              {{ proof_preview.summary_target_count }} new domain{% if proof_preview.summary_target_count != 1 %}s{% endif %} ready for the cheap summary screen + {{ proof_preview.cached_deep_target_count }} cached-summary winner candidate{% if proof_preview.cached_deep_target_count != 1 %}s{% endif %} waiting for deep proof → up to {{ proof_preview.deep_proof_target_count }} deep proofs → up to {{ proof_preview.max_source_pages }} source pages. Conservative maximum spend ${{ "%.4f"|format(proof_preview.estimated_max_cost_usd) }} against a ${{ "%.2f"|format(proof_preview.configured_cost_cap_usd) }} cap.\n              <small>UTC daily ledger: ${{ "%.4f"|format(proof_preview.daily_budget.spent_usd) }} spent + ${{ "%.4f"|format(proof_preview.daily_budget.reserved_usd) }} reserved · ${{ "%.4f"|format(proof_preview.daily_budget.remaining_usd) }} remaining of ${{ "%.2f"|format(proof_preview.daily_budget.limit_usd) }}.</small>\n              <small>Provisional global leaders: {{ proof_preview.provisional_deep_targets|join(', ') }}. Cached summaries compete continuously with fresh discoveries; detailed proof always takes the strongest available names.</small>\n              {% if proof_preview.cached_deep_targets_sample %}<small>Cached winner sample: {{ proof_preview.cached_deep_targets_sample|join(', ') }}{% if proof_preview.cached_deep_target_count > proof_preview.cached_deep_targets_sample|length %}, …{% endif %}</small>{% endif %}\n              {% if proof_preview.summary_targets_sample %}<small>Next new summary targets: {{ proof_preview.summary_targets_sample|join(', ') }}{% if proof_preview.summary_target_count > proof_preview.summary_targets_sample|length %}, …{% endif %}</small>{% endif %}\n              <small>{% if proof_preview.within_cost_cap %}✓ preflight is inside the configured cap{% else %}✗ preflight exceeds the cap and the paid proof will refuse to start{% endif %} · paid requests made by this preview: {{ proof_preview.paid_requests_made }}</small>\n            {% else %}\n              No new summaries or cached winner candidates are currently queued for proof.\n            {% endif %}''',
    "dashboard winner preview",
)
write(path, text)


# ---------------------------------------------------------------------------
# Regression tests for the permanent queue, cached-only cost model, dashboard
# bands and 15-minute controller.
# ---------------------------------------------------------------------------
test_path = ROOT / "tests/test_winner_queue.py"
test_path.write_text(
    '''from pathlib import Path\n\nfrom sqlalchemy import create_engine\nfrom sqlalchemy.orm import Session\n\nfrom app.config import Settings\nfrom app.database import Base\nfrom app.dataforseo import estimate_provider_proof_max_cost_usd\nfrom app.link_hunter_preview import (\n    build_provider_proof_preview,\n    select_cached_deep_proof_targets_with_ranking,\n)\nfrom app.models import BacklinkSummary, Domain, ProviderQuery\n\n\ndef _summary(domain_id: int, *, pages: int, sites: int, rank: float) -> BacklinkSummary:\n    return BacklinkSummary(\n        domain_id=domain_id,\n        provider="dataforseo",\n        backlinks=pages * 2,\n        referring_pages=pages,\n        referring_domains=sites,\n        referring_main_domains=sites,\n        rank=rank,\n        raw_summary={\n            "url": "cached.example",\n            "backlinks": pages * 2,\n            "referring_pages": pages,\n            "referring_domains": sites,\n            "referring_main_domains": sites,\n            "rank": rank,\n        },\n    )\n\n\ndef test_cached_only_deep_proof_has_no_new_summary_cost() -> None:\n    assert estimate_provider_proof_max_cost_usd(0, 5, 25) == 0.1515\n\n\ndef test_cached_summary_stays_in_winner_queue_until_deep_proved() -> None:\n    engine = create_engine("sqlite:///:memory:")\n    Base.metadata.create_all(engine)\n    settings = Settings(link_hunter_proof_batch_size=5)\n    with Session(engine) as db:\n        strong = Domain(name="strong.example")\n        weaker = Domain(name="weaker.example")\n        db.add_all([strong, weaker])\n        db.flush()\n        db.add_all([\n            _summary(strong.id, pages=200, sites=60, rank=75),\n            _summary(weaker.id, pages=20, sites=8, rank=30),\n        ])\n        db.commit()\n\n        names, *_ = select_cached_deep_proof_targets_with_ranking(db, settings)\n        assert names[:2] == ["strong.example", "weaker.example"]\n\n        db.add(ProviderQuery(\n            provider="dataforseo",\n            endpoint="backlinks",\n            target="strong.example",\n            status="complete",\n        ))\n        db.commit()\n        names, *_ = select_cached_deep_proof_targets_with_ranking(db, settings)\n        assert "strong.example" not in names\n        assert names[0] == "weaker.example"\n\n\ndef test_preview_can_run_cached_winner_queue_without_new_summary_targets() -> None:\n    engine = create_engine("sqlite:///:memory:")\n    Base.metadata.create_all(engine)\n    settings = Settings(\n        dataforseo_login="configured",\n        dataforseo_password="configured",\n        link_hunter_proof_batch_size=5,\n        link_hunter_backlinks_per_domain=25,\n        link_hunter_proof_max_cost_usd=0.18,\n    )\n    with Session(engine) as db:\n        domain = Domain(name="winner.example")\n        db.add(domain)\n        db.flush()\n        db.add(_summary(domain.id, pages=80, sites=30, rank=70))\n        db.commit()\n        preview = build_provider_proof_preview(db, settings)\n\n    assert preview["summary_target_count"] == 0\n    assert preview["cached_deep_target_count"] == 1\n    assert preview["work_available_count"] == 1\n    assert preview["deep_proof_target_count"] == 1\n    assert preview["provisional_deep_targets"] == ["winner.example"]\n    assert preview["estimated_max_cost_usd"] > 0\n    assert preview["ready_for_controlled_proof"] is True\n\n\ndef test_controller_and_dashboard_expose_new_behavior() -> None:\n    scheduler = Path(".github/workflows/link-hunter-approved-scheduler.yml").read_text(encoding="utf-8")\n    production = Path(".github/workflows/link-hunter-production-batch.yml").read_text(encoding="utf-8")\n    dashboard = Path("app/templates/dashboard.html").read_text(encoding="utf-8")\n    assert 'cron: "*/15 * * * *"' in scheduler\n    assert "daily_budget_exhausted" in production\n    assert "work_available_count" in production\n    assert "Small keeper · $5–$15/mo" in dashboard\n    assert "Good earner · $15–$50/mo" in dashboard\n    assert "Acquisition priority · $50+/mo" in dashboard\n''',
    encoding="utf-8",
)

print("winner queue v2 patches applied")
