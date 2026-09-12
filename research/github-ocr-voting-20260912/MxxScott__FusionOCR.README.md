# FusionOCR

Multi-engine OCR pipeline for **children's handwritten English** — exam scripts, answer sheets, exercise books.

Outputs structured JSON that can be consumed directly by [Verdikt](https://github.com/MxxScott/Verdikt) for AI-powered marking.

## Architecture

```
Image
  └─► Preprocessor
        · Remove ruled lines (horizontal + vertical margin lines)
        · Deskew · Denoise · Adaptive binarize · Morphological cleanup
            └─► Segmenter (horizontal projection → line crops)
                  ├─► TrOCR-large-handwritten (batch, 8-beam) ─┐
                  ├─► TrOCR-large-printed     (batch, 8-beam) ─┤
                  ├─► EasyOCR                 (parallel)       ┤─► Weighted consensus → JSON
                  └─► Tesseract               (PSM-7)         ─┘
```

**Why two TrOCR models?**
Children write on a spectrum from block print to semi-cursive. The handwriting model covers irregular joined letters; the printed model covers block-letter writers. Consensus between both is more robust than either alone.

## Output

```json
{
  "source": "script_page1.jpg",
  "full_text": "The water cycle begins when...",
  "lines": [
    {
      "line": 1,
      "text": "The water cycle begins when",
      "confidence": 0.91,
      "engines": {
        "trocr_handwritten": "The water cycle begins when",
        "trocr_printed":     "The water cycle begins when",
        "easyocr":           "The water cycle begins when",
        "tesseract":         "The water eycle begins when"
      },
      "flagged": false
    }
  ],
  "flagged_lines": [],
  "elapsed_sec": 6.4
}
```

Lines below confidence `0.65` are included in `flagged_lines` for human or AI review.

## Setup

```bash
pip install -r requirements.txt
```

Tesseract also needs to be installed system-wide: https://github.com/tesseract-ocr/tesseract

**VRAM note:** Two TrOCR-large models require ~3GB VRAM (~4.6GB total download). On CPU, inference is slower but functional.

## Usage

```bash
# Single image
python main.py path/to/script_page.jpg

# Test runner
python test.py                    # synthetic test
python test.py --sample           # download real handwriting sample
python test.py path/to/image.jpg  # your own image
```

```python
from main import run_ocr
result = run_ocr("script_page.jpg")
print(result["full_text"])
```

## Using with Verdikt

FusionOCR's JSON output feeds directly into [Verdikt](https://github.com/MxxScott/Verdikt) for AI marking:

```bash
# Step 1 — transcribe
python main.py script.jpg
# → saves ocr_result.json in ocr_logs/

# Step 2 — mark (in Verdikt)
python backend/pipeline.py ocr_logs/ocr_result.json mark_scheme.json
```

## Configuration (`main.py`)

| Variable | Default | Notes |
|---|---|---|
| `NUM_BEAMS` | `8` | Beam search width — higher = more accurate |
| `TROCR_BATCH` | `4` | Reduce if GPU OOM |
| `CONFIDENCE_THRESHOLD` | `0.65` | Below this → flagged for review |
| `MODELS_DIR` | `C:\Users\lawizi\FusionOCR-models` | Where models are cached |

## Stack

`transformers` · `torch` · `easyocr` · `pytesseract` · `opencv-python` · `python-Levenshtein` · `Pillow`
