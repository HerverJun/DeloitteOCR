"""Import official train/validation/test identities; audit duplicates before publication."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
from scipy.fft import dctn
from scipy.io import loadmat
from torchvision.datasets import Flowers102

from image_platform.datasets import publish_fixed_dataset
from image_platform.images import decode, file_sha
from image_platform.settings import Settings
from image_platform.store import Store, dump, now


ROOT = Path("E:/image-platform-data/public/flowers102")
PROJECT = "project_flowers102_official"
# Confirmed visually before any model completed; evidence is kept in reports/.
REVIEWED_VALIDATION_DUPLICATES = {1835: 1858, 1929: 1959, 7015: 7040, 7029: 8080}


def main():
    store = Store(Settings.from_env())
    prior = store.one("SELECT id FROM datasets WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (PROJECT,))
    if prior:
        print(dump({"project_id": PROJECT, "dataset_id": prior["id"], "existing": True}))
        return
    label_numbers = loadmat(ROOT / "imagelabels.mat", squeeze_me=True)["labels"]
    splits = loadmat(ROOT / "setid.mat", squeeze_me=True)
    split_of = {int(number): split for split, key in [("train", "trnid"), ("val", "valid"), ("test", "tstid")] for number in splits[key]}
    assert len(split_of) == 8189
    labels = [{"label_id": f"flower_{i+1:03d}", "display_name": name, "definition": f"Oxford Flowers102 official category {i+1}"} for i, name in enumerate(Flowers102.classes)]

    def inspect(number):
        path = ROOT / "jpg" / f"image_{number:05d}.jpg"
        image, digest = decode(path)
        gray = np.asarray(image.convert("L").resize((32, 32)), dtype=np.float32)
        transformed = dctn(gray, type=2, norm="ortho")[:8, :8].reshape(-1)
        bits = transformed > np.median(transformed[1:])
        phash = sum(int(value) << i for i, value in enumerate(bits))
        return {"id": f"asset_flowers102_{number:05d}", "number": number, "path": str(path), "sha256": digest,
                "name": path.name, "width": image.width, "height": image.height, "phash": phash,
                "label_id": f"flower_{int(label_numbers[number-1]):03d}", "split": split_of[number], "group_id": digest}

    items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, item in enumerate(pool.map(inspect, range(1, 8190)), 1):
            items.append(item)
            if i % 500 == 0:
                print(f"Decoded and fingerprinted {i}/8189", flush=True)
    by_hash = defaultdict(list)
    for item in items:
        by_hash[item["sha256"]].append(item)
    quarantine = []
    priority = {"test": 0, "val": 1, "train": 2}
    for group in by_hash.values():
        if len({item["label_id"] for item in group}) > 1:
            raise ValueError(f"Conflicting official labels: {[item['number'] for item in group]}")
        if len({item["split"] for item in group}) > 1:
            keep = min(group, key=lambda item: priority[item["split"]])["split"]
            quarantine.extend({**item, "reason": "exact content occurs in a more protected split"} for item in group if item["split"] != keep)
    excluded = {item["id"] for item in quarantine}
    items = [item for item in items if item["id"] not in excluded]
    bands = defaultdict(list)
    candidates = []
    for i, item in enumerate(items):
        possible = set()
        for band in range(8):
            possible.update(bands[(band, (item["phash"] >> (8 * band)) & 255)])
        for j in possible:
            other = items[j]
            distance = (item["phash"] ^ other["phash"]).bit_count()
            if other["split"] != item["split"] and distance <= 2:
                candidates.append({"first": other["number"], "second": item["number"], "first_split": other["split"], "second_split": item["split"],
                                   "same_label": other["label_id"] == item["label_id"], "phash_hamming": distance})
        for band in range(8):
            bands[(band, (item["phash"] >> (8 * band)) & 255)].append(i)
    confirmed = [pair for pair in candidates if REVIEWED_VALIDATION_DUPLICATES.get(pair["first"]) == pair["second"]]
    if len(confirmed) != 4:
        raise ValueError("Public duplicate evidence differs from reviewed pinned dataset")
    items = [item for item in items if item["number"] not in REVIEWED_VALIDATION_DUPLICATES]
    audit = {"checked_at": datetime.now(timezone.utc).isoformat(), "original_counts": dict(Counter(split_of.values())),
             "published_counts": dict(Counter(item["split"] for item in items)), "exact_quarantine": quarantine,
             "cross_split_phash_candidates": candidates, "visually_confirmed_pairs": confirmed,
             "near_duplicate_quarantine": list(REVIEWED_VALIDATION_DUPLICATES),
             "visual_review_evidence": "reports/flowers102-duplicate-candidates.png"}
    Path("reports/flowers102-data-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with store.connect(write=True) as con:
        con.execute("INSERT OR IGNORE INTO projects(id,name,description,labels,created_at) VALUES(?,?,?,?,?)",
                    (PROJECT, "公开基准 · Flowers102", "Oxford 官方真实图片 · 102 类 · 每类 10 张训练图。保留官方验证/测试划分，用于通用少样本评测。", dump(labels), now()))
        for item in items:
            con.execute("INSERT OR IGNORE INTO assets(id,project_id,path,name,sha256,width,height,label_id,group_id,source_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (item["id"], PROJECT, item["path"], item["name"], item["sha256"], item["width"], item["height"], item["label_id"], item["group_id"], "Oxford Flowers102 official", now()))
            con.execute("INSERT OR IGNORE INTO annotations VALUES(?,?,NULL,?,'public_dataset',?)", ("annotation_" + item["id"], item["id"], item["label_id"], now()))
    provenance = {"dataset": "Oxford Flowers102", "homepage": "https://www.robots.ox.ac.uk/~vgg/data/flowers/102/",
                  "setid_sha256": file_sha(ROOT / "setid.mat"), "labels_sha256": file_sha(ROOT / "imagelabels.mat"),
                  "protocol_sha256": file_sha("benchmarks/flowers102-protocol.json"), "audit": "reports/flowers102-data-audit.json"}
    version = publish_fixed_dataset(store, PROJECT, "Flowers102 去重固定划分", {item["id"]: item["split"] for item in items}, provenance)
    Path("reports/flowers102-import.json").write_text(json.dumps({"project_id": PROJECT, "dataset_id": version["id"], "stats": version["stats"], "provenance": provenance}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(dump({"project_id": PROJECT, "dataset_id": version["id"], "counts": audit["published_counts"], "perceptual_candidates": len(candidates)}), flush=True)


if __name__ == "__main__":
    main()
