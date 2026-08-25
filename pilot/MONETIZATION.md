# Three-site pilot monetization queue

No external offer should be activated without a tracked affiliate/partner URL. Until then, `/go/main` records commercial intent and serves an internal selection guide so traffic is not wasted.

## teamgerardiperformance.com

**Preferred first application: Caliber affiliate program (Impact).**

Why it fits:
- Exact broad intent: online personal training / coaching.
- Independent alternative rather than pretending to continue the former Gerardi offer.
- Official program is designed for publishers/review sites and pays across free-to-paid and coaching conversions.
- Once approved, place the tracked URL in `TEAM_GERARDI_OFFER_URL`.

Operating rule:
- Keep the site clearly independent.
- Do not use Gerardi branding, copy or endorsement language.
- Keep `noindex,follow` during the pilot so the experiment measures surviving inbound links instead of deliberately trying to rank for the active business name.

## craftsheaven.club

**Primary route: woodworking-plan affiliate offer.**

Why it fits:
- Historical deep-link evidence points directly to `/woodworkingplans` and a large woodworking-plan offer.
- Highest-intent landing path should remain usable exactly as an old visitor expects.

Candidates to vet before activation:
- A reputable woodworking-plan / project-plan publisher with a tracked partner program.
- TedsWoodworking via ClickBank is a commercially strong exact-intent candidate, but product/reputation quality should be reviewed before making it the primary recommendation.
- Higher-trust alternatives can be tested against it later.

Once chosen, place the tracked URL in `CRAFTSHEAVEN_OFFER_URL`.

## satvic.yoga

**Primary route: independent yoga membership/course/resource affiliate.**

Why it fits:
- Broad yoga, breathwork and mindful-practice intent.
- Avoid implying affiliation with any particular Satvic/Sattvic school or movement.

Candidates:
- Glo affiliate/partner program if approved.
- Other established yoga subscription/course providers with transparent tracked affiliate terms.

Once approved, place the tracked URL in `SATVIC_YOGA_OFFER_URL`.

## Test sequence

1. Measure inbound sessions before monetisation.
2. Measure internal CTA interest rate while offers are pending.
3. Activate one tracked primary offer per site.
4. Measure outbound CTR, conversions, revenue and net contribution.
5. Only A/B test secondary offers after the baseline period is preserved.
6. Review Day 7 / 15 / 30 / 60 before scaling acquisition volume.
