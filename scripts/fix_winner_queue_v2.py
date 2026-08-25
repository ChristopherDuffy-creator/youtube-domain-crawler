from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected one match, found {text.count(old)}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


# Preserve the pre-existing newest-first order when fresh targets have equal
# free evidence, while still allowing cached winners to outrank them.
replace_once(
    "app/link_hunter_preview.py",
    '''    provisional_pool = list(dict.fromkeys([*cached_targets, *targets]))
    provisional_pool.sort(
        key=lambda target: (-provisional_scores.get(target, 0.0), target)
    )
''',
    '''    provisional_pool = list(dict.fromkeys([*cached_targets, *targets]))
    provisional_position = {target: index for index, target in enumerate(provisional_pool)}
    provisional_pool.sort(
        key=lambda target: (
            -provisional_scores.get(target, 0.0),
            provisional_position[target],
        )
    )
''',
    "winner preview tie break",
)

# Do every free DNS rejection before even constructing the paid provider
# client. This preserves the zero-paid-call safety contract for live domains.
replace_once(
    "app/link_hunter.py",
    '''    client = DataForSEOClient(settings)
    domain_batches: list[tuple[Domain, Opportunity, list[SourceLink]]] = []
''',
    '''    client: DataForSEOClient | None = None
    domain_batches: list[tuple[Domain, Opportunity, list[SourceLink]]] = []
''',
    "delay provider client construction",
)
replace_once(
    "app/link_hunter.py",
    '''    if targets:
        try:
            summary_response = _bulk_provider_call(
''',
    '''    if targets:
        client = DataForSEOClient(settings)
        try:
            summary_response = _bulk_provider_call(
''',
    "construct provider for new summary work",
)
replace_once(
    "app/link_hunter.py",
    '''    if not deep_targets:
        counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
        return counters

    for target in deep_targets:
''',
    '''    if not deep_targets:
        counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
        return counters

    if client is None:
        client = DataForSEOClient(settings)

    for target in deep_targets:
''',
    "construct provider for cached-only deep work",
)

# These assertions described the old two-hour/twelve-slot behavior. Update them
# to protect the new continuous winner queue instead of forcing stale wording.
replace_once(
    "tests/test_dashboard_system_views.py",
    '    assert "Next Web run" in template\n',
    '    assert "Winner queue cadence" in template\n    assert ">15 min<" in template\n',
    "dashboard cadence regression",
)
replace_once(
    "tests/test_link_hunter_approved_scheduler.py",
    '    assert \'cron: "43 0,2,4,6,8,10,12,14,16,18,20,22 * * *"\' in scheduler\n',
    '    assert \'cron: "*/15 * * * *"\' in scheduler\n',
    "scheduler cadence regression",
)
replace_once(
    "tests/test_link_hunter_dashboard_proof_status.py",
    '    assert "approved twelve-slot controller" in text\n',
    '    assert "approved winner controller checks every 15 minutes" in text\n',
    "dashboard controller wording regression",
)
replace_once(
    "tests/test_link_hunter_production_batch_workflow.py",
    '    assert "No unchecked targets queued; zero paid calls made" in text\n',
    '    assert "No winner-queue work queued; zero paid calls made" in text\n    assert "daily_budget_exhausted" in text\n',
    "production zero-work wording regression",
)

print("winner queue integration regressions fixed")
