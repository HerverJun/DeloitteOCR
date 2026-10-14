"""Reverify and restore the fixed three-component cold backup into new paths."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup_workspace import restore as restore_platform, verify as verify_platform


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def classification_backup(repository):
    source = repository / "src/image_platform/backup.py"
    spec = importlib.util.spec_from_file_location("classification_cold_backup", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def one(path, sql, parameters=()):
    with sqlite3.connect(path) as database:
        database.row_factory = sqlite3.Row
        row = database.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None


def check(run_root, output_root):
    index = json.loads((run_root / "portal/fixed-candidate-index.json").read_text(encoding="utf-8"))
    state = json.loads((run_root / "state.json").read_text(encoding="utf-8"))
    backup = Path(index["backup_group"]).resolve(strict=True)
    group = json.loads((backup / "installation-manifest.json").read_text(encoding="utf-8"))
    if group.get("format") != 1 or group.get("mode") != "cold":
        raise ValueError("Backup group is not a cold snapshot")
    classification = classification_backup(Path(state["repositories"]["classification"]))
    components = {"platform": verify_platform(backup / "platform"),
                  "ocr": verify_platform(backup / "ocr"),
                  "classification": classification.verify(backup / "classification")}
    for name, manifest in components.items():
        if (len(manifest["files"]) != group["components"][name]["files"]
                or digest(backup / name / "manifest.json")
                != group["components"][name]["manifest_sha256"]):
            raise ValueError(f"Backup component changed: {name}")
    output_root.mkdir(parents=True, exist_ok=True)
    restored = output_root / ("restore-" + uuid.uuid4().hex)
    restored.mkdir()
    platform, ocr, classifier = (restored / name for name in
                                 ("platform-data", "ocr-data", "classification-data"))
    restore_platform(backup / "platform", platform)
    restore_platform(backup / "ocr", ocr)
    classification.restore(backup / "classification", classifier)
    proof = index["proof"]
    asset = one(platform / "assets.sqlite3",
                "SELECT root_id,relative_path,blob_hash FROM registered_asset WHERE asset_id=?",
                (proof["pdf_asset_id"],))
    if asset is None or asset["root_id"] != "uploads" or asset["blob_hash"] != proof["pdf_sha256"]:
        raise ValueError("Restored PDF source identity differs")
    source = (platform / "asset-uploads" / asset["relative_path"]).resolve()
    if not source.is_relative_to(platform / "asset-uploads") or digest(source) != proof["pdf_sha256"]:
        raise ValueError("Restored PDF original differs")
    result = one(platform / "artifact-center/artifacts.sqlite3",
                 "SELECT blob_hash FROM published_artifact WHERE blob_hash=?",
                 (proof["ocr_export_sha256"],))
    if result is None:
        raise ValueError("Restored OCR artifact metadata is missing")
    blob = platform / "artifact-center/blobs" / result["blob_hash"][:2] / result["blob_hash"]
    if digest(blob) != proof["ocr_export_sha256"]:
        raise ValueError("Restored managed OCR result differs")
    document = one(ocr / "workbench.sqlite3",
                   "SELECT id,page_count FROM documents WHERE id=?",
                   (proof["ocr_document_id"],))
    if document is None or document["page_count"] != 2:
        raise ValueError("Restored OCR document differs")
    with sqlite3.connect(ocr / "workbench.sqlite3") as db:
        pages = db.execute("SELECT page_number FROM pages WHERE document_id=? ORDER BY page_number",
                           (proof["ocr_document_id"],)).fetchall()
    if [item[0] for item in pages] != [1, 2]:
        raise ValueError("Restored OCR page locators differ")
    model = one(classifier / "platform.sqlite3",
                "SELECT package_path FROM models WHERE id=?",
                (proof["classification_model_id"],))
    if model is None:
        raise ValueError("Restored classification model is missing")
    package = Path(model["package_path"]).resolve()
    if not package.is_relative_to(classifier) or not (package / "model.onnx").is_file():
        raise ValueError("Restored classification model path differs")
    return {"status": "passed", "backup_group": str(backup), "restored_root": str(restored),
            "files": {name: len(value["files"]) for name, value in components.items()},
            "pdf_sha256": digest(source), "ocr_artifact_sha256": digest(blob),
            "ocr_pages": [item[0] for item in pages],
            "classification_model_id": proof["classification_model_id"],
            "checks": ["three component manifests intact", "new-directory restore",
                       "original PDF and managed result hashes", "native page and model paths"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    run = args.run_root.resolve()
    report = check(run, args.output_root or run / "portal/backup-check")
    target = args.report or run / "portal/backup-check.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
