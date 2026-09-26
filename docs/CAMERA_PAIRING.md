# Camera pairing proposals

`pair_cameras.py` samples two recordings at common nominal Zambia wall-clock times and asks
`gpt-5.6-luna` for reviewable overlap or handoff geometry. It writes `proposal.json` and a
self-contained `review.html`; neither artifact activates a pair or changes FieldKit/CountKit.

```sh
export OPENAI_API_KEY=...
python pair_cameras.py propose \
  --a recordings/RDA-TG-KTB/north/20260908-161500.mkv \
  --b recordings/RDA-TG-KTB/south/20260908-161500.mkv \
  --camera-a north --camera-b south --site RDA-TG-KTB \
  --samples 4 --step 2 --output pairing-review
```

`--start` accepts an offset-aware ISO datetime. Without it, sampling begins 30 seconds after
the later recorder filename time. Recorder filenames are interpreted as Africa/Lusaka
(UTC+02), regardless of the computer timezone. Samples are limited to 2–6 and scaled to at
most 1280 pixels wide for API review; JSON retains native dimensions, requested nominal time,
actual decoded PTS, source/JPEG SHA-256 hashes, and normalized coordinates against each image.
Only requested JPEG frames are sent. The API key is read only from `OPENAI_API_KEY`.
Inputs must be finalized, immutable recording segments; the command does not lock files that
are still being written.
Sampled footage is sent to OpenAI. `store: false` disables response storage for this request,
but it is not Zero Data Retention; the organization's configured data controls still apply.

The JSON is a versioned, provisional advisor artifact. `overlap` and `adjacent_handoff` remain
`status: proposal`; `uncertain` and `unrelated` are `requires_review`. A human must compare the
unaltered images and overlays, correct geometry and clock evidence, then create a Label draft
and use CountKit's versioned Save flow. Do not import CountKit code or write active config from
this artifact. OSD readouts are evidence for coarse recording-time correction intervals only;
they are not measured inter-camera skew. This first command preserves readings for review and
does not calculate or apply an offset.
Vision models can fail at precise spatial localization, so polygons and landmarks always need
comparison against the unaltered sampled frames before downstream use.

Identical inputs, model, prompt, and sampling parameters reuse an existing valid output to
avoid another paid call. Changed inputs conflict with the existing output directory; choose a
new directory. A command makes at most one request, never retries or falls back to another
model/provider, and publishes artifacts atomically after all frames and the response validate.

Offline checks:

```sh
/opt/anaconda3/bin/python pair_cameras.py
git diff --check
```

API contracts: [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[image inputs](https://developers.openai.com/api/docs/guides/images-vision), and
[structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), and
[API data controls](https://developers.openai.com/api/docs/guides/your-data).
