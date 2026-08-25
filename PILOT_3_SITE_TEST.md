# Expandosaurus 3-Site Validation Pilot

## Goal
Prove the full commercial chain before scaling acquisition:

`predicted linked exposure -> real inbound visits -> outbound monetisation clicks -> conversions -> revenue`

This pilot is deliberately small. It is not the start of the 1,000-site rollout.

## Hard guardrails
- Maximum 3 domains in the pilot.
- Prefer domains at normal registration pricing; no premium purchases.
- Do not exceed the crawler's calculated max purchase price without an explicit economic reason.
- Reject names with material trademark, active-brand, impersonation, phishing, spam, malware, or reputation risk.
- Do not recreate a prior business in a way that could imply continuity, affiliation, or endorsement.
- Preserve the crawler/provider cost controls already in production.
- Production crawler behaviour remains unchanged by this pilot branch until measurement changes are separately reviewed.

## Original candidates screened out
### satvic.yoga
Reject for the pilot. The exact `Satvic Yoga` identity is actively used by a large yoga brand/channel, including the `@satvic.yoga` handle and Satvic Movement ecosystem. This creates avoidable brand/confusion risk.

### petworthy.co
Reject for the pilot. `PETWORTHY` has a registered international trademark/publication covering commercial marketplace/retail activity related to veterinary products. This is too close to the intended pet monetisation use.

### tapasyavastram.in
Reject for the pilot. The exact domain/name was previously used as an identifiable Shopify clothing business. Avoid using the expired name as a replacement commercial identity without deeper rights/history clearance.

## Replacement candidate set to final-check at registrar
The replacements are intentionally more descriptive/generic and span different expected-strength bands so the pilot calibrates the model rather than cherry-picking only one traffic pattern.

### 1. yogastation.guide
- Current dashboard band: Acquisition Priority
- Projected linked-video exposure: 102,670/month
- Expected clicks: 258/month
- Modelled revenue: $13-$64/month
- Current dashboard availability: likely available (RDAP/DNS)
- Monetisation route: content restore
- Pilot role: high-exposure traffic-validation site

### 2. ultimatevideographer.com
- Current dashboard band: Good Earner
- Projected linked-video exposure: 8,961/month
- Expected clicks: 17/month
- Modelled revenue: $4-$17/month
- Current dashboard availability: available via Porkbun
- Displayed price: $11.08
- Monetisation route: course or lead page
- Pilot role: commercial-intent / course-lead validation

### 3. recipe.how
- Current dashboard band: Micro
- Projected linked-video exposure: 21,842/month
- Expected clicks: 18/month
- Modelled revenue: $1-$4/month
- Current dashboard availability: likely available (RDAP/DNS)
- Monetisation route: content restore
- Pilot role: low-value control / calibration site

## Why include a low-value control?
If all three are top-ranked winners, a good result does not tell us how well the scoring model separates strong from weak opportunities. A deliberately weak third site gives us a useful control. If the model is working, `yogastation.guide` should materially outperform `recipe.how` on inbound and/or monetisable value.

## Required final checks before purchase
For each domain:
1. Registrar availability and live checkout price.
2. Trademark/brand-name collision search.
3. Prior-site identity and purpose review.
4. Search-engine reputation/spam/malware check.
5. Link-context review: why was the domain linked and what visitor intent should we expect?
6. Confirm the proposed landing experience satisfies that intent without impersonating the former owner.
7. Record expected clicks, expected revenue range, purchase ceiling, and acquisition price at the moment of purchase.

Any failed domain is replaced by the next safest candidate; do not force the shortlist.

## Build design
Use one shared lightweight site framework with per-domain configuration rather than three unrelated builds.

Every pilot site must ship with:
- HTTPS and canonical domain handling
- first-party analytics
- inbound landing-path tracking
- outbound-link event tracking
- campaign/offer identifiers
- basic SEO metadata and sitemap where relevant
- privacy/cookie disclosure appropriate to the actual tracking stack
- uptime/error monitoring
- no unnecessary daily AI-content generation

## Measurement schema
Capture at minimum, per domain and per day:
- sessions
- unique visitors
- landing path
- referrer where available
- outbound clicks
- outbound CTR
- conversion count
- gross revenue
- direct variable cost
- net contribution

Also freeze the crawler prediction at acquisition so later recalculation cannot rewrite history:
- predicted monthly exposure
- predicted monthly clicks
- predicted revenue low/high
- buy score
- monetisation route
- purchase ceiling
- acquisition price

## Decision checkpoints
### Day 1
Confirm DNS, HTTPS, analytics and inbound events are working.

### Day 7
Early traffic-direction check. Do not make major conclusions from low sample sizes.

### Day 15
Compare observed inbound rate with the crawler's existing 15-day early signal philosophy. Investigate large prediction errors.

### Day 30
Primary pilot readout:
- predicted vs actual inbound traffic
- outbound CTR
- conversion rate
- revenue
- contribution
- monetisation lessons

### Day 60
Persistence check. Determine whether linked traffic is durable enough to justify broader acquisition.

## Scale gates
Do not jump straight from 3 to mass acquisition.

- If inbound traffic is broadly real but monetisation is weak: improve landing/offer matching and run the test longer.
- If monetisation works but traffic predictions are badly overstated: recalibrate the crawler before scaling.
- If both traffic and monetisation are directionally validated: move to a 10-site cohort.
- After the 10-site cohort validates portfolio economics and automation: move to 20/month, then increase only from measured results.

## Immediate next action
Final-check the three replacement names at a registrar. The only user-required step should be payment/account authentication for domains that pass the checks. Everything else should be prepared around that gate.
