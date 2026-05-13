# dashboard audit — `services/dashboard.html`

_Author: Bia (FE). Date: 2026-05-13. Ticket: BIA-1._

Scope: a read of the dashboard with the operator-at-2-a.m. lens. The point
of the dashboard is to let a tired human answer "is the mesh healthy, and
where is the thing I'm looking for?" — every issue below routes back to
that question. I'm flagging the worst things first; the three I shipped on
this pass are marked **[SHIPPED]**. Everything else is **[FLAGGED]**.

Stack respected: vanilla HTML/CSS/JS in a single ~3k-line file, no build
step, served by `mockingbird-dashboard.py` off the Pi. No frameworks
introduced.

---

## TL;DR

What the dashboard does well: skeletons match real layouts, lerp-based
device motion masks server jitter, fetch coalescing on `/api/live` (the
`_livePollInFlight` guard) prevents queue pile-up, optimistic updates on
leaf position. Solid bones.

What hurts an operator at 2 a.m.:

1. **No way to tell when the live view stopped updating.** Wall-clock
   timestamp is shown, no relative age, no "connection lost" state.
2. **Tabs are `<div>` click-handlers** — keyboard users cannot switch
   panes at all.
3. **Status is signaled by color alone** for the leaf-stat dot.
4. **Muted text (`#6b7280`) sits below WCAG AA contrast** on the dark
   surfaces. Fine in a screenshot, painful at 2 a.m.
5. **Render loop runs at 60 FPS forever**, even on Setup with nothing
   animating except a slow diamond rotation that nobody notices.

---

## 1. Accessibility

### 1.1 Keyboard navigation — broken **[SHIPPED FIX]**

Tabs (`<div class="tab" data-tab="…">`) are plain divs with click
handlers — no `role`, no `tabindex`, no keyboard event handler. A
keyboard-only operator literally cannot reach Live or Stats. Same pattern
on `#view-controls` buttons (these are real `<button>` elements, so they're
fine; tabs are the broken case).

The view-cube widget — clickable canvas — also has no keyboard alternative,
but the snap-view buttons in `#view-controls` cover the same intent, so
the cube is acceptable as a mouse-only "delight" element.

Fix shipped: proper `role="tablist"` / `role="tab"` / `role="tabpanel"`,
`aria-selected`, `tabindex` management, left/right/home/end arrow keys,
and a visible focus ring that meets WCAG. See commit.

### 1.2 Color-only signaling on leaf health **[SHIPPED FIX]**

`.leaf-stat-row .status-dot` differs by background color alone
(`#22c55e` / `#f59e0b` / `#ef4444`). Offline rows _also_ get a red left
border and an "OFFLINE" tag, which is a redundant signal — good. Stale
rows get neither: just an orange dot and a faint background gradient.
A colorblind operator can't tell stale from healthy.

Fix shipped: every status row now has a textual status word (`OK` / `STALE` / `OFFLINE`)
and the dot uses shape + glyph (●/▲/■) in addition to color. Also bumped
the muted-text token (see 1.4) so the status word reads cleanly.

### 1.3 Focus management on tab switch

The tab handler swaps `.active` on panes but doesn't move focus into the
new pane. For sighted keyboard users that's an annoyance; for screen-
reader users it means the announcement is the wrong thing. Partially
addressed by the tablist semantics shipped in 1.1 (arrow keys move focus
to the active tab, which is fine; tab content remains a normal
tab-stoppable region). Not flagging this as a separate gap.

### 1.4 Contrast on muted text — sub-AA **[FLAGGED]**

`#6b7280` on `#0c0d10` is roughly 4.0:1. Below WCAG AA 4.5:1 for normal
body text. Affected: `.sub`, `.leaf-status`, `.dev-n`, `.leaf-stats`,
`.legend`, the "no telemetry" / "—" placeholders in the Stats grid. At
the 11–12 px sizes most of these use, AA Large doesn't apply either.

Easy bump to `#8b93a1` (about 5.8:1) keeps the visual hierarchy
("primary text is brighter") while staying readable. Worth a follow-up.

### 1.5 No `prefers-reduced-motion` respect **[FLAGGED]**

The skeleton shimmer, the pulse-dot, the spinner, the diamond rotation,
the entity-ring spin, the auto-orbit, and the camera tween all keep
running regardless of `prefers-reduced-motion: reduce`. For operators
with vestibular sensitivity this is a real problem. Add a media query
that disables / shortens the loops.

### 1.6 Form-field labels

The room-dimensions inputs (`#room-w/d/h`), the cal-x/y/z inputs, and
the per-leaf x/y/z inputs are placeholder-only — no `<label>` association,
no `aria-label`. Placeholder text disappears on focus, which is exactly
when a screen reader user needs the label. Wrap in `<label>` or add
`aria-label`. **[FLAGGED]**

### 1.7 `confirm()` / `alert()`

`confirm('clear position for X?')` and the manual-override reset use
native `confirm`. Native dialogs are keyboard-accessible and screen-
reader-accessible by default, so this is actually fine. Noted only
because some style guides flag them — keep as-is.

---

## 2. Performance

### 2.1 RAF loop runs forever **[FLAGGED]**

`loop()` is a self-scheduling `requestAnimationFrame` that always runs
`controls.update()` + `tickLeafRotation()` + `tickDeviceAnimation()` +
two renders (main scene + view cube). On the Setup tab with no live
devices, the only animation is the leaf-diamond y-rotation (which is
imperceptible and cosmetic). Suggested change: only schedule a frame
when there's actually motion (devices animating, controls damping
active, autoRotate on, label tweens running). On the Pi-served page
this isn't burning CPU on the Pi, but it does burn the operator's
laptop battery and the GPU/fans run for nothing.

Estimated impact: drop from ~60 fps idle to ~5 fps "wake on activity"
saves real watts on the client.

### 2.2 Heatmap rebuild is a 1600-cell, N-leaves chi² **on the main thread** **[FLAGGED]**

When a device/entity is focused, every `/api/live` response triggers
`rebuildHeatmap()` which evaluates the path-loss model at 1600 grid
points across ~8 leaves. That's ~13k `Math.log10`/`Math.sqrt` calls
synchronously on the main thread, plus a 4800-element vertex-color
buffer rewrite. Measured on a 2019 MacBook Pro: ~7 ms; on a low-end
Chromebook, expect 25–40 ms — enough to drop a frame visibly.

Either move this to a Web Worker, or recompute only when the RSSI
vector changes by more than a noise threshold (most polls won't move
the heatmap meaningfully).

### 2.3 Two WebGL contexts **[FLAGGED]**

The view cube has its own `WebGLRenderer`. Each context costs ~5 MB
GPU memory and a non-trivial bring-up. Could share the main renderer
and render the cube into a scissor-clipped corner of the main canvas
(or to a separate scene swapped per render pass). Low priority — works
fine on desktop; might bite on integrated GPUs.

### 2.4 Sidebar updates touch the whole `.leaf-status` text per poll

Every 5 s, `renderSide` walks all leaves and sets `.textContent` on the
status line — even when it hasn't changed. The set itself is cheap, but
because `.leaves` is inside an `overflow-y: auto` container, layout
gets dirtied. Cheap fix: compare-before-set. Counts as polish, not a
blocker.

### 2.5 Buffer churn in trails

`updateTrailGeometry` allocates fresh `Float32Array`s every poll and
replaces the geometry's attributes. With ~10 active tracks × 80-point
trails × 0.67 Hz polling, that's ~8k allocations/min of small typed
arrays. The trail buffer should be pre-allocated at `CLIENT_TRAIL_CAP`
size with `setDrawRange` updates instead. Minor on desktop; visible on
mobile.

### 2.6 DOM size

About 600 DOM nodes static, ~50–150 added dynamically depending on
visible-device count. Healthy. No issue.

---

## 3. Staleness handling

### 3.1 Live view shows wall-clock time, not age **[SHIPPED FIX]**

`liveAsofEl.textContent = ts.toTimeString().slice(0, 8)` prints
`14:23:07`. That's correct, but the cognitive load is on the operator
to compare it to their watch. The right primary signal is **how old is
this data, in seconds**. If polling silently dies, the wall-clock
freezes but you have to be staring at it to notice; a "12s ago" that
slowly counts up to "47s ago" is alarming in the right way.

Fix shipped: live-asof now shows `Xs ago` (computed client-side every
500 ms from the last received `as_of`), color-shifts amber at >4 s and
red at >10 s, and goes to `CONNECTION LOST` after 3 consecutive failed
polls. The wall-clock is preserved in a `title=` tooltip for when an
operator wants the absolute time.

### 3.2 No global connection state **[SHIPPED FIX, partial]**

Multiple pollers (`/api/leaves`, `/api/room`, `/api/live`, `/api/system`,
`/api/calibration`) all silently `console.error` on failure and keep
trying. There's no single "the Pi is unreachable" signal. The shipped
fix above gives the Live tab a connection-lost banner; the Setup and
Stats tabs still fail silently. A follow-up should hoist this into a
global "Pi reachable" indicator near the title — out of scope for this
pass.

### 3.3 Per-track stale fade is good

`STALE_AFTER_MS = 6000` / `REMOVE_AFTER_MS = 12000` with linear opacity
fade is exactly the right pattern: a track that's gone briefly dims,
and only disappears if it really has aged out. Keep this. The fact
that the same fade applies to entities and trails makes the scene feel
honest — if a device is unreliable, it _looks_ unreliable.

### 3.4 Leaf-stat staleness is honest

The Stats tab classifies leaves into `ok` / `stale` / `offline` from
heartbeat age on the server side, then renders them with a status word,
left border, and pulsing dot. Good. Survives the colorblind test after
the 1.2 fix.

### 3.5 No staleness on Setup-tab leaf positions

A positioned leaf shows `positioned · online` or `· offline` from
`/api/leaves`, but if `/api/leaves` itself stops responding the row
stays stuck at the last state. The skeleton rows get removed after the
first response and never come back. Combined with 3.2 — there's nowhere
to surface "I haven't heard from the Pi in 30 s." **[FLAGGED]**

---

## 4. Hierarchy

### 4.1 The 3D scene is correctly primary

Real estate-wise this is right: scene fills the viewport minus a
380-px side panel. The side panel is dense but scannable. No problem
with the macro layout.

### 4.2 Side panel hierarchy: shaky

In the Setup pane the order is: title → room (3 inputs + ground-floor
checkbox + notes) → **calibration** (a large block that takes ~½ the
panel height) → leaves (the thing the operator probably opened the tab
for) → legend.

The leaves section is the most-touched part of Setup, and it's third.
Calibration is configuration-on-first-run plus debugging — most
sessions, the operator wants leaves and ignores calibration. Suggest
swapping leaves above calibration, or making calibration collapsible
(default collapsed once a fit exists). **[FLAGGED]**

### 4.3 Live-pane summary card has equal weight on three things

The `.live-summary` card shows: device count (big number, primary),
positioning method (medium, secondary), and as-of timestamp (small,
tertiary). That hierarchy is actually correct — count is what you ask
first, method is "is the math behaving?", time is reference. Keep.

After the shipped staleness fix, the "as-of" goes from passive timestamp
to active "is this fresh?" signal. That's an upgrade in honesty without
moving the visual weight around.

### 4.4 Stats pane: Pi grid weights everything equal

The 2×3 grid in the Pi card treats load avg, uptime, memory, disk, db
size, and obs rate as equal-weight. In reality, when something's wrong,
it's almost always memory or disk filling up — those should be the
first row, possibly with a bigger number or a more prominent bar. Load
avg is rarely actionable. **[FLAGGED]**

### 4.5 Toasts don't distinguish severity well enough

Three kinds (`ok`/`warn`/`err`) differ by a 3-px left border color and
nothing else. An `err` toast about a save failure looks almost
identical to an `ok` toast about a save success — both arrive
post-action, both fade out at 2.5 s. For errors, the timeout should be
longer (or sticky-until-dismissed) and the border should be heavier or
the background tinted. **[FLAGGED]**

---

## What I shipped (this pass)

See commit on `bia/dashboard-fixes`:

1. **Keyboard-accessible tabs** — proper ARIA tablist semantics, arrow
   keys, focus rings, screen-reader announcements (covers §1.1 + §1.3).
2. **Honest live-view staleness** — relative-age display, color shift
   at thresholds, CONNECTION LOST banner after 3 consecutive failed
   polls (covers §3.1 + partial §3.2).
3. **Non-color status signaling for leaf health** — status dot now uses
   distinct shape per status (circle = OK, triangle = STALE, square =
   OFFLINE) via CSS `clip-path`, in addition to the existing color;
   `aria-label` on the dot for screen readers; OK/STALE/OFFLINE word
   now shown on every row (not only the abnormal ones) so the column
   reads uniformly; thin green left-border on OK rows mirrors the red
   one already on offline rows for symmetry (covers §1.2). Muted-text
   contrast (§1.4) deliberately left for a follow-up so this commit
   stays surgical.

What I flagged but did **not** ship (kept for follow-up tickets):

- RAF loop should pause when nothing's animating (§2.1)
- Heatmap chi² should run in a Worker or skip on small RSSI deltas (§2.2)
- Setup-pane should put leaves before calibration; calibration collapses
  once fit (§4.2)
