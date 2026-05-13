# Propagation physics review — mockingbird BLE RSSI positioning

*Author: Dr. Vlad Moskovavich*
*Scope: review of the propagation model implicit in `services/mockingbird_calibration.py` and `services/mockingbird_tracks.py`, and what physics says about achievable accuracy.*

---

Ah, here we are. Eight little ESP32 boards on the wall, 2.4 GHz, BLE
advertisements, and an ambition to localize phones to sub-foot accuracy.
The team has done good engineering — robust MLE, IRLS, Huber loss, entity
Kalman, EWMA on the fingerprints. Now let us see what physics has to say
about whether this can keep working, and where the floor lies.

I will not be polite about this. The team needs to know what is fundamental
and what is a software bug.

---

## 1. What is RSSI, physically?

Start from Friis. Two antennas in free space, separated by distance \(d\),
operating at wavelength \(\lambda\). The received power is

$$
P_r = P_t \, G_t \, G_r \, \left( \frac{\lambda}{4 \pi d} \right)^2
$$

In dB form, this is the cleaner thing to look at:

$$
P_r[\mathrm{dBm}] = P_t[\mathrm{dBm}] + G_t[\mathrm{dBi}] + G_r[\mathrm{dBi}] - 20 \log_{10}(d) - 20 \log_{10}(f) - 32.44
$$

with \(d\) in meters and \(f\) in MHz. Plug in BLE: \(f = 2440\) MHz, so
the constant becomes \(-40.2\) dB at \(d = 1\) m. A typical BLE advertiser
transmitting at 0 dBm into an antenna with \(G_t \approx 0\) dBi, received
by a PCB antenna with \(G_r \approx 0\) dBi (more on this fiction later)
gives expected RSSI at 1 m of about \(-40\) dBm. In free space.

**RSSI is the receiver's digital-domain estimate of the integrated power
in the received passband during the packet preamble.** It is a single
scalar per packet. It collapses everything — the magnitude squared of a
complex baseband signal that itself is the coherent sum of *every*
multipath component reaching the antenna — into one number, quantized to
1 dB by the radio's RSSI register. Phase is gone. Polarization is gone.
Angle of arrival is gone. You see a number, that is all.

Why does the indoor measurement deviate from Friis? Five reasons, in
order of magnitude:

1. **Multipath.** The transmitter radiates in all directions, the
   receiver collects energy via many paths (direct, plus reflections off
   walls, floor, ceiling, your refrigerator, your body). The complex
   baseband sum has random phases. It can constructively add (you get
   *more* than free-space RSSI — yes, really) or destructively cancel
   (you get a deep null, 20+ dB below Friis). This is fading. It changes
   when the geometry changes by \(\lambda/4 \approx 3\) cm.
2. **Obstructions / shadowing.** Drywall ≈ 3 dB. Brick ≈ 6 dB. A human
   body ≈ 5–10 dB. A refrigerator ≈ 20+ dB. These are large-scale
   log-normal effects on top of the small-scale fading.
3. **Antenna pattern non-isotropy.** The ESP32 PCB antenna is a meandered
   inverted-F, not a dipole. Its pattern in the plane of the PCB has 5–8
   dB of asymmetry, and a null off the long axis. The board's
   orientation matters. (See section 5.)
4. **Frequency-dependent effects.** BLE channel-hops across 40 channels
   spanning 2402–2480 MHz. Different channels see different multipath
   geometries — the constructive/destructive interference pattern is a
   function of wavelength. Same room, same geometry, different channel:
   different RSSI by up to 10 dB.
5. **Receiver chain non-idealities.** RSSI calibration of the ESP32
   radio is approximate; the AGC, mixer non-linearities, and digital
   filter response introduce a few dB of bias that varies *per chip*.
   This is the "RX bias" the team is already correcting for. Good.

So when the code writes `RSSI = P0 - 10·n·log10(d)`, it is fitting a
model where (1) and (2) are absorbed into the slope \(n\), and where (3),
(4), (5) get pushed into the residual variance \(\sigma\). That is the
honest reading of what the code is doing.

---

## 2. Path loss model: what is the code using, and is it right?

The code uses **log-distance path loss**:

$$
\overline{\mathrm{RSSI}}(d) = P_0 - 10 \, n \, \log_{10}(d)
$$

where \(P_0\) is the mean RSSI at \(d = 1\) m and \(n\) is the path-loss
exponent. This is the right *family* of models for this problem. It is
empirical, not derived — there is no derivation of \(n\) from first
principles in a residential indoor environment. \(n\) is a fit parameter
that absorbs the average attenuation contribution of the environment.

Typical values from the literature for 2.4 GHz indoors:

| environment              | \(n\)        |
|--------------------------|--------------|
| free space (Friis)       | 2.0          |
| open-plan office, LOS    | 1.8–2.2      |
| residential LOS          | 2.0–2.8      |
| residential, mixed LOS/NLOS | 2.8–3.5   |
| residential, NLOS through walls | 3.5–5.0 |
| dense office, NLOS       | 4.0–6.0      |

The code clamps \(n \in [2.0, 4.5]\) (or \([1.8, 5.0]\) for the anchor
path). This is reasonable. For a *single* residential room with
line-of-sight between most leaves and the device, \(n \approx 2.5\) is
the right ballpark. For an apartment-scale deployment with the device
roaming behind walls, \(n \approx 3.0\text{–}3.5\) is more honest.

**But here is the deeper issue: a single global \(n\) is not the right
model.** Indoor propagation is *piecewise*: free-space-ish on the
direct LOS segment, then a discrete loss event when the path crosses a
wall. The right model — if you want to get serious — is

$$
\overline{\mathrm{RSSI}}(d) = P_0 - 10 \, n_0 \, \log_{10}(d) - \sum_k W_k(\mathrm{path})
$$

where \(W_k\) are per-wall attenuation terms (the "Motley-Keenan" or
"COST 231 multi-wall" model). With only 8 leaves and no floor plan input,
fitting wall positions is over-parameterized — the team is right to
stick with log-distance. The compensation the team has already built
(per-leaf \((P_0^i, n_i)\) + TX/RX bias decomposition) is approximately
absorbing wall effects into per-leaf parameters when the device-to-leaf
path is dominated by a wall. This is a clever pragmatic move and I
endorse it.

**Verdict: log-distance with per-leaf \(n_i\) clamped to indoor range is
the right model for this hardware budget.** The team should *not* try to
move to multi-wall without a floor plan as input.

---

## 3. Multipath and small-scale fading — why a stationary device shows 15 dB swings

Now we get to the part that physics does not let you escape.

The received complex amplitude at the antenna is

$$
r(t) = \sum_{k} a_k(t) \, e^{j \phi_k(t)}
$$

where \(k\) indexes propagation paths (direct, single-bounce off each
surface, double-bounce, etc.) and the phases \(\phi_k = 2\pi d_k / \lambda\)
are extremely sensitive to path-length changes. Move the device by
\(\lambda/2 \approx 6\) cm and the phases of single-bounce paths shift
by \(\pi\) — destructive becomes constructive, and vice versa.

**Two regimes:**

- **Rayleigh fading** — when there is no dominant line-of-sight component
  and you sum many comparable scatterers. Amplitude \(|r|\) is Rayleigh-
  distributed, *power* is exponential. The standard deviation of RSSI in
  dB is about **5.6 dB** (this is the dB-scale stdev of \(10 \log_{10}
  |r|^2\) for an exponential distribution — derive it as an exercise; it
  is \( \pi / (\ln 10 \cdot \sqrt{6}) \cdot 10 \approx 5.57 \) dB). Deep
  fades 20+ dB below the mean occur with probability \(\sim 1\%\).
- **Rician fading** — when there *is* a dominant LOS component and
  scattered components are smaller. Parameterized by the K-factor
  \(K = P_\mathrm{LOS} / P_\mathrm{scatter}\). For \(K = 0\) you recover
  Rayleigh. For \(K \to \infty\) you recover deterministic LOS. In a
  typical residential room with one direct path and modest reflections,
  \(K \approx 5\text{–}10\) dB, giving RSSI stdev of **3–5 dB**.

**Numerical estimate for mockingbird's room:**

- LOS distance 3 m, typical drywall room
- LOS component \(\approx -50\) dBm (Friis + a couple of dB margin)
- Single-bounce floor reflection at ~4 m path, ground reflection
  coefficient ~0.5 at this incidence → \(\approx -56\) dBm
- Wall reflection ~5 m path, coeff ~0.3 → \(\approx -60\) dBm
- Aggregate scattered power: maybe \(-54\) dBm
- \(K \approx -50 - (-54) = 4\) dB → Rician with moderate K

This gives **RSSI stdev ≈ 4–5 dB** for a *stationary* device on a single
channel. Across BLE's channel hopping (different \(\lambda\), different
multipath sum), add another 3–5 dB peak-to-peak. So **observing a
stationary device show 15 dB peak-to-peak swing over 60 seconds is
exactly what physics predicts.** It is not a bug.

The EWMA in `tracks.py` with \(\alpha = 0.35\) gives an effective
averaging window of \( \approx 1/\alpha \approx 3\) samples, which at a
BLE advertising interval of \(\sim 1\) s is 3 seconds. This is way too
short to average out multipath fading, which has a time scale set by
how fast the *scatterer geometry* changes (people walking past) — could
be 10 s, could be minutes. The team needs a longer effective window for
stationary devices, and the adaptive EWMA is the right direction.

One more important point. **Multipath stdev does not improve with more
leaves at the same site.** Different leaves see *different* multipath
realizations, which is good for averaging the *position* estimate
(diversity gain), but the stdev *per leaf* is still 4–5 dB. The Cramér-
Rao bound (section 6) sees this directly.

---

## 4. Body absorption and shadowing — what to design around

The human body at 2.4 GHz is mostly water. Water has \(\epsilon_r
\approx 78\) and significant loss tangent. Penetration depth at 2.4 GHz
is about **1.5 cm**. A torso is 25 cm thick. So a body in the direct path
between transmitter and receiver attenuates by — well, it does not
"attenuate by a thickness" because the wave does not penetrate; it
*diffracts around*. The right model is knife-edge diffraction loss past
the body's silhouette, which gives roughly:

- **Body fully blocking LOS, antenna behind body relative to TX**:
  8–15 dB shadowing. Highly variable depending on body part and posture.
- **Hand holding phone, body to one side**: 3–6 dB.
- **Phone in pocket**: 5–12 dB (worse on the hip side facing away from
  receiver).
- **Sitting on a couch, phone on coffee table**: 2–5 dB lower variance
  because the body is no longer in the path.

This is **time-varying and large**. A person walking across a room will
shadow different leaves at different moments. The IRLS Huber loss the
team has implemented is the right defense — it down-weights a leaf whose
distance residual blows up because the body just stepped in front of it.
Without that, a single shadowed leaf can drag the MLE solution by 1–2 m,
which is exactly what the comment in `mle_multilaterate` describes.

**Design implication:** Mount leaves *high* — above head height (2 m+).
This minimizes the duration that any given leaf is body-shadowed as the
person moves around. The line from a leaf at 2.2 m down to a phone at
1.0 m clears most furniture and most of the body torso.

**Another implication:** Avoid placing leaves where the dominant
furniture (a refrigerator, a large monitor, a metal filing cabinet) sits
in the LOS path to the room's typical occupancy zone. Metal scatterers
do not just attenuate — they *redirect* energy and create strong
specular multipath. A leaf with a refrigerator between it and the
kitchen will receive packets primarily via reflection off the *opposite
wall*, which means its RSSI is encoding the wrong geometry entirely.

---

## 5. Antenna pattern of the ESP32 PCB inverted-F

The ESP32 WROOM-32's onboard antenna is a meandered PIFA (Planar
Inverted-F Antenna) printed on the corner of the module. It is *not*
isotropic, despite being treated as such by the log-distance model.

Published patterns (Espressif AN, and various third-party measurements
of the WROOM-32) show:

- **Peak gain**: ~+1 to +2 dBi in the H-plane broadside to the PCB
- **Null in the +X direction** (off the long axis of the PCB, in the
  plane of the board): −5 to −8 dBi relative to peak
- **Back lobe**: 3–5 dB below peak depending on what is behind the PCB
  (ground plane of carrier board, USB connector, etc.)
- **Polarization**: primarily linear, aligned with the antenna's long
  dimension. Cross-polarization rejection 10–15 dB.

So the **total pattern range across orientations is 8–12 dB.** If two
leaves are mounted on opposite walls with the PCB long axis pointing
toward each other, one will see the other through its *null* — receiving
8 dB less than if it were broadside. The team's TX/RX bias decomposition
captures this as a constant per-leaf offset, **but only if the leaf is
not moved and the device's orientation is the dominant geometry.** A
phone in motion sees a different leaf's pattern angle at every position,
and that cannot be calibrated out without knowing the leaf's full 3D
pattern *and* the phone's position (which we are trying to find — chicken,
egg).

**Practical consequences:**

1. **Mount all leaves with the PCB antenna corner pointing toward the
   primary occupancy zone** (the center of the room). Not flat against
   the wall — that puts the wall in the back lobe. A 1 cm standoff helps.
2. **Polarization matters.** A phone held vertically and a leaf
   PCB-flat-on-the-wall are cross-polarized — that is up to 10 dB of
   extra loss right there. If you can mount leaves at 45°, you split the
   loss across any phone orientation.
3. **For trilateration accuracy, the *effective* path-loss model becomes
   angle-dependent**: \(P_r = P_0 + G_t(\theta_t, \phi_t) + G_r(\theta_r,
   \phi_r) - 10n \log_{10}(d)\). The team is absorbing the *angular
   averages* into the bias terms. The angular *variance* (how much the
   pattern changes as the device moves) becomes part of \(\sigma\). This
   is fine for a stationary device, painful for a moving one.

---

## 6. The Cramér-Rao bound: what is physically achievable?

This is the part the team needs to internalize. There is a *lower bound*
on position variance, derivable from the Fisher information, that no
estimator — no matter how clever — can beat.

**Setup.** Each leaf \(i\) measures \(\mathrm{RSSI}_i = P_0 - 10n
\log_{10}(d_i) + \epsilon_i\) with \(\epsilon_i \sim \mathcal{N}(0,
\sigma^2)\), independent across leaves. Position \(\mathbf{p} = (x, y,
z)\) is unknown. Leaf positions \(\mathbf{x}_i\) and \((P_0, n)\) are
known.

**Fisher information matrix.** Let \(\beta = 10n/\ln 10\). Then
\(\partial \mathrm{RSSI}_i / \partial \mathbf{p} = -\beta \cdot
(\mathbf{p} - \mathbf{x}_i) / d_i^2\). The Fisher information is

$$
\mathbf{F} = \frac{\beta^2}{\sigma^2} \sum_i \frac{1}{d_i^2}
\hat{\mathbf{u}}_i \hat{\mathbf{u}}_i^\top
$$

where \(\hat{\mathbf{u}}_i = (\mathbf{p} - \mathbf{x}_i) / d_i\) is the
unit vector from leaf to device. The CRLB on position covariance is
\(\mathbf{F}^{-1}\).

**Plug in mockingbird's numbers:**

- \(n \approx 2.8\), so \(\beta \approx 12.16\)
- \(\sigma \approx 5\) dB (Rician + channel-hop variance)
- 6 leaves in a 4 m × 4 m × 2.5 m room, device near center
- Mean leaf-to-device distance \(d \approx 2.5\) m

The scalar coefficient is \(\beta^2/\sigma^2 \cdot \sum 1/d_i^2 \approx
(148/25) \cdot (6/6.25) \approx 5.7 \, \mathrm{m}^{-2}\). For
geometrically well-conditioned leaves (roughly equally distributed in
direction), the unit vectors \(\hat{\mathbf{u}}_i\) span 3D nicely and
\(\sum \hat{\mathbf{u}}_i \hat{\mathbf{u}}_i^\top \approx (N/3) \mathbf{I}\).
So

$$
\mathbf{F} \approx 5.7 \cdot \frac{6}{3} \, \mathbf{I} \, \mathrm{m}^{-2}
= 11.4 \, \mathbf{I} \, \mathrm{m}^{-2}
$$

giving per-axis variance \(1/11.4 \approx 0.088\) m² and **per-axis
stdev ≈ 0.30 m = 30 cm**. Total position stdev \(\approx \sqrt{3} \cdot
0.30 \approx 50\) cm.

**This is the CRLB for a single instantaneous measurement.** If you
average \(N\) independent samples over time, the variance drops by \(N\)
— but multipath fades are *not* independent samples; they are correlated
over the coherence time of the channel (which can be many seconds for a
stationary device, fast for a moving one). The *effective* number of
independent samples in a 10-second window is closer to 3–5, not 10
(BLE adv interval) or 50 (5 samples/s scan rate).

**So a realistic floor for a single-snapshot position estimate is
30–50 cm per axis, 50–80 cm total.**

The team reports "23 cm σ (sub-foot) via information-form averaging +
entity Kalman" in the recent commit log. **This is achievable, but only
under specific conditions**: the device must be stationary long enough
for the Kalman to integrate many measurements (which beats the CRLB
because the CRLB is per-snapshot, and Kalman integrates information over
time — equivalent to \(\sigma_\mathrm{eff} = \sigma / \sqrt{N_\mathrm{eff}}\)
with \(N_\mathrm{eff} \sim 10\)).

**For a moving device, the floor returns to ~50–80 cm.** The team should
expect tracking accuracy to *degrade* as devices move faster, and not
chase 23 cm jitter on a walking person — that is below the CRLB and you
will be chasing an illusion.

**Conditioning matters.** When the leaves are nearly coplanar (all on
walls at the same height), the vertical component of \(\mathbf{F}\)
collapses and \(\sigma_z\) blows up. Mockingbird has 8 leaves; **if they
are all at the same Z, z-axis accuracy is at the 1–2 m level no matter
what.** Put at least 2 leaves at a *different* height — ceiling, or
floor — to break the degeneracy. Without that, do not bother reporting
\(z\) at all; constrain it to a fixed plane and solve 2D.

---

## Summary: physical effects the current implementation is ignoring that matter most

Ranked by magnitude of impact on positioning accuracy, largest first:

1. **Z-axis geometric degeneracy when all leaves are coplanar.** If all
   8 leaves sit at the same height on the walls, the Fisher information
   in \(z\) collapses by 1–2 orders of magnitude. \(\sigma_z\) silently
   blows up to >1 m even when \(\sigma_{xy}\) is tight. The covariance
   ellipsoid the dashboard renders will be a long cigar in \(z\) — verify
   this in the data; if true, drop to 2D solve or add a ceiling/floor
   leaf.

2. **Body shadowing as a time-varying, non-Gaussian, *correlated-across-
   leaves* noise.** The IRLS Huber loss handles single-leaf outliers,
   but does not handle the case where a body simultaneously shadows two
   adjacent leaves (correlated downward bias on both). This pulls the
   estimate toward the unblocked side by ~30–50 cm. Detection: look for
   simultaneous Huber-down-weighting events on geometrically-adjacent
   leaves.

3. **Antenna pattern dependence on transmitter (phone) orientation.**
   The TX/RX bias decomposition captures *leaf* patterns. It cannot
   capture the *phone's* orientation-dependent radiation pattern, which
   in pockets and hands varies by 5–10 dB as the person turns. This
   shows up as an apparent "device walks 50 cm" when the person merely
   rotates. Suspect this whenever apparent motion lacks an IMU signature.

4. **Multipath correlation time vs. EWMA window.** EWMA \(\alpha=0.35\)
   averages over ~3 BLE adv intervals (~3 s). Multipath fades for a
   stationary phone in a quiet room have correlation times of 10+ s
   (only changes when furniture or people move). The current smoothing
   does not actually achieve the \(N_\mathrm{eff}\) reduction it appears
   to. Stationary device variance is biased optimistic in the short term
   and pessimistic in the long term. Adaptive EWMA already exists — make
   it more aggressive when the device is confirmed stationary (use the
   covariance trace or a small velocity-magnitude estimate as the
   confidence signal).

5. **BLE channel-hopping injects per-packet RSSI variance that is
   uncorrelated with geometry.** Same position, different channel: up to
   10 dB difference because the multipath sum shifts with \(\lambda\).
   The collector should log channel number per advertisement (if the
   ESP32 firmware exposes it — check `BLEAdvertisedDevice`) and the
   path-loss fit should include a per-channel offset (39 nuisance
   parameters that absorb the channel-dependent constructive/destructive
   bias for each channel). This typically reduces \(\sigma\) from ~5 dB
   to ~3 dB, which through the CRLB squares to a ~2.7× variance
   reduction on position — meaningful.

These are the five. The first three are *geometric and physical* — they
are about how you mount the hardware. The last two are *signal-
processing* refinements that have clear knobs in the existing code.

Fix the geometry first. Tune the filters second. Do not chase 10 cm
performance until both are done — the CRLB will not allow it.

— Vlad
