# ocr-read-packaging

**Read and compare text on printed packaging, where a single OCR engine quietly gets it wrong.**

[![License: MIT](https://img.shields.io/badge/License-MIT-informational)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)

---

## The problem

Text on packaging is not text on a page.

![The same text, scanned flat and photographed on packaging](docs/images/problem-flat-vs-curved.png)

Both panels contain exactly the same two lines. The left one is what a document OCR engine is
built for. The right one is what it actually receives when the text lives on a product:

- **The surface is curved.** Labels wrap around jars, tubes and bottles, so characters are
  geometrically compressed towards the edges and the baseline bows. Every glyph is slightly
  distorted, and distorted differently depending on where it sits.
- **The typography is not chosen for legibility.** Packaging uses condensed, display and
  decorative faces, tight tracking, and text set at small sizes around a curve.
- **The substrate fights you.** Metallised, glossy, varnished and transparent materials throw
  specular highlights that locally erase contrast. White-on-white and metallic-on-metallic
  printing is normal in this domain.
- **It is photographed, not scanned.** Uneven lighting, an off-axis camera, depth of field
  falling away across the crop, sensor noise.

Any one of these is manageable. Together they push a general-purpose OCR engine outside the
conditions it was tuned for.

### The part that actually hurts

An OCR engine under these conditions does not fail loudly. It returns *confident-looking text
that is subtly wrong* — a `0` for an `O`, `1178` for `1187`, a dropped character inside a
highlight. And a single engine gives you **no signal about which of its readings you should not
trust**. Every line comes back looking equally plausible.

That is the real problem this package addresses. Not "OCR is imperfect" — that is true
everywhere and not interesting. The problem is:

> **A single OCR engine cannot tell you when it is wrong.**

If you are comparing a printed artwork against its reference to catch a wrong lot number or a
wrong expiry date, an unflagged misread is worse than no reading at all: it silently passes.

---

## The approach

Instead of trying to make one engine read curved text perfectly, this package **manufactures a
confidence signal** and spends effort only where that signal says it is needed.

![The three-stage pipeline: two OCR engines read each crop and their agreement is measured; readings the engines agree on are compared deterministically, readings they disagree on go to a vision model acting as arbiter; every path ends in match, mismatch or undetermined](docs/images/pipeline.svg)

Each crop is read twice, by two engines that fail differently. Where they agree, the reading is
trusted and compared deterministically. Where they disagree — and in the one ambiguous case where
only one engine sees a row of dots — the pair goes to a vision model. Nothing is ever guessed:
when neither stage can decide honestly, the verdict is `undetermined`.

**1. Two independent engines read the same crop.**
Different OCR engines fail differently on curved and stylised text — they are trained on
different data, with different preprocessing and different decoding. Where two engines
independently agree on a reading, that reading is probably right. Where they disagree, you have
located an unreliable reading **without needing to know the ground truth**. Disagreement is the
confidence signal one engine cannot give you.

**2. Agreement is measured by alignment, not by string equality.**
Two *correct* readings of the same crop still differ: one inserts a space, one breaks the line
elsewhere, one orders a two-column block differently. Comparing them character-by-character with
`==` would flag all of that as disagreement. Instead the texts are compared with Needleman-Wunsch
global sequence alignment — an insertion costs one position rather than shifting and breaking
everything after it — plus normalisation variants that neutralise word order and word
segmentation. This separates *how the text was transcribed* from *what the text says*.

**3. Only genuinely ambiguous crops reach a vision model.**
A VLM reads curved packaging text far better than a document OCR engine does, but it is orders of
magnitude more expensive per crop, so it cannot be the first thing you call. Here it is the
*arbiter*: it sees only the crops the two engines could not agree on. It is asked to transcribe
both images **verbatim, without correcting what it sees**, and its two transcriptions are then
compared with the same alignment machinery. When it is not sure, it says so — and the result is
`undetermined` rather than a fabricated verdict.

The net effect: the cost of a vision model on the hard crops, the cost of ordinary OCR on the
easy ones, and an explicit `undetermined` outcome for anything the pipeline cannot honestly
decide.

### Why this generalises

Nothing above is specific to packaging, or to the particular engines shipped here. The consensus
idea works with any two OCR engines that fail differently — including two local, offline ones —
and applies to any domain where text is photographed rather than scanned: engraved plates, dot-matrix
industrial marking, embossed cards, labels on curved equipment. Packaging is simply the case that
makes the problem obvious.

---

## Install

```bash
pip install ocr-read-packaging
```

Cloud engines are optional extras — the package imports and runs with none of them installed:

```bash
pip install "ocr-read-packaging[azure]"    # Azure AI Vision + Azure OpenAI arbiter
pip install "ocr-read-packaging[google]"   # Google Cloud Vision
pip install "ocr-read-packaging[all]"      # everything, including the demo image generator
```

---

## Quickstart

Run the whole pipeline against the bundled demo images, with no credentials and no network:

```bash
git clone https://github.com/alessandromaddaloni98/ocr-read-packaging.git && cd ocr-read-packaging
pip install -e .
python examples/quickstart.py
```

```
Two engines agreeing — the arbiter is never called

match:
  match         1.000  Text matches.
mismatch:
  mismatch      0.846  Text differs substantially.
dot-pattern:
  mismatch      0.000  Different number of elements — reference has 7, target has 6.

Two engines disagreeing — the crop is escalated

disagree:
  match         1.000  Text matches.  [arbiter was called]

Same crop, but with no arbiter configured

disagree:
  undetermined    -    The engines disagreed and no arbiter is configured — review this manually.
```

With real engines:

```python
from ocr_read_packaging import compare_image_files, get_ocr_provider, get_vlm_provider

engines = [get_ocr_provider("azure_vision"), get_ocr_provider("google_vision")]
arbiter = get_vlm_provider("azure_openai")

result = compare_image_files(
    "reference.png", "target.png", ocr_providers=engines, vlm_provider=arbiter
)
print(result.verdict.value, result.note)
```

Or compare two readings directly, without images:

```python
from ocr_read_packaging import Settings, compare_texts

settings = Settings()

print(compare_texts("LOT 2024-A", "LOT 2024-A", settings=settings).is_match)   # True
print(compare_texts("BATCH X-1187", "BATCH X-1178", settings=settings).is_match)  # False

# Two engines reading the same block often disagree about layout, not content.
# The winning comparison variant is reported back, so you know why a pair matched.
reordered = compare_texts(
    "net wt 250 g  lot 2024-a  exp 2026-05",
    "lot 2024-a exp 2026-05 net wt 250 g",
    settings=settings,
)
print(reordered.method, reordered.score)   # nw_token_sort 1.0

resegmented = compare_texts(
    "store in a cool dry place away from direct sunlight",
    "store in a cooldry place awayfrom direct sun light",
    settings=settings,
)
print(resegmented.method, resegmented.score)   # nw_nospace 1.0
```

Every result carries a `note`: one plain-language sentence describing what was found,
written for whoever is checking the artwork rather than for whoever configured the pipeline.

---

## How a verdict is reached

![Decision tree: an uncertain reading goes to the arbiter; two dot runs are compared by count; one dot run against ordinary text goes to the arbiter; text of at most fifteen characters is compared with a threshold of 0.97, longer text with 0.95 using the best of three normalisation variants](docs/images/decision-flow.svg)

The comparison is not one similarity test. Which test applies depends on what the text looks
like.

| Input | Comparison | Why |
|---|---|---|
| Either read flagged uncertain | escalate to the arbiter | The text itself is not trustworthy, so comparing it proves nothing |
| Both sides are runs of dots | compare **counts** | Normalisation strips bullet characters, so both sides would otherwise compare as equally empty |
| Exactly one side reads as dots | escalate to the arbiter | The engines disagree about *what they are looking at*, not just about characters |
| Text ≤ 15 characters | one alignment, threshold **0.97** | One wrong character in a short code is a large fraction of it |
| Text > 15 characters | best of three alignments, threshold **0.95** | Engines disagree about layout more often than about content |

The three long-text variants are the text as-is, token-sorted (order-invariant), and
space-stripped (segmentation-invariant). The best score wins, and **the winning variant is
reported back**, so you know *why* a pair matched — `nw_token_sort` means the words were
reordered, `nw_nospace` means they were split differently.

### Worked examples

The images below are generated by [`scripts/generate_demo_images.py`](scripts/generate_demo_images.py).
Every image in this repository is synthetic: the text is invented, the substrate is drawn, and
the curvature, lighting, defocus and noise are applied numerically. Regenerate them with:

```bash
python scripts/generate_demo_images.py
```

**Same text, different conditions → match**, no arbiter call.

![Match](docs/images/demo-match.png)

**A short field with two digits transposed → mismatch.** `BATCH X-1187` against `BATCH X-1178`
scores below the strict short-text threshold.

![Mismatch](docs/images/demo-mismatch.png)

**Seven dots against six → mismatch**, decided by count rather than by characters.

![Dot pattern](docs/images/demo-dot-pattern.png)

---

## Configuration

Everything is configured through a `Settings` dataclass, which every entry point accepts:

```python
from ocr_read_packaging import Settings, compare_texts

settings = Settings().with_overrides(nw_long=0.97, ocr_language="en")
result = compare_texts("LOT 2024-A", "LOT 2024-B", settings=settings)
```

`Settings.from_env()` reads the environment. It loads a `.env` file **only** when you pass one
explicitly or set `OCR_ENV_FILE` — a library should not pick up a stray `.env` from your project
without being asked. Every variable is listed and commented in [`.env.example`](.env.example).

Credentials are never stored in `Settings`. Each provider reads its own from the environment when
it is constructed, so you can hold and log a `Settings` object without it ever containing secrets.

---

## Bring your own engine

A provider is any object with a `name` and a `read` method. It subclasses nothing and imports
nothing from this package:

```python
class TesseractOCR:
    name = "tesseract"

    def read(self, image, *, crop_id=""):
        import pytesseract
        try:
            text = pytesseract.image_to_string(image[:, :, ::-1], config="--psm 6")
        except Exception as exc:
            # Always raise — an empty string legitimately means "no text in this crop".
            raise ProviderError(f"tesseract failed on {crop_id!r}: {exc}") from exc
        return " ".join(text.split())
```

The consensus idea does not depend on the engines shipped here. Any two engines that fail
differently will do — **including two entirely local ones**, which removes the cost and the
privacy exposure of cloud OCR altogether. Two Tesseract configurations with different
page-segmentation modes already fail differently enough to be useful; see
[`examples/custom_provider.py`](examples/custom_provider.py).

Check what is available in your environment with:

```bash
orp providers
```

```
azure_openai     vlm  not configured       AZURE_OPENAI_ENDPOINT is not set or is not a URL
azure_vision     ocr  not configured       AZURE_VISION_KEY is not set
google_vision    ocr  missing dependency   google-cloud-vision is not installed
```

The distinction matters: *missing dependency* means install something, *not configured* means
set a credential.

---

## Command line

```bash
orp compare reference.png target.png
orp batch reference_dir/ target_dir/          # pairs files by name
orp providers                                 # what is installed and configured
```

```
OK    lot-code                     1.000  nw             Text matches.
DIFF  batch-number                 0.846  nw             Text differs substantially.
??    expiry-date                    -    vlm_needed     Could not be verified automatically.

3 compared — 1 matched, 1 differed, 1 undetermined
```

Useful flags: `--engines a,b` to choose OCR providers, `--vlm none` to leave disagreements
undetermined rather than paying for a model, `--json` for machine-readable output, and
`--nw-agreement` / `--nw-short` / `--nw-long` to override thresholds for one run.

Exit codes are meant for scripting: `0` when everything matched, `1` when anything mismatched
**or could not be determined**, `2` when the run could not be performed at all. A broken
configuration and a genuine mismatch need different responses.

## Tuning

| Setting | Default | Raise it to… | Lower it to… |
|---|---|---|---|
| `nw_agreement` | 0.93 | Trust the engines less; more crops reach the arbiter — more accurate, slower, more expensive | Cut arbiter calls, at the risk of accepting a reading two engines only loosely agreed on |
| `nw_short` | 0.97 | Be stricter on short codes | Tolerate OCR noise in short fields — rarely what you want, since these are usually the critical ones |
| `nw_long` | 0.95 | Be stricter on blocks of text; expect more false mismatches from ordinary OCR noise | Tolerate more difference in long text |
| `short_text_len` | 15 | Treat more fields as "short", applying the strict threshold to them | — |

---

## Limitations

These are properties of the design, not defects to be worked around later.

- **It compares crops; it does not find them.** There is no detection or segmentation stage. You
  supply a reference region and the corresponding target region, roughly aligned. Locating and
  pairing regions is your pipeline's job.
- **Similarity is length-relative, so a single changed character inside a long block can pass.**
  One substitution costs roughly `1/length` of the score, so one wrong digit in a 46-character
  sentence scores about 0.96 — above the default `nw_long` of 0.95 — and is reported as a
  **match**. This is inherent to comparing whole blocks by similarity, and the same tolerance is
  what makes the comparison survive OCR noise.
  **Remedy: crop short critical fields — lot number, expiry date, serial — into their own
  regions**, where the far stricter `nw_short` applies. Lowering `nw_long` instead produces false
  mismatches across every long block.
- **The built-in engines are cloud services.** They cost money per call and they receive your
  images. See [Privacy](#privacy).
- **VLM transcription is not deterministic**, even at temperature 0. The arbiter's verdict on a
  borderline crop can vary between runs.
- **Normalisation is Latin-script oriented.** Text in Greek, Cyrillic, CJK or Arabic scripts is
  stripped by the allowed-character rule and will not compare meaningfully. Widening the
  character class is the right fix for those scripts.
- **The visual-confusion matrix does not currently affect the score.** It shapes which alignment
  is chosen, not how many positions count as matching; `("lot0", "loto")` and `("lot0", "lotx")`
  score identically. This is documented and pinned by tests rather than silently carried.

---

## Privacy

Each configured cloud provider **receives a copy of every image you send it**, subject to that
provider's own retention and data-handling terms. If your images are sensitive, use local
engines: the provider protocol is designed so that swapping cloud engines for offline ones
changes nothing else about the pipeline.

The package itself sends nothing anywhere, collects no telemetry, and writes nothing outside the
paths you give it.

---

## Contributing

Issues and pull requests are welcome. The test suite runs offline — no credentials and no network
access are required:

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT. See [LICENSE](LICENSE).
