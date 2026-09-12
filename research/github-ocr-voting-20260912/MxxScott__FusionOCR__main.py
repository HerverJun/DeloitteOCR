"""
FusionOCR — children's handwriting OCR pipeline
================================================

Accuracy is the primary objective. This pipeline is designed for
exam scripts written by children — irregular letter formation,
mixed print/cursive, lined paper, varying pen pressure.

Architecture:
  Image
    └─► Preprocessor (remove ruled lines · deskew · denoise · binarize)
          └─► Segmenter (line crops)
                ├─► TrOCR-large-handwritten  (batch) ─┐
                ├─► TrOCR-large-printed       (batch) ─┤  consensus
                ├─► EasyOCR                  (thread) ─┤  voting
                └─► Tesseract                         ─┘
                      └─► JSON output with confidence + flagged lines

Why two TrOCR models?
  Children write in a spectrum from neat print to semi-cursive.
  The handwriting model catches cursive; the printed model catches
  block-letter writers. Consensus between the two is more robust
  than either alone.

Confidence below CONFIDENCE_THRESHOLD → line flagged for human review.
Keep threshold conservative: missed OCR errors in marked scripts are
more costly than over-flagging.
"""

import os, time, json, logging, functools
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from PIL import Image
import torch
import numpy as np
from transformers import TrOCRProcessor, VisionEncoderDecoderModel
import easyocr
import pytesseract
import Levenshtein

from preprocessor import preprocess
from segmenter    import segment_lines

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR    = os.path.join(SCRIPT_DIR, "ocr_logs")
# MODELS_DIR = r"C:\Users\lawizi\FusionOCR-models"
MODELS_DIR = r"C:\Users\lawizi\FusionOCR-models"
os.makedirs(LOG_DIR,    exist_ok=True)
os.makedirs(MODELS_DIR, exist_ok=True)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Two TrOCR models — covers both handwriting and block-print styles
TROCR_HANDWRITTEN = "microsoft/trocr-large-handwritten"
TROCR_PRINTED     = "microsoft/trocr-large-printed"

# Beam search width — higher = more accurate, slower
# 8 beams is the accuracy sweet spot for children's writing
NUM_BEAMS = 8

# Lines per TrOCR batch — reduce if you hit OOM on GPU
TROCR_BATCH = 4

# Lines below this confidence are flagged for human review
# Conservative threshold: better to flag too many than miss an error
CONFIDENCE_THRESHOLD = 0.65

# Engine weights in consensus vote (must sum to 1.0)
# TrOCR-handwritten weighted highest; Tesseract lowest for cursive
WEIGHTS = {
    "trocr_hw":   0.40,
    "trocr_pr":   0.30,
    "easyocr":    0.20,
    "tesseract":  0.10,
}

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging() -> str:
    fname = datetime.now().strftime("ocr_run_%Y-%m-%d_%H-%M-%S.log")
    path  = os.path.join(LOG_DIR, fname)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    logging.getLogger().handlers[1].setLevel(logging.INFO)
    return path

log_path = setup_logging()

def logged(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        logging.debug(f"[{fn.__name__}] start")
        try:
            r = fn(*a, **kw)
            logging.debug(f"[{fn.__name__}] done")
            return r
        except Exception as e:
            logging.error(f"[{fn.__name__}] {e}", exc_info=True)
            raise
    return wrapper

# ── Model loading ─────────────────────────────────────────────────────────────

print(f"Device: {DEVICE}")
print("Loading TrOCR-large-handwritten...")
_proc_hw  = TrOCRProcessor.from_pretrained(TROCR_HANDWRITTEN, cache_dir=MODELS_DIR)
_model_hw = VisionEncoderDecoderModel.from_pretrained(TROCR_HANDWRITTEN, cache_dir=MODELS_DIR).to(DEVICE)
_model_hw.eval()

print("Loading TrOCR-large-printed...")
_proc_pr  = TrOCRProcessor.from_pretrained(TROCR_PRINTED, cache_dir=MODELS_DIR)
_model_pr = VisionEncoderDecoderModel.from_pretrained(TROCR_PRINTED, cache_dir=MODELS_DIR).to(DEVICE)
_model_pr.eval()

print("Loading EasyOCR...")
_easyocr = easyocr.Reader(
    ['en'], gpu=(DEVICE == "cuda"),
    model_storage_directory=MODELS_DIR,
    download_enabled=True,
)

# ── OCR engines ───────────────────────────────────────────────────────────────

@logged
def _batch_trocr(line_imgs: list, processor, model) -> list:
    """Batch inference for one TrOCR model. Returns [(text, conf), ...]."""
    results = []
    for i in range(0, len(line_imgs), TROCR_BATCH):
        batch = [img.convert("RGB") for img in line_imgs[i:i + TROCR_BATCH]]
        inputs = processor(images=batch, return_tensors="pt", padding=True).to(DEVICE)

        with torch.no_grad():
            output = model.generate(
                **inputs,
                num_beams=NUM_BEAMS,
                output_scores=True,
                return_dict_in_generate=True,
            )

        texts = processor.batch_decode(output.sequences, skip_special_tokens=True)

        for j, text in enumerate(texts):
            # Confidence: mean max-token probability across generated steps
            if output.scores:
                probs = []
                for step in output.scores:
                    if j < step.shape[0]:
                        probs.append(torch.softmax(step[j], dim=-1).max().item())
                conf = round(float(np.mean(probs)) if probs else 0.65, 3)
            else:
                conf = 0.65
            results.append((text.strip(), conf))

    return results


def batch_trocr_hw(line_imgs): return _batch_trocr(line_imgs, _proc_hw, _model_hw)
def batch_trocr_pr(line_imgs): return _batch_trocr(line_imgs, _proc_pr, _model_pr)


@logged
def run_easyocr(line_imgs: list) -> list:
    out = []
    for img in line_imgs:
        arr = np.array(img.convert("RGB"))
        parts = _easyocr.readtext(arr, detail=0, paragraph=False)
        out.append(" ".join(parts).strip())
    return out


@logged
def run_tesseract(line_imgs: list) -> list:
    cfg = "--oem 3 --psm 7"   # single line mode
    return [
        pytesseract.image_to_string(img.convert("RGB"), config=cfg).strip()
        for img in line_imgs
    ]

# ── Weighted consensus ────────────────────────────────────────────────────────

def _weighted_pick(candidates: list) -> tuple:
    """
    Weighted Levenshtein consensus.
    candidates: list of (text, weight) tuples.
    Returns (winning_text, confidence).
    """
    if not candidates:
        return "", 0.0
    if len(candidates) == 1:
        return candidates[0][0], 0.5

    texts   = [c[0] for c in candidates]
    weights = [c[1] for c in candidates]

    def weighted_dist(w):
        return sum(
            wt * Levenshtein.distance(w, other)
            for other, wt in zip(texts, weights) if w != other
        )

    best   = min(texts, key=weighted_dist)
    dsum   = weighted_dist(best)
    maxlen = max(len(t) for t in texts) or 1
    norm   = maxlen * (len(texts) - 1)
    conf   = max(0.0, 1.0 - dsum / norm) if norm > 0 else 1.0
    return best, round(conf, 3)


def consensus(hw_text, hw_conf, pr_text, pr_conf,
              easy_text, tess_text) -> tuple:
    """
    Word-level weighted consensus across all four engines.
    If top two TrOCR models agree perfectly, trust their result directly.
    """
    # Fast path: both TrOCR models agree + high confidence
    if hw_text == pr_text and min(hw_conf, pr_conf) >= 0.88:
        return hw_text, round((hw_conf + pr_conf) / 2, 3)

    hw_words   = hw_text.split()
    pr_words   = pr_text.split()
    easy_words = easy_text.split()
    tess_words = tess_text.split()

    max_len = max(len(hw_words), len(pr_words), len(easy_words), len(tess_words), 1)
    words, confs = [], []

    for i in range(max_len):
        candidates = []
        for wlist, wkey in [(hw_words,   "trocr_hw"),
                             (pr_words,   "trocr_pr"),
                             (easy_words, "easyocr"),
                             (tess_words, "tesseract")]:
            if i < len(wlist):
                candidates.append((wlist[i], WEIGHTS[wkey]))

        w, c = _weighted_pick(candidates)
        words.append(w)
        confs.append(c)

    text = " ".join(words)
    conf = round(sum(confs) / len(confs), 3) if confs else 0.0
    return text, conf

# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_ocr(image_path: str) -> dict:
    start = time.time()
    logging.info(f"=== FusionOCR start: {image_path} ===")

    # 1. Preprocess (remove ruled lines, deskew, denoise, binarize)
    preprocessed = preprocess(image_path, binarize=True, remove_lines=True)

    # 2. Segment into lines
    line_imgs = segment_lines(preprocessed)
    n = len(line_imgs)
    logging.info(f"[Segmenter] {n} lines detected")

    # 3. Run all engines — TrOCR models batched, others parallel
    with ThreadPoolExecutor(max_workers=3) as pool:
        hw_future   = pool.submit(batch_trocr_hw, line_imgs)
        pr_future   = pool.submit(batch_trocr_pr, line_imgs)
        easy_future = pool.submit(run_easyocr,    line_imgs)
        tess_results = run_tesseract(line_imgs)   # sequential, fast

    hw_results   = hw_future.result()    # [(text, conf), ...]
    pr_results   = pr_future.result()
    easy_results = easy_future.result()  # [text, ...]

    # 4. Weighted consensus per line
    line_data = []
    for i in range(n):
        hw_t,   hw_c  = hw_results[i]
        pr_t,   pr_c  = pr_results[i]
        easy_t        = easy_results[i]
        tess_t        = tess_results[i]

        final_text, final_conf = consensus(hw_t, hw_c, pr_t, pr_c, easy_t, tess_t)
        flagged = final_conf < CONFIDENCE_THRESHOLD

        logging.info(
            f"  Line {i+1:02d}  [{final_conf:.2f}]{'  ⚠' if flagged else '   '}  {final_text!r}"
        )

        line_data.append({
            "line":       i + 1,
            "text":       final_text,
            "confidence": final_conf,
            "engines": {
                "trocr_handwritten": hw_t,
                "trocr_printed":     pr_t,
                "easyocr":           easy_t,
                "tesseract":         tess_t,
            },
            "flagged": flagged,
        })

    full_text     = "\n".join(r["text"] for r in line_data)
    flagged_lines = [r for r in line_data if r["flagged"]]
    elapsed       = round(time.time() - start, 2)

    result = {
        "source":        image_path,
        "full_text":     full_text,
        "lines":         line_data,
        "flagged_lines": flagged_lines,
        "elapsed_sec":   elapsed,
    }

    # 5. Save JSON
    json_path = os.path.join(
        LOG_DIR, datetime.now().strftime("ocr_result_%Y-%m-%d_%H-%M-%S.json")
    )
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # 6. Summary
    print("\n" + "=" * 60)
    print(f"FUSIONOCR  ·  {n} lines  ·  {elapsed}s  ·  {DEVICE}")
    print("=" * 60)
    print(full_text)
    if flagged_lines:
        print(f"\n⚠  {len(flagged_lines)} line(s) flagged (confidence < {CONFIDENCE_THRESHOLD}):")
        for fl in flagged_lines:
            print(f"   Line {fl['line']:02d}  [{fl['confidence']:.2f}]  {fl['text']!r}")
    print(f"\nResult → {json_path}")
    print("=" * 60 + "\n")

    return result


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SCRIPT_DIR, "test_image.png")
    if not os.path.exists(path):
        print("Usage: python main.py <image_path>")
    else:
        run_ocr(path)
