# Expandosaurus 3-Site Validation Pilot

## Goal
Prove the full commercial chain before scaling acquisition:

`predicted linked exposure -> real inbound visits -> outbound monetisation clicks -> conversions -> revenue`

This pilot is deliberately small. It is not the start of the 1,000-site rollout.

## Hard guardrails
- Maximum 3 domains in the pilot.
- User is the final acquisition gate and has manually selected the pilot cohort.
- Prefer domains at normal registration pricing; no premium purchases unless manually approved.
- Do not exceed the crawler's calculated max purchase price without an explicit economic reason.
- Flag material trademark, active-brand, impersonation, phishing, spam, malware, or reputation risk for the user's final decision rather than automatically rejecting descriptive/generic names.
- Do not recreate a prior business in a way that could imply continuity, affiliation, or endorsement.
- Preserve the crawler/provider cost controls already in production.
- Production crawler behaviour remains unchanged by this pilot branch until measurement changes are separately reviewed.

## Locked pilot cohort

### 1. satvic.yoga
- Current dashboard band: Acquisition Priority
- Projected linked-video exposure: 85,073/month
- Expected clicks: 176/month
- Modelled revenue: $32-$141/month
- Current dashboard availability: available via Porkbun
- Displayed price: $26.26
- Monetisation route: affiliate landing
- Pilot role: strong yoga/wellness traffic and affiliate-intent validation
- Naming note: `satvic/sattvic` is a traditional descriptive yoga concept. Avoid copying Satvic Movement branding, trade dress, content, or implying affiliation.

### 2. petworthy.co
- Current dashboard band: Acquisition Priority
- Projected linked-video exposure: 37,708/month
- Expected clicks: 137/month
- Modelled revenue: $25-$110/month
- Current dashboard availability: likely available (RDAP/DNS)
- Monetisation route: affiliate landing
- Pilot role: commercial pet-intent validation
- Naming note: prior Pet Worthy activity appears defunct, but an exact PETWORTHY mark exists in a pet/veterinary commercial class. User has chosen the domain as final acquisition gate; avoid presenting the pilot as the former business or as affiliated with another PETWORTHY rights holder.

### 3. craftsheaven.club
- Pilot role: explicit deep-link / offer-intent validation
- Historic link evidence found pointing specifically to `craftsheaven.club/woodworkingplans` and describing an offer for "16,000 woodworking plans".
- This gives the pilot a clear original landing-path and visitor-intent hypothesis rather than a generic homepage test.
- At acquisition, freeze the crawler's current exposure/click/revenue/buy-score metrics from the production dashboard before any later recalculation.
- Do not copy any prior site's protected content or imply continuity with the former operator.

## Why this cohort is useful
The three sites test different monetisation/intent patterns:
- `satvic.yoga`: wellness/affiliate intent with strong projected traffic.
- `petworthy.co`: pet/tutorial traffic with straightforward commercial affiliate potential.
- `craftsheaven.club`: an unusually explicit historical deep link and offer intent (`/woodworkingplans`), useful for testing whether restoring visitor intent improves conversion.

## Required final checks before purchase
For each domain:
1. Registrar availability and live checkout price.
2. Record any material brand/trademark caveat for the user's final acquisition decision.
3. Prior-site identity and purpose review.
4. Search-engine reputation/spam/malware check.
5. Link-context review: why was the domain linked and what visitor intent should we expect?
6. Confirm the proposed landing experience satisfies that intent without impersonating the former owner.
7. Record expected clicks, expected revenue range, purchase ceiling, and acquisition price at the moment of purchase.

If a domain is no longer available or has a serious technical/reputation problem, return it to the user rather than silently substituting another domain.

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

For historic deep links such as `/woodworkingplans`, preserve the inbound path or use a semantically correct internal redirect so the original link does not land on an irrelevant generic homepage.

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
Final-check live registrar availability/price for `satvic.yoga`, `petworthy.co`, and `craftsheaven.club`. The user performs checkout/payment. Once acquired, configure DNS and deploy the shared pilot framework with predictions frozen at purchase.
