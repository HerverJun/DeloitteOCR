"""Select one existing public full page before inference, retaining all table GT."""

import argparse
import hashlib
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--labels", type=Path, required=True)
p.add_argument("--benchmark", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
benchmark = json.loads(a.benchmark.read_text("utf-8"))
assert hashlib.sha256(a.labels.read_bytes()).hexdigest() == benchmark["annotation_sha256"]
pages = {p["page_info"]["image_path"]: p for p in json.loads(a.labels.read_text("utf-8"))}
candidates = []
for sample in benchmark["samples"]:
    page = pages[sample["page_info"]["image_path"]]
    tables = [t for t in page["layout_dets"] if t["category_type"] == "table"]
    text = "".join(t.get("html", "") for t in tables)
    if (2 <= len(tables) <= 4 and all(not t.get("ignore") and t.get("html") for t in tables) and 100 < len(text) < 4000 and "$" not in text and "\\" not in text):
        candidates.append((hashlib.sha256(sample["page_info"]["image_path"].encode()).hexdigest(), sample, tables))
candidates.sort(key=lambda item: item[0])
_, sample, tables = candidates[0]
image = a.benchmark.parent / "pages" / (sample["id"] + ".png")
assert hashlib.sha256(image.read_bytes()).hexdigest() == sample["source_image_sha256"]
tables.sort(key=lambda t: (t.get("order", 0), str(t["anno_id"])))
value = {"scope": "Complete real public page, all non-ignored table annotations. Deterministic page-name SHA order among already downloaded pages with 2-4 tables, 100-4000 HTML characters and no LaTeX. Not an additional independent page in the 200-region corpus.", "dataset": benchmark["dataset"], "revision": benchmark["revision"], "license": benchmark["license"], "image": str(image.resolve()), "image_sha256": sample["source_image_sha256"], "page_info": sample["page_info"], "annotations": tables}
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
print(json.dumps({"page_id": sample["id"], "tables": len(tables), "image_sha256": value["image_sha256"]}))
