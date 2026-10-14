"""Reproducible 200-region research benchmark from 200 distinct real pages.

OmniDocBench is research-only; downloaded content stays outside the release.
No generated ground truth and no rewriting of source transcriptions.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request
from PIL import Image, ImageDraw, ImageFont

REVISION = "aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    pages = json.loads(a.labels.read_text("utf-8"))
    pages.sort(
        key=lambda p: hashlib.sha256(p["page_info"]["image_path"].encode()).hexdigest()
    )
    used, items = set(), []
    for kind, count in [("handwriting_candidate", 80), ("print", 80), ("table", 40)]:
        picked = 0
        for page in pages:
            info = page["page_info"]
            name = info["image_path"]
            note = info["page_attribute"]["data_source"] == "note"
            if (
                name in used
                or (kind == "handwriting_candidate" and not note)
                or (kind == "print" and note)
            ):
                continue
            candidates = []
            for block in page["layout_dets"]:
                text = (
                    block.get("html", "") if kind == "table" else block.get("text", "")
                )
                if block.get("ignore") or block["category_type"] != (
                    "table" if kind == "table" else "text_block"
                ):
                    continue
                if (
                    "$" in text
                    or "\\" in text
                    or not (
                        10 <= len(text) <= 6000
                        if kind == "table"
                        else 20 <= len(text) <= 400
                    )
                ):
                    continue
                candidates.append(block)
            if not candidates:
                continue
            candidates.sort(key=lambda b: str(b["anno_id"]))
            block = candidates[0]
            items.append(
                {
                    "id": f"{len(items)+1:04d}",
                    "kind": kind,
                    "page_info": info,
                    "annotation": block,
                    "reference": (
                        block.get("html", "") if kind == "table" else block["text"]
                    ),
                }
            )
            used.add(name)
            picked += 1
            if picked == count:
                break
        if picked != count:
            raise ValueError(f"Insufficient {kind} pages: {picked}/{count}")
    (a.output / "pages").mkdir(exist_ok=True)
    (a.output / "images").mkdir(exist_ok=True)

    def prepare(item):
        name = item["page_info"]["image_path"]
        url = (
            f"https://hf-mirror.com/datasets/opendatalab/OmniDocBench/resolve/{REVISION}/images/"
            + urllib.parse.quote(name)
        )
        original = a.output / "pages" / (item["id"] + ".png")
        if not original.exists():
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(url, timeout=60) as response:
                        payload = response.read()
                    original.write_bytes(payload)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(1)
        with Image.open(original) as image:
            assert image.size == (
                item["page_info"]["width"],
                item["page_info"]["height"],
            )
            poly = item["annotation"]["poly"]
            box = [
                max(0, math.floor(min(poly[::2])) - 3),
                max(0, math.floor(min(poly[1::2])) - 3),
                min(image.width, math.ceil(max(poly[::2])) + 3),
                min(image.height, math.ceil(max(poly[1::2])) + 3),
            ]
            target = a.output / "images" / (item["id"] + ".png")
            image.convert("RGB").crop(box).save(target)
        return {
            **item,
            "image": target.relative_to(a.output).as_posix(),
            "image_sha256": sha(target),
            "source_image_sha256": sha(original),
            "source_url": url,
            "crop_box": box,
        }

    completed = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(prepare, item) for item in items]
        for future in as_completed(futures):
            completed.append(future.result())
            if len(completed) % 20 == 0:
                print(f"Downloaded and cropped {len(completed)}/200", flush=True)
    completed.sort(key=lambda item: item["id"])
    review_path = Path(__file__).resolve().parents[1] / "research/public-benchmark-review.json"
    review = json.loads(review_path.read_text("utf-8")) if review_path.exists() else None
    if review:
        assert review["dataset_revision"] == REVISION and review["annotation_sha256"] == sha(a.labels)
        reviewed = {item["id"]: item for item in review["samples"]}
        for item in completed:
            if item["id"] in reviewed:
                assert item["image_sha256"] == reviewed[item["id"]]["image_sha256"]
                item["kind"] = reviewed[item["id"]]["kind"]
    result = {
        "schema_version": 1,
        "dataset": "opendatalab/OmniDocBench",
        "revision": REVISION,
        "annotation_sha256": sha(a.labels),
        "license": "research purposes only, not for commercial use",
        "scope": "200 real image regions from 200 distinct source pages; not full-page or intranet accuracy",
        "selection": "SHA256 page-name order; 80 note, 80 non-note text, 40 tables; first annotation ID; exclude ignored/LaTeX blocks; text 20-400 characters, table HTML 10-6000 characters",
        "handwriting_status": review["method"] if review else "candidate note regions require visual audit before handwriting CER is claimed",
        "samples": completed,
    }
    (a.output / "benchmark.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
    )
    # Large enough to identify printed vs handwritten source; references stay in JSON.
    for start in range(0, 80, 10):
        board = Image.new("RGB", (1500, 1800), "#dddddd")
        draw = ImageDraw.Draw(board)
        for index, item in enumerate(completed[start : start + 10]):
            left, top = (index % 2) * 750, (index // 2) * 360
            with Image.open(a.output / item["image"]) as image:
                image.thumbnail((730, 320))
                board.paste(image, (left + 10, top + 30))
            draw.text((left + 10, top + 8), item["id"], fill="black")
        board.save(a.output / f"handwriting-contact-{start//10+1:02d}.jpg", quality=92)
    print(
        json.dumps(
            {"samples": len(completed), "manifest": str(a.output / "benchmark.json")}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
