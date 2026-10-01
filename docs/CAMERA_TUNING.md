# Camera image tuning for detection

How to set the Hikvision image/exposure parameters so vehicles stay
detectable day and night. These are set in the **camera's own web UI**
(`http://<camera-ip>` → Configuration → Image → Display Settings), not in
FieldKit. Settled 2026-08-20 after the cam4 night-streaking incident;
cam4 (192.168.1.76) is the reference camera.

## The settings

| Setting | Value | Why |
|---|---|---|
| Exposure Mode | Auto | Camera picks shutter/gain within the limits below. |
| Shutter Range (slow end) | **1/100** (was 1/6) | The critical one. At 1/6 s a moving car integrates for 166 ms and becomes a light streak at night — undetectable and unlabelable. 1/100 keeps vehicles sharp. Daytime is unaffected: auto exposure only reaches for slow shutters in the dark. |
| Shutter Range (fast end) | 1/100000 | Leave as-is. |
| Limit Gain | **~60** (was 100) | Gain at 100 amplifies headlight glare into full-frame bloom and adds sky noise. 50–70 is the tuning band; raise gain before ever loosening the shutter. |
| Backlight → HLC | On (if firmware has it) | Suppresses bright point sources (headlights) specifically. Do not combine with WDR/BLC — HLC alone. |
| Contrast / saturation | Slightly flat ("washed") profile | See below. |
| Sharpening | Moderate | Halo artifacts confuse edges. |
| Digital noise reduction | Low/moderate, never max | Heavy DNR smears the fine texture the detector keys on, especially at night. |
| Anti-Banding | Off | Outdoor scene, no mains flicker. |

If night ends up too dark after these limits, nudge gain up within 50–70.
Do not touch the shutter floor again — a darker sharp vehicle is detectable,
a bright 100 ms smear is not.

Optional: the camera's **Custom1/Custom2** scene tabs with Scheduled-Switch
can hold separate day/night parameter sets, but the limits above are
inherently night-only in effect (daytime auto exposure never hits them), so
the schedule is not needed.

## Why the flat profile

A punchy, contrasty image pushes tones toward the extremes — highlights clip,
shadows crush, and clipped pixels carry zero information for the model. The
slightly flat rendering keeps the whole scene inside the usable range (same
idea as log/neutral profiles on cinema cameras, except YOLO consumes it
directly with no grading step). Detectors key on local edges and texture, not
global contrast, so the flat look costs nothing. Don't push it further,
though: genuinely low contrast compresses the features that small/distant
vehicles depend on.

## Consistency rule (matters more than any single setting)

The Label tab's captured samples fine-tune the model that then runs on the
same feeds. Whatever look the cameras produce, **training capture and
inference must see the same look** — so:

- Apply the same profile to **every** camera (cam3 was still on the old
  contrasty profile as of 2026-08-20 — align it), or the fine-tuned model
  carries a per-camera domain gap.
- Once set, don't drift. Changing the profile later devalues every sample
  captured before the change.
- Samples captured under an old profile are a slight mismatch; keep them if
  volumes are low, but favor new captures.
