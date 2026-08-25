from __future__ import annotations

from app.acquisition import AcquisitionError, quote_registration, register_domain
from app.config import get_settings

APPROVED_CAPS_CENTS = {
    "satvic.yoga": 2626,
    "teamgerardiperformance.com": 1108,
}


def main() -> int:
    settings = get_settings()
    total = 0
    purchased = []
    for domain, cap in APPROVED_CAPS_CENTS.items():
        try:
            quote = quote_registration(domain, settings)
            if quote.cost_cents > cap:
                raise AcquisitionError(
                    f"Price cap triggered for {domain}: {quote.cost_cents}c > approved {cap}c"
                )
            total += quote.cost_cents
            print(f"APPROVED QUOTE {domain} ${quote.price_usd:.2f} cap={cap}c")
        except AcquisitionError as exc:
            print(f"ABORT {domain}: {exc}")
            return 2

    if total > 3734:
        print(f"ABORT combined total {total}c exceeds approved 3734c")
        return 2

    for domain, cap in APPROVED_CAPS_CENTS.items():
        try:
            result = register_domain(
                domain,
                settings,
                dry_run=False,
                allow_live_purchase=True,
                max_cost_cents=cap,
            )
            purchased.append(domain)
            print(
                f"PURCHASED {result.domain} cost={result.cost_cents}c "
                f"order_id={result.order_id or '-'} balance={result.balance_cents if result.balance_cents is not None else '-'}"
            )
        except AcquisitionError as exc:
            print(f"PURCHASE ERROR {domain}: {exc}")
            # Stop immediately: never continue spending after an unexpected failure.
            return 3

    print(f"PURCHASE COMPLETE count={len(purchased)} total_cap=3734c")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
