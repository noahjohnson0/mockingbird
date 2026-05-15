# Dashboard naive-user QA pass

**Tester:** Priya, pretending to be someone's parent
**Target:** `http://mockingbird-pi:8080` (read against `services/dashboard.html`)
**Date:** 2026-05-14
**Persona:** smart but non-technical adult, has never heard of BLE, RSSI, MLE, MAC addresses, or σ. Reading the screen for the first time.

## What I tried, what I expected, what I got

I opened the page. There's a black 3D thing in the middle. On the right there's a panel with three tabs at the top: **Setup**, **Live**, **Stats**. Okay, I'll click Live because I want to see something live.

I see "live · device tracking", a big number, the word "centroid" in blue, and then "—" on the right. I don't know what centroid means. There's a checkbox for "σ ellipsoids" — I have no idea what that is. There's a section called "entities" with a tiny grey line saying "clusters of co-located devices, tightest σ". I read that three times. I'm still confused.

Below that there's a "devices" list with rows like `c4:f3:1e:...` and `−72 dBm` and `3L`. I assume these are phones? But I can't tell which one is mine.

I go back to Setup. I see "room", "path-loss calibration", "calibration anchors", "leaves". The word "leaves" makes me think of trees. I scroll down to a legend at the bottom: "◆ leaves (rotating diamonds)". Okay so a leaf is a sensor. Why is it called a leaf? Nothing on the page tells me.

I click the **3D** button. Nothing visible happens — it was already selected. I click **ortho**. The 3D view shifts a little. I don't know what ortho means. Tooltip says "Orthographic camera (no perspective foreshortening)". I do not know what foreshortening is.

I click "refit from live data (bootstrap)". I don't know what bootstrap means in this context (shoelaces?). The button just says "fitting…" for a while.

## Top 5 most confusing things — bug report format

### 1. "centroid", "σ", "σ ellipsoids", "multilat", "RMSE", "dBm" — jargon with zero plain-English fallback
- **Where:** Live tab summary (`#live-method`, `#live-method-detail`), live toolbar ("σ ellipsoids" checkbox), device rows ("−72 dBm"), calibration summary ("fit · RMSE 3.2 dB").
- **Expected:** As a parent, I'd expect a hover tooltip or a one-line plain-English caption ("how sure we are about each device's position" instead of "σ ellipsoids").
- **Got:** Pure jargon. The σ ellipsoid tooltip *does* exist and reads "1σ uncertainty ellipsoids around each MLE-positioned device" — which has two more pieces of jargon (`1σ`, `MLE-positioned`). The tooltip is making it worse, not better.
- **Severity:** Medium. Power users are fine; first-time viewers bounce.
- **Suggested fix:** Add a tiny "What does this mean?" link next to each technical term, or replace the in-UI label with a plain-English version and put the technical term in the tooltip (inverted from today).

### 2. "leaves" is undefined anywhere on the page
- **Where:** Setup tab `<h2>leaves</h2>`, Stats tab "leaves" section, the 3D scene legend ("◆ leaves").
- **Expected:** Either a glossary entry, a tooltip, or a label like "sensor nodes (leaves)" the first time the word appears.
- **Got:** The word is used as if you already know what it means. The legend says diamonds are leaves but doesn't say a leaf is a Wi-Fi sensor.
- **Severity:** Low for engineers (they know), High for first-time demo viewers — and demos are the moment this matters.
- **Suggested fix:** First mention should be "sensor nodes (we call them *leaves*)". After that the short form is fine.

### 3. The Live tab's big number has no clear meaning out of context
- **Where:** `#live-count` + `.live-count-label` ("devices in view").
- **Expected:** "12 devices in view" — okay, in view of *what*? In view of the room? On the network? Within Bluetooth range?
- **Got:** Just a number and "devices in view". For somebody who didn't read the PRD: am I looking at my house? The street? Why does it say 23 when only 4 people live here?
- **Severity:** Medium. This is the headline number on the most-visited tab and it doesn't explain itself.
- **Suggested fix:** Add a sub-line: "Bluetooth devices currently being heard by ≥2 sensors". The phrase "Apple-fingerprint devices seen by ≥2 positioned leaves in the last 8 s" *does* exist as a sub-header — but it's even more confusing than "devices in view".

### 4. "path-loss calibration" — section name reads like a physics homework problem
- **Where:** Setup tab, the prominent expandable section.
- **Expected:** A friendly button like "Teach the system where you are" or "Calibrate sensors". I'd at least want a "what is this?" prompt.
- **Got:** "path-loss calibration" with sub-text "not calibrated yet — positions use weighted centroid". Two unfamiliar terms in the first eight words. The "auto-detect (click between leaves)" button is the friendliest thing here but it's halfway down the panel.
- **Severity:** Medium-High. Calibration is the *one thing* a non-technical user might actually need to do, and the section is the most intimidating one on the page.
- **Suggested fix:** Rename the section header to something like "Tune the sensors (one-time)" or "Calibrate". Keep "path-loss calibration" as a sub-line for the engineers.

### 5. The 3D view is undiscoverable — I don't know I can interact with it
- **Where:** The big black scene on the left.
- **Expected:** Some visual hint that I can rotate/zoom (a curved arrow icon, a "drag to rotate" overlay on first load).
- **Got:** The legend at the bottom of the Setup pane says `drag rotate · wheel zoom · right-drag pan` in tiny grey text. I missed it the first three times. On the Live tab the legend isn't there at all, so if I land on Live first I have no idea the scene is interactive.
- **Severity:** Medium. The view-cube widget *does* exist but it looks like a static logo until you happen to hover.
- **Suggested fix:** Show a one-time "drag to rotate, scroll to zoom" overlay on the scene that fades after first interaction. Put the legend (or a compact icon version) on all tabs, not just Setup. Also: when I refresh, my last view state should restore — but the bigger fix is just *telling me I can interact with it.*

## Honorable mentions (didn't make top 5)

- "reset names" button on the Live toolbar is right next to two checkbox toggles — clicking it nukes my manual overrides with no confirm. Easy fat-finger destructive action.
- The "auto-detect (click between leaves)" wizard has a 72-px-tall countdown number `—` before you start it. I thought something was broken until I clicked the button.
- Stats tab "obs.sqlite" — the lowercase filename with a dot is a really technical thing to surface as a top-level label. "database size" would be friendlier.
- Tab order is Setup → Live → Stats but a first-time user almost certainly wants to land on Live and only visit Setup if something looks wrong. Worth considering Live as the default tab. (Bia: Sophie said you'd already added "persist active tab across reload" — BIA-4. If the user landed on Live last time they'll come back to Live, which helps. But fresh installs still go to Setup.)

## Engineer-translation summary (TL;DR for Bia)

The dashboard is built for someone who already knows the PRD. For demos and for new household-installer users, the top 4 friction points are:

1. **Jargon density** — invert tooltips: plain English in the label, jargon in the tooltip.
2. **"Leaf" is undefined** — first mention should expand to "sensor nodes (leaves)".
3. **No glossary / "what is this?" affordance anywhere** — even one help icon in the top-right would do it.
4. **3D scene is undiscoverable** — first-load overlay + legend on all tabs.

Filing these as four follow-up tickets to Sophie unless Bia wants to bundle them. Priya will chase Friday.
