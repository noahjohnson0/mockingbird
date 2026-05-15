# Mockingbird commercialization wedge — 2026-05-14

**Author:** Mara Solberg (Head of BD)
**Audience:** Noah
**Status:** pressure-test, not a recommendation memo for outside readers
**Frame:** if we had to pick ONE wedge to chase for the next 12 months, no investor pressure, what's it?

---

## Ground-truth check before sizing

Before I rank wedges, the honest state of the asset:

- We have a **working capability**, not yet a product. 8 leaves, one home, one operator (Noah), no second deployment, no install procedure, no commissioning UX, no second-site calibration story, no SLA, no support model.
- Unit cost of a "site": ~10 × $5 ESP32 ($50) + Pi ($35) + Opal ($65) + power/cabling + ~4–8 hours of skilled commissioning. BOM is trivial; **labor and calibration are the cost center.**
- Differentiator that's real: **multi-MAC entity clustering survives Apple Continuity rotation.** Most cheap indoor-BLE stacks can't do this. That's the technical moat worth selling against.
- Differentiator that is NOT yet real: room-level accuracy guarantees, multi-tenant data isolation, fingerprinting that holds up across households, anomaly alerts that don't false-positive into uselessness.
- **No paying customer. One inbound from a security integrator** per the brief — that's a signal, not a pipeline.

Pricing the asset honestly upstream of this exercise is the difference between picking a wedge and picking a fantasy.

---

## Candidates

### 1. Indoor positioning as a service — multi-unit residential property managers

- **TAM signal:** Weak-to-moderate. Property managers buy access control, smart locks, leak sensors, package lockers. They do *not* historically buy "where are people inside the building" — that's a privacy land mine in residential. The buyer pain ("how do I know if a unit is occupied/abandoned/being subletted") exists but has cheaper proxies (utility data, smart-lock logs).
- **Buyer profile:** Regional property management company COO or head of operations. Long approval chain through legal (resident privacy, fair housing).
- **Sales cycle reality:** 6–9 months minimum. Pilot → legal review → board approval → rollout. Channel through prop-tech distributors realistic but slow. Single-property pilots are cheap to get; multi-property contracts are where the money is and the gating is brutal.
- **What would have to be true:** A specific high-pain workflow (unauthorized subletting? unit-turn verification?) where our data is the cheapest evidence available, AND a privacy story that survives legal review (resident opt-in, MAC-only, no PII).
- **Biggest reason to skip:** Privacy + fair-housing exposure on residential tracking is severe and unsexy. One bad press cycle ends the company.

### 2. Occupancy analytics for commercial real estate

- **TAM signal:** Strongest of the bunch on paper. Post-2020 hybrid-work CRE has a real budget line for "do my employees actually use this space" — VTS, Density, XY Sense, Spaceti, Disruptive Tech, Butlr all live here. Density alone has raised >$200M. The category is *validated*, which cuts both ways: real buyers exist; we are not the first vendor in the room.
- **Buyer profile:** Head of Workplace / Real Estate at a Fortune-2000, or facilities director at a flex-space operator (Industrious, Convene, WeWork survivors). Champion is usually a workplace-experience analyst; economic buyer is the CRE/CFO axis.
- **Sales cycle reality:** 4–9 months. Pilot one floor → measure against badge data → scale to a portfolio. Procurement is professional and slow but the contract sizes are real ($30–150k ARR per building, more for portfolios).
- **What would have to be true:** We can credibly position against Density ($$ ceiling-mounted depth sensors, room-level), Butlr (thermal, anonymized), and the badge-data status quo. Our anti-pitch: **BLE entity clustering tells you "this is the same person across 4 rooms over 3 hours" — utilization, not just headcount.** That's a real differentiator IF we can prove it scales past one home.
- **Biggest reason to skip:** Incumbents are well-funded and the BLE-based players (Cisco DNA Spaces, Aruba Meridian) bundle indoor location into the WiFi infrastructure already paid for. We have to explain why a separate sensor mesh beats "free with your WiFi controller." That's a hard slide.

### 3. "Smart presence" for HomeKit / Home Assistant prosumer

- **TAM signal:** Weak commercially, strong as a community/distribution play. HA users buy $100–500 of gear gladly. ~500k active HA installs globally. If we shipped a $199 starter kit and converted 1% → $1M revenue, but that's a hobby business not a company.
- **Buyer profile:** Self. Same buyer as user. Forum-driven word-of-mouth.
- **Sales cycle reality:** Days. Reddit/HA forums/YouTube → Shopify checkout. No sales team needed. Lovely.
- **What would have to be true:** Hardware margin survives Shopify + fulfillment + 15% returns + support tickets. Honestly questionable at $199 with our BOM. AND we're competing with ESPresense (free, open-source, BLE-based, has the HA mindshare). We'd need to be *meaningfully* better, not just polished.
- **Biggest reason to skip:** This is a brand-building and community-building exercise that *looks* like revenue. Worth doing as a side-channel for credibility/recruiting/data, NOT as the wedge. Also: support burden of consumer hardware will eat the team alive.

### 4. White-label sensing kit for security integrators (the inbound)

- **TAM signal:** Moderate. The integrator channel (ADT, Vector, regional commercial integrators) is hungry for differentiated sensors they can roll into existing contracts. Margins on integrator-channel hardware are real (40–60% to integrator, 30–40% to us).
- **Buyer profile:** Two-step: (a) the integrator's product/procurement lead picks us as a SKU; (b) the integrator's salesperson sells our box into their end customer alongside cameras/alarms. We never meet the end customer. **The integrator is the customer.**
- **Sales cycle reality:** Long first sale (3–6 months to get on an integrator's line card), short *subsequent* sales (they sell it for us). Once a single regional integrator adopts and has a working install playbook, the unit economics compound.
- **What would have to be true:** The inbound is a real procurement conversation, not a "cool, send us a demo unit" tire-kicker. We need to know: do they have a *specific* end-customer pain they're trying to solve, or are they shopping? We also need a story for **install time** — integrators bill labor; if our calibration eats 8 hours per site, they won't carry it. Our adaptive auto-calibration roadmap is directly load-bearing here.
- **Biggest reason to skip:** Integrator-channel businesses are slow to ramp and we lose the direct customer feedback loop that makes a product great. We'd be building to an integrator's spec, not an end-user's pain.

### 5. B2B sale to elder-care / aging-in-place

- **TAM signal:** Real and growing — assisted-living facilities, home-care agencies, "aging at home" services. The pain is concrete: falls, wandering, missed routines, "is grandma OK." Average revenue per resident in assisted living is high enough to absorb $20–50/month of sensing.
- **Buyer profile:** **Two very different buyers.** (a) Facility operator (head of nursing / operations at an ALF chain) — buys for liability reduction, staff efficiency. (b) Adult child of aging parent — buys for peace of mind, $50–100/month willing. The B2B path is bigger but slower; the B2C path is faster but is a different company.
- **Sales cycle reality:** Facility chains: 9–18 months, regulated (HIPAA-adjacent even if we never touch health data), procurement-heavy, requires nursing-staff training. Direct-to-family: weeks, but high CAC and product needs to be self-installable.
- **What would have to be true:** Our data has to support **specific clinical-ish signals** — fall events, ambulation patterns, sundowning behavior, routine deviation. Our presence/anomaly-alerts PRD is exactly this, which is interesting. We'd need clinical advisors and probably a partnership with an EHR/RMS in the ALF space.
- **Biggest reason to skip:** Regulated buyer, long cycle, and we'd be one of many "passive sensing for elder care" vendors (CarePredict, Caraway, MedSign, etc.). Our BLE-entity-clustering advantage is less obvious here than computer vision or radar — neither of which we do.

### 6. Research / government applied-RF contracts

- **TAM signal:** Real but niche. DARPA, IARPA, DHS S&T, NIST, academic RF labs all spend on novel indoor-RF work. SBIR Phase I = $50–250k, Phase II = $750k–$2M.
- **Buyer profile:** Program manager at a federal lab, or PI at a university partner. They buy *novelty and IP*, not products.
- **Sales cycle reality:** SBIR timelines are 6–12 months from BAA → award. Once you're in, very sticky.
- **What would have to be true:** We'd pivot the team's identity from "product company" to "applied-research shop." Anthony might love that for a quarter; it kills the option to build a real product company because the incentives are entirely different. Government contracts also pay slowly and require security clearances for the interesting work.
- **Biggest reason to skip:** This funds engineering but it doesn't build a business. Easy to fall in love with the non-dilutive money and wake up 3 years later as a consultancy.

---

## My call

**Top recommendation: #2, occupancy analytics for CRE, with the integrator inbound (#4) as a parallel low-effort track to validate the unit economics question.**

Reasoning, plainly:

- **#2 is the only candidate where (a) the buyer category already buys this kind of thing, (b) our specific technical differentiator (entity clustering across MAC rotation = real utilization, not just counts) maps to a real buyer-language outcome ("we can tell you how a single employee uses your floor, not just how many bodies are in it"), and (c) deal sizes justify the sales motion.**
- It is NOT the easiest wedge. Density and friends are well-funded and well-known. We win by being the cheap, sensor-light, BYO-deploy option for mid-market CRE that can't afford a Density rollout — *if* we can prove the data is good enough. Pricing positioning: $8–15k/floor/year as the "good-enough" tier vs. Density's $50–100k.
- #4 (integrator inbound) runs in parallel because (a) it's already on our desk and we'd be stupid to ignore an inbound, (b) it stress-tests our install/commissioning story which #2 also needs, and (c) integrator-channel revenue, if it materializes, funds the slower CRE motion. **But I am not building a roadmap around #4 until I know whether this single inbound represents a real opportunity or a curious procurement person.**
- #5 (elder-care) is the consolation prize if #2 hits a wall — same sensing primitives, different buyer story. Keep it warm, don't chase it yet.
- #1, #3, #6: skip as wedges. #3 is worth doing as a marketing/community side-channel (open-source-friendly HA integration). #6 is worth doing if-and-only-if a specific PM hands us a fully-baked opportunity.

**What would change my mind:** If 2 of the first 5 discovery calls (below) say "we already have Density / Cisco DNA Spaces and it's fine," #2 is dead and we should look harder at #5.

---

## Next discovery move: 5 calls, one question each

Goal of this batch: pressure-test #2 (CRE occupancy) hard, and in parallel run a single qualifying call on the inbound. **One sharp question per call — the rest of the conversation is listening.** I keep verbatim notes.

1. **Head of Workplace at a Fortune-500 with hybrid-work policy** (Priya can source from her network).
   - Question: *"Walk me through the last time you made a real-estate decision — keep a floor, give up a floor, redesign a space. What data did you actually use, and what data did you wish you had?"*
   - What I'm listening for: do they cite badge data, sensor data, or vibes? Is "individual utilization vs. headcount" a distinction they care about, or is headcount enough?

2. **Facilities director at a flex-space / coworking operator** (Industrious, Convene, regional).
   - Question: *"If I could tell you, room by room, which members are repeat users of which spaces — not just counts but identity-stable patterns — what would you do with that, and would your CFO pay for it?"*
   - What I'm listening for: do they light up at "identity-stable" or do they say "we already know that from check-ins"? Tells me if our differentiator matters or is invisible.

3. **The security integrator that inbounded.** Whoever sent the inbound.
   - Question: *"What end-customer are you trying to win with this, and what's the specific pain in their current install that you think mockingbird solves?"*
   - What I'm listening for: a named end-customer and a named pain = real. "We're building a portfolio of differentiated sensors" = tire-kicker. Either answer is useful; I need to know which it is before any more time goes into them.

4. **CRE broker or tenant rep at a JLL/Cushman/CBRE-tier firm.**
   - Question: *"When your tenants ask 'do we need this much space,' how does that conversation go and who, if anyone, brings data?"*
   - What I'm listening for: is there a data-buying motion in the *brokerage* layer (i.e., could we sell to brokers who resell to tenants)? This is an alternative GTM I want to learn about before committing to direct-to-tenant.

5. **Someone who already deployed Density or Butlr** — even informally, a friend of a friend.
   - Question: *"What did the deal actually look like — pilot scope, ramp, what the renewal conversation was about — and what do you wish was different about the product?"*
   - What I'm listening for: the unsexy truths about incumbents that we can position against. Renewal-conversation gripes are the most valuable signal in B2B.

**Threshold for "real":** 3 of 5 calls produce a named workflow + a stated willingness-to-pay range. If that happens, we draft a one-page positioning document and a pilot-to-contract graduation criterion (see [[pilot-graduation-criteria]] if/when that memory exists), and pick a first design-partner target. If it doesn't happen, we re-rank.

**Timeline:** I can have 5 calls booked within 2 weeks if I start sourcing tomorrow. Verbatim notes back to the team within 24h of each call. Synthesis memo by 2026-06-15.

---

## What I'm NOT doing

- Pricing anything. Pricing comes after positioning, which comes after discovery. Anyone who asks me for a price sheet before then gets a polite "let me show you the value first."
- Building a deck. A deck without a customer is a moodboard.
- Promising engineering anything customer-shaped this quarter. Q3 PRDs are locked, and they happen to be the right capabilities for #2 anyway (entity clustering hardening, presence/anomaly alerts) — let's not muddy them with bespoke customer asks until a deal is real.
