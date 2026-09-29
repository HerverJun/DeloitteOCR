"""Audit a fixed candidate's offline runtimes, license files and model bytes."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def audit(run_root):
    index = json.loads((run_root / "portal/fixed-candidate-index.json").read_text(encoding="utf-8"))
    if index.get("schema_version") != 1:
        raise ValueError("Unsupported candidate index")
    root = Path(index["candidate_path"]).resolve(strict=True)
    candidate_manifest = root / "candidate-manifest.json"
    if digest(candidate_manifest) != index["candidate_manifest_sha256"]:
        raise ValueError("Candidate identity differs from run index")
    candidate = json.loads(candidate_manifest.read_text(encoding="utf-8"))
    if (candidate["format"] != 3 or candidate["optional_apps"] != {
            "ocr": "review_and_pdf_native", "classification": "runtime_and_weights"}):
        raise ValueError("Candidate components differ from the fixed scope")
    declared = candidate["files"]
    inventory = json.loads((root / "dependency-inventory.json").read_text(encoding="utf-8"))
    prefixes = {"platform": "runtimes/platform/Lib/site-packages/",
                "data": "runtimes/data/Lib/site-packages/",
                "ocr_control": "bundles/ocr/runtimes/control/Lib/site-packages/",
                "ocr_pdf": "bundles/ocr/runtimes/pdf/Lib/site-packages/",
                "classification_control": "bundles/classification/runtimes/control/Lib/site-packages/"}
    if set(inventory) != set(prefixes):
        raise ValueError("Runtime inventory is incomplete")
    license_count = 0
    for runtime, rows in inventory.items():
        if not rows:
            raise ValueError(f"Empty dependency inventory: {runtime}")
        for item in rows:
            if not item["license_files"]:
                raise ValueError(f"License text is missing: {item['distribution']}")
            for relative in item["license_files"]:
                name = prefixes[runtime] + relative
                file = root / name
                if (not file.is_file() or name not in declared
                        or digest(file) != declared[name]["sha256"]):
                    raise ValueError(f"License file differs from candidate: {name}")
                license_count += 1
    source = index["license_resource"]
    if digest(Path(source["path"])) != source["sha256"]:
        raise ValueError("Upstream license source manifest changed")
    pinned = json.loads((root / "license-sources.json").read_text(encoding="utf-8"))
    if pinned.get("format") != 1 or len(pinned.get("files", {})) != 7:
        raise ValueError("Pinned upstream license source set changed")
    for relative, record in pinned["files"].items():
        cache = Path(source["path"]).parent / relative
        if (not cache.is_file() or cache.stat().st_size != record["bytes"]
                or digest(cache) != record["sha256"]):
            raise ValueError(f"Pinned upstream license differs: {relative}")
    model_source = index["model_resource"]
    if digest(Path(model_source["path"])) != model_source["sha256"]:
        raise ValueError("Model source manifest changed")
    model_manifest = root / "bundles/classification/resources/weights/manifest.json"
    if digest(model_manifest) != model_source["sha256"]:
        raise ValueError("Packaged model manifest changed")
    models = json.loads(model_manifest.read_text(encoding="utf-8"))["models"]
    if set(models) != {"resnet18", "dinov2", "mobilenetv4", "repvit"}:
        raise ValueError("Offline encoder coverage is incomplete")
    for encoder, item in models.items():
        relative = f"bundles/classification/resources/weights/{encoder}/model.safetensors"
        weight = root / relative
        card = root / f"bundles/classification/resources/third-party/{encoder}-model-card.md"
        if (item["license"] != "apache-2.0" or not weight.is_file()
                or weight.stat().st_size != item["bytes"]
                or digest(weight) != item["sha256"]
                or declared[relative]["sha256"] != item["sha256"]
                or not card.is_file() or digest(card) != item["model_card_sha256"]):
            raise ValueError(f"Offline encoder changed: {encoder}")
    for pth in root.rglob("python312._pth"):
        if any(path in pth.read_text(encoding="utf-8").lower() for path in ("e:\\", "d:\\")):
            raise ValueError("Embedded runtime uses a development path")
    return {"status": "passed", "candidate_manifest_sha256": index["candidate_manifest_sha256"],
            "runtimes": {key: len(value) for key, value in inventory.items()},
            "license_files": license_count, "upstream_notices": len(pinned["files"]),
            "models": {key: value["sha256"] for key, value in models.items()},
            "checks": ["all distribution license texts bundled", "upstream notices hash pinned",
                       "four model weights and cards hash pinned", "no embedded development paths"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.run_root.resolve())
    target = args.output or args.run_root / "portal/distribution-audit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
