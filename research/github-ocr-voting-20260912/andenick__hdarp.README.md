# HDARP — Multi-Engine OCR Consensus for PDF Extraction

**A Python library that chunks large PDFs by content density, runs up to three OCR engines over the pages, and adjudicates their disagreements with a six-rule consensus hierarchy.**

> **About the name**: HDARP began life as the acronym of an in-house protocol, *Hybrid Direct Agent Reading Protocol*. This repository publishes **one half of that protocol — the OCR-consensus layer**. The agent-reading half is not here: there is no vision call, no model client, no API key and no prompt anywhere in this package. The name is kept because it is the package name on disk and in the import path (`import hdarp`), not because this code performs agent reading. **The protocol that grew around this layer is documented in this repository as specification — see [The HDARP protocol today](#the-hdarp-protocol-today-v6-direct-agent-reading-for-hard) and [docs/protocol-v6.md](docs/protocol-v6.md).**
>
> **Snapshot note**: This repository is a frozen snapshot of the HDARP **v5.1** OCR-consensus layer, released **2026-05-01**. It is preserved as a self-contained reference implementation of the multi-engine consensus approach and is not tracked against later, unpublished versions of the protocol. Everything below describes the code in this repository as of that date.
>
> **On the numbers**: Accuracy and cost figures in this README and `docs/` are **indicative** — illustrative ranges and worked examples drawn from development use, *not* results from a published, reproducible benchmark dataset. No formal benchmark corpus is released with this repo. Treat them as order-of-magnitude guidance, not measured claims.

---

## The HDARP protocol today (v6): direct agent reading for hard documents

Everything below the next divider describes **the code in this repository** — the v5.1
OCR-consensus half of the protocol. This section describes the **protocol as it exists today**,
which has moved on: modern HDARP is a discipline for having AI agents **read hard documents
directly** — scanned books, statistical tables, equations, mixed-content reports — chunk by
chunk, with validation stages that refuse silent failure. OCR is no longer the headline; it is a
supporting layer (verbatim cross-checking and per-page rescue). The full specification is
[docs/protocol-v6.md](docs/protocol-v6.md); the shape in brief:

- **Mandatory chunking.** Any PDF over 10 pages or 1 MB is split; one chunk is read, processed,
  and committed at a time. No whole-document dumps.
- **Four content types per chunk, all four checked.** Body text, tables (→ CSV), equations
  (→ LaTeX), figures (→ structured descriptions). Absence of a type is *explicitly confirmed*,
  never silently omitted — a chunk processed for body text only is incomplete by definition.
- **Two extraction modes.** *Verbatim* (faithful full-text for public-domain/technical sources)
  and *analytical digest* (paraphrased, for in-copyright material) — chosen per document, recorded
  per document.
- **A validation stage that can fail.** Chunk-boundary markers, four-type completeness checks,
  and a reviewer that can reject a batch back to `PREPARED`. The protocol's hardest rule is
  **anti-silent-degradation**: when agent extraction fails, the run stops and reports — it never
  substitutes a plain OCR dump or an invented summary and calls it done.
- **A retry ladder, not a shrug.** Failures bisect to single pages, retry with scholarly framing,
  and only three outcomes terminate without extraction (exact duplicate, content-filter
  exhaustion after the full ladder, physically unreadable file) — each recorded, never guessed.
- **OCR's two supporting roles.** (a) a *verbatim sibling* — an OCR pass alongside the agent
  extraction, into a separate tree, so the two readings can be compared (augmentation, never
  substitution); (b) *per-page rescue* when a single page defeats the ladder.
- **Per-document processing records.** Identity, chunk counts, per-type counts, quality scores,
  and provenance — every extraction leaves a machine-readable detail row.

**What is in this repository vs. the protocol:** the Python package below implements the
density-aware chunker and the six-rule consensus engine — real, runnable, tested code for the
OCR half. The agent-reading half is *specified* here (v6) but not *shipped* here: it is a
prompt/validation discipline over a vision-capable model, not a library. Where the protocol
needs an engine for the OCR roles above, this consensus layer is the design that slot fills.

A note on engines and scope: the production ecosystem around the protocol also includes a
separate local-GPU extraction engine (multi-model vision routing); it is a distinct system and
is not part of this package or its claims.

---

## What this package does

| Capability | Module | Notes |
|---|---|---|
| Assess a PDF's MB/page density and chunk it adaptively | `splitter.py` | Retries with fewer pages when a chunk overshoots the size limit |
| Wrap PaddleOCR, EasyOCR and Tesseract behind one interface | `ocr_engines.py` | Lazy init; degrades gracefully to whichever engines are installed |
| Adjudicate three disagreeing OCR reads into one line | `consensus.py` | Six rules in priority order, each result carrying its own audit trail |
| Short-circuit PDFs that already carry a text layer | `ocr_engines.py`, `processor.py` | Copies the text layer; runs no OCR — see [Embedded-text short-circuit](#3-the-embedded-text-short-circuit) |
| Run a corpus through chunk → OCR → catalog, sequentially | `orchestrator.py` | CSV catalog with SHA-256 input/output hashes |
| Score an extraction on a 27-point scale | `quality_scorer.py` | Standalone; the pipeline does not call it — see [QualityScorer](#qualityscorer-standalone-and-mostly-caller-fed) |

## What this package does **not** do

Read this list before adopting it. Each item is something the wider protocol, or an
earlier description of this repository, has claimed — and that this code does not contain.

- **No agent or vision extraction.** Nothing here calls a language or vision model. There is no
  table → CSV, equation → LaTeX or figure → description path in this package; those artifacts
  come from elsewhere in the wider protocol and are not shipped here.
- **No confidence threshold and no gap marking.** The consensus engine always returns text. See
  [Confidence reporting](#5-confidence-reporting-and-what-is-not-guaranteed).
- **No validator stage.** `orchestrator.py` prepares and processes; nothing checks the result.
- **No concurrency.** No threading, no multiprocessing, no async anywhere in the package.
- **No benchmark corpus and no eval harness.** The accuracy figures are indicative only.
- **No content-filter or refusal handling**, because there is no model call to be filtered.

---

## How it works

### 1. Density-aware chunking

A 100-page text-heavy academic paper behaves nothing like a 100-page image-heavy annual report.
The splitter measures the actual MB/page density of each PDF and picks a chunking strategy:

| Density | MB/page | Strategy | Rationale |
|---------|---------|----------|-----------|
| LOW | < 0.05 | PAGE_FIRST | Text-heavy: maximize pages per chunk (up to 10) |
| MEDIUM | 0.05-0.10 | SIZE_FIRST | Mixed: balance pages and size |
| HIGH | > 0.10 | SIZE_FIRST | Image-heavy: strict size limits with retry |

The splitter retries with progressively fewer pages if a chunk exceeds the size limit, and
accepts oversized single pages with a warning flag rather than failing. It writes a
`manifest.json` alongside the chunks, which `orchestrator.py` reads back.

### 2. Six-rule OCR consensus

When three OCR engines look at the same line, which one is right? The engine applies six rules
in priority order and returns the first one that matches:

| # | Rule | Confidence | When It Fires |
|---|------|------------|---------------|
| 1 | **Perfect Agreement** | up to 0.99 | All engines produce identical text. Confidence: `1 - (1-p₁)(1-p₂)(1-p₃)` |
| 2 | **Majority Agreement** | up to 0.95 | 2+ engines agree. 5% confidence boost for consensus. |
| 3 | **High-Confidence Unilateral** | up to 0.90 | One engine ≥0.95 confidence, all others <0.50. Trust the confident one. |
| 4 | **Column-Type Validation** | up to 0.92 | NUMERIC context catches O→0, I→1 substitutions. |
| 5 | **Character Similarity** | up to 0.90 | >80% character overlap. Weighted by engine priority. |
| 6 | **Default to Primary** | 0.60-0.90 | Fallback to PaddleOCR. `max(0.60, conf × 0.9)` — note the floor *raises* a read the engine itself distrusted. |

The Rule 1 confidence combination assumes engine errors are independent: if PaddleOCR is 90%
confident, EasyOCR 85% and Tesseract 80%, the probability all three are wrong simultaneously is
(0.10)(0.15)(0.20) = 0.003, giving 99.7% confidence in the agreed text — capped at 0.99. That
independence assumption is exactly what Rule 1 gets wrong when two engines make the *same* OCR
error; that limitation is documented and asserted in the test suite. Full detail, worked examples
and known limitations: [docs/CONSENSUS_RULES.md](docs/CONSENSUS_RULES.md).

### 3. The embedded-text short-circuit

`Sraffa30Processor.process_pdf()` checks for an embedded text layer **before** doing anything
else. If the PDF has one, that text is returned verbatim and **no OCR and no consensus run at
all**. This is deliberate: re-OCR'ing a born-digital PDF is slower and strictly worse than
reading its own text layer.

Two consequences you need to know about:

- **The gate is coarse.** A page counts as having text if it yields more than 50 stripped
  characters, and the document counts if the kept pages total more than 100 characters. So a
  *mostly scanned* document with a handful of born-digital pages takes this path, and the scanned
  pages are never OCR'd. If you need consensus on every page, call `process_page()` directly.
- **This path reports no confidence at all.** `confidence` is `None` and `confidence_basis` is
  `'not_measured_embedded_text_layer'`. Copying a text layer is not a measurement, so there is no
  number to report — not `1.0`, and not any other constant. The batch catalog stores nothing for
  such a document rather than a perfect score.

The text layer is only as good as whoever produced it: exact for a born-digital PDF, and only as
good as the producer's own OCR for a scan that was OCR'd elsewhere. This package cannot tell the
difference, and does not claim to.

### 4. Batch processing

`orchestrator.py` discovers PDFs in a corpus directory, tracks each one in a CSV catalog
(`PENDING → PREPARED → COMPLETE`, or `FAILED`), chunks them, processes every chunk in order, and
concatenates the results with an explicit `<!-- chunk: … -->` boundary marker. After a batch it
advances automatically to the next batch of PENDING documents; `--single` stops after one.

Batches run **one after another in a single process**. The catalog's `quality_score` column holds
the mean OCR consensus confidence in [0, 1], or nothing when no confidence was measured — it is
**not** the 27-point `QualityScorer` total.

### 5. Confidence reporting (and what is *not* guaranteed)

The design intent is that a fabricated number is worse than a missing one. What this snapshot
implements towards that intent is **reporting, not refusal**:

- **Every result carries the rule that produced it**, the engines that contributed, and the
  alternatives considered — so a low-quality line is identifiable after the fact.
- **The embedded-text path reports no confidence at all**, rather than a hardcoded 1.0.
- **The consensus engine applies no minimum-confidence threshold and marks no gaps.** Rule 6
  always returns the primary engine's text, and its `max(0.60, conf × 0.9)` floor *raises* a read
  the engine itself distrusted — three garbage reads at 0.11 / 0.09 / 0.05 come back as text at
  0.600.

That floor is the sharpest edge in this package, so Rule 6 records what it did to the number.
When the floor changes the value, the result's `metadata` carries:

```python
result.metadata["primary_engine_confidence"]  # 0.11 — what the engine actually reported
result.metadata["confidence_floor_applied"]   # True — the 0.60 floor raised it
result.confidence                             # 0.600 — the floor, not a measurement
```

**If your use case needs "do not use below X", you must apply that floor yourself** — to
`result.confidence`, or, if you want the floor undone, to
`metadata["primary_engine_confidence"]`. Gap marking is a property of the surrounding protocol,
not of this package.

### QualityScorer: standalone, and mostly caller-fed

`QualityScorer` is a **standalone utility**. Nothing in the pipeline calls it, and it is not part
of the chunk → OCR → catalog path.

Of its 27 points, only 9 can be scored from what this package itself produces:

| Component | Points | Scoreable from this package's output? |
|-----------|--------|---------------------------------------|
| Text | 4 | Yes — from `result['text']` |
| Formatting | 3 | Yes — from `result['text']` |
| OCR Confidence | 2 | Yes — from `result['confidence']` (`None` on the embedded-text path scores 0) |
| Tables | 8 | **No** — you must supply parsed tables |
| Equations | 3 | **No** — you must supply equations |
| Figures | 3 | **No** — you must supply figure descriptions |
| Metadata | 4 | **No** — you must supply page numbers, headers, cross-references, title |

The 18 caller-fed points are kept rather than deleted because the scoring logic works and is
useful to anyone who *does* have those artifacts from another tool. But scoring a
this-package-only extraction will cap out at 9/27, which grades F. Do not read that as a verdict
on the extraction.

---

## Installation

```bash
git clone https://github.com/andenick/hdarp.git
cd hdarp
pip install -e .            # importable from anywhere; chunking + consensus only
```

`pip install -e .` pulls only the light dependencies (`pypdf`, `Pillow`, `numpy`) — enough to chunk PDFs and run the consensus engine. Add extras as needed:

```bash
pip install -e ".[pdf]"     # PyMuPDF, for rendering pages and reading text layers
pip install -e ".[ocr]"     # PaddleOCR + EasyOCR + Tesseract wrappers
pip install -e ".[dev]"     # pytest
pip install -r requirements.txt   # or: everything at once
```

### OCR engine dependencies

Running actual OCR requires the engines themselves. Install them separately:

```bash
# PaddleOCR (primary engine)
pip install paddlepaddle paddleocr

# EasyOCR (secondary engine)
pip install easyocr

# Tesseract (tertiary engine)
# Windows: Download from https://github.com/UB-Mannheim/tesseract/wiki
# macOS: brew install tesseract
# Linux: sudo apt install tesseract-ocr
pip install pytesseract

# PDF handling
pip install pypdf PyMuPDF
```

See [Third-party components and licences](#third-party-components-and-licences) before shipping a build that includes PyMuPDF.

---

## Quick start

### Chunk a PDF

```python
from hdarp import PDFSplitterOrchestrator

splitter = PDFSplitterOrchestrator(
    max_chunk_size_mb=1.0,
    max_chunk_pages=10
)

# Assess density first
report = splitter.assess_pdf_density("large_document.pdf")
print(f"Density: {report.density_category} ({report.avg_density:.3f} MB/page)")
print(f"Strategy: {report.recommended_strategy}")
print(f"Estimated chunks: {report.estimated_chunks}")

# Chunk with the strategy that density selected
result = splitter.chunk_pdf_intelligent("large_document.pdf", "output/chunks/")
print(f"Created {len(result.chunks)} chunks")
```

### Run OCR consensus

```python
from hdarp import Sraffa30ConsensusEngine

engine = Sraffa30ConsensusEngine()

result = engine.adjudicate(
    paddle_text="Total Revenue: $1,234,567",
    paddle_conf=0.92,
    easyocr_text="Total Revenue: $1,234,567",
    easyocr_conf=0.88,
    tesseract_text="Total Revenue: $l,234,567",  # Common OCR error: 1→l
    tesseract_conf=0.75,
    column_type="TEXT"
)

print(f"Winner: {result.text}")           # "Total Revenue: $1,234,567"
print(f"Confidence: {result.confidence}")  # 0.95 (majority agreement, 2 of 3 engines — Rule 2 cap)
print(f"Rule: {result.rule_applied}")      # "majority_agreement"
print(f"Engines: {result.winning_engines}")# ["paddle", "easyocr"]
```

These values are asserted in `tests/test_consensus.py`, so they cannot drift away from the code.

### Extract a PDF end to end

```python
from hdarp import Sraffa30Processor   # requires the [pdf] and [ocr] extras

processor = Sraffa30Processor()
result = processor.process_pdf("scanned_document.pdf")

print(result["method"])            # 'pymupdf_embedded' or 'sraffa30_ocr'
print(result["confidence"])        # float for the OCR path, None for embedded text
print(result["confidence_basis"])  # how to read the line above
```

### Score an extraction

`QualityScorer` is standalone — nothing in the pipeline calls it, and you supply the
table/equation/figure/metadata inputs it scores (see the table
[above](#qualityscorer-standalone-and-mostly-caller-fed)):

```python
from hdarp import QualityScorer

scorer = QualityScorer()
score = scorer.score_extraction(extraction_result)
print(f"Quality: {score.total}/27 ({score.grade})")
```

### Run the example, and the tests

```bash
python examples/run_example.py          # chunk a generated PDF, then adjudicate
python examples/run_example.py doc.pdf  # ... or chunk a real one
python -m pytest                        # 25 tests across splitter, consensus and orchestrator
```

The example needs no OCR engine installed; it exercises the splitter and the consensus engine
only.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    HDARP Pipeline                    │
├──────────────────────────────────────────────────────┤
│                                                      │
│  PDF Input                                           │
│    │                                                 │
│    ▼                                                 │
│  ┌──────────────────────┐                            │
│  │ Density Assessment   │ ← Classify LOW/MED/HIGH    │
│  │ (splitter.py)        │                            │
│  └──────────┬───────────┘                            │
│             │                                        │
│             ▼                                        │
│  ┌──────────────────────┐                            │
│  │ Adaptive Chunking    │ ← PAGE_FIRST or SIZE_FIRST │
│  │ (splitter.py)        │   with retry on oversize   │
│  └──────────┬───────────┘                            │
│             │                                        │
│             ▼                                        │
│  ┌──────────────────────┐   text layer found         │
│  │ Embedded-text check  ├──────────────────┐         │
│  │ (ocr_engines.py)     │                  │         │
│  └──────────┬───────────┘                  │         │
│             │ no text layer                │         │
│             ▼                              │         │
│  ┌──────────────────────┐                  │         │
│  │ 3-engine OCR         │ Paddle/Easy/Tess │         │
│  │ (ocr_engines.py)     │                  │         │
│  └──────────┬───────────┘                  │         │
│             ▼                              │         │
│  ┌──────────────────────┐                  │         │
│  │ Consensus (6 rules)  │ per line         │         │
│  │ (consensus.py)       │                  │         │
│  └──────────┬───────────┘                  │         │
│             │◄─────────────────────────────┘         │
│             ▼                                        │
│  ┌──────────────────────┐                            │
│  │ Batch Orchestration  │ ← Sequential batches,      │
│  │ (orchestrator.py)    │   CSV catalog, hashes      │
│  └──────────────────────┘                            │
│                                                      │
└──────────────────────────────────────────────────────┘

Off to the side, called by nobody in the pipeline:
  quality_scorer.py — 27-point scoring for extractions you assemble yourself
```

---

## Module reference

Line counts below are `wc -l` of the files in this commit.

| Module | LOC | Purpose |
|--------|-----|---------|
| `consensus.py` | 661 | 6-rule consensus adjudication engine |
| `ocr_engines.py` | 494 | PaddleOCR, EasyOCR, Tesseract wrappers with unified interface |
| `splitter.py` | 451 | Density-aware PDF chunking with retry logic |
| `orchestrator.py` | 444 | Batch pipeline with state management and auto-continuation |
| `processor.py` | 332 | Multi-engine OCR processing orchestration |
| `quality_scorer.py` | 320 | 27-point quality scoring framework (standalone) |

2,702 lines across the six modules, 2,781 including `__init__.py`; plus tests and a runnable example.

---

## Performance

Indicative performance figures live in one place — [docs/ARCHITECTURE.md § Performance](docs/ARCHITECTURE.md#performance) — together with the caveat that applies to them. They are illustrative ranges from development use, not benchmark results; no benchmark dataset or eval harness ships with this repo.

---

## Design philosophy

This package was built to feed large document corpora into downstream scholarly and regulatory
analysis. Three principles guide its design:

1. **Accuracy over speed**: Three OCR engines are slower than one, but the consensus mechanism
   catches errors that no single engine would. In scholarly contexts, a missed decimal point or a
   fabricated number can invalidate an entire analysis.

2. **Transparency over opacity**: Every adjudication records which engine won, which rule applied,
   what the confidence was, and what the alternatives were — including when a confidence was
   floored rather than measured. A researcher can inspect any result and understand why it was
   chosen.

3. **Safety over convenience**: never present a guess as a reading. In this snapshot that principle
   is implemented as *reporting* — every line carries its rule and confidence, and the
   embedded-text path reports no confidence rather than a fake perfect one — not as *refusal*: the
   consensus engine has no minimum-confidence threshold and does not gap-mark. See
   [Confidence reporting](#5-confidence-reporting-and-what-is-not-guaranteed).

---

## Related repositories

Other repositories published by the same author. They are listed for context only — nothing in
this package depends on them, and none of them is required to use it:

- [anu-framework](https://github.com/andenick/anu-framework)
- [anu-architecture](https://github.com/andenick/anu-architecture)
- [shaikh-capitalism-data](https://github.com/andenick/shaikh-capitalism-data)

---

## License

MIT — see [LICENSE](LICENSE).

### Third-party components and licences

The MIT licence covers this repository's own code only. A build that installs the dependencies below inherits their terms, and one of them is copyleft:

| Component | Licence | Notes |
|---|---|---|
| **PyMuPDF** (`fitz`) | **AGPL-3.0-or-later, or a commercial licence from Artifex** | **Copyleft.** Used for page rendering (`processor.py`) and embedded-text extraction (`ocr_engines.py`). If you distribute or network-serve a build that includes PyMuPDF, AGPL obligations apply to that build unless you hold a commercial licence. It is an optional extra here (`pip install -e ".[pdf]"`), but the OCR path does not work without it. |
| pypdf | BSD-3-Clause | PDF reading/writing in `splitter.py`. |
| Pillow | MIT-CMU | Image handling. |
| NumPy | BSD-3-Clause | Array handling in the engine wrappers. |
| PaddleOCR / PaddlePaddle | Apache-2.0 | Optional OCR extra. |
| EasyOCR | Apache-2.0 | Optional OCR extra. |
| pytesseract | Apache-2.0 | Optional OCR extra; wraps the Tesseract binary (Apache-2.0), which you install separately. |

Licences are as published by each project at the time of writing; verify against the version you install.

---

## Citation

If you use this library in academic work:

```bibtex
@software{hdarp2026,
  title = {HDARP: Multi-Engine OCR Consensus for PDF Extraction},
  author = {Anderson, Nicholas},
  version = {5.1},
  year = {2026},
  url = {https://github.com/andenick/hdarp}
}
```
