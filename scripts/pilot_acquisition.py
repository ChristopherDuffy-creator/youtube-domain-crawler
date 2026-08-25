from __future__ import annotations

import argparse
import sys

from app.acquisition import AcquisitionError, PILOT_DOMAINS, quote_registration, register_domain
from app.config import get_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quote or register the approved 3-site pilot domains")
    parser.add_argument(
        "--mode",
        choices=("quote", "dry-run", "live"),
        default="dry-run",
        help="quote only, Porkbun dry-run, or live registration",
    )
    parser.add_argument(
        "--approve-live-purchase",
        action="store_true",
        help="required together with --mode live",
    )
    parser.add_argument(
        "domains",
        nargs="*",
        default=sorted(PILOT_DOMAINS),
        help="optional subset of the approved pilot allowlist",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    domains = args.domains or sorted(PILOT_DOMAINS)

    if args.mode == "live" and not args.approve_live_purchase:
        print("Live purchase blocked: pass --approve-live-purchase after final approval.", file=sys.stderr)
        return 2

    failed = False
    total_cents = 0
    for domain in domains:
        try:
            quote = quote_registration(domain, settings)
            total_cents += quote.cost_cents
            print(f"QUOTE {quote.domain} ${quote.price_usd:.2f} ({quote.cost_cents}c)")
            if args.mode == "quote":
                continue

            result = register_domain(
                domain,
                settings,
                dry_run=args.mode != "live",
                allow_live_purchase=args.mode == "live" and args.approve_live_purchase,
                max_cost_cents=quote.cost_cents,
            )
            mode_label = "DRY-RUN" if result.dry_run else "PURCHASED"
            print(
                f"{mode_label} {result.domain} cost={result.cost_cents}c "
                f"order_id={result.order_id or '-'}"
            )
        except AcquisitionError as exc:
            failed = True
            print(f"ERROR {domain}: {exc}", file=sys.stderr)

    print(f"TOTAL QUOTED ${total_cents / 100:.2f}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
