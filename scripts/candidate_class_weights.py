"""Seed verified optional encoder weights into the native offline cache."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil


ID = re.compile(r"[a-z0-9_]+\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install_weights(bundle: Path, data: Path) -> dict:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    manifest = bundle / "resources" / "weights" / "manifest.json"
    if not manifest.is_file():
        return {"installed": [], "existing": [], "corrupt_cache": []}
    declaration = json.loads(manifest.read_text(encoding="utf-8"))
    models = declaration.get("models")
    if declaration.get("format") != 1 or not isinstance(models, dict):
        raise ValueError("Invalid offline model resource manifest")
    result = {"installed": [], "existing": [], "corrupt_cache": []}
    for encoder, item in models.items():
        name, revision, sha = item["name"], item["revision"], item["sha256"]
        if (not ID.fullmatch(encoder) or not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9_.]+", name)
                or not REVISION.fullmatch(revision) or not SHA.fullmatch(sha)
                or item.get("license") != "apache-2.0"):
            raise ValueError("Invalid offline model identity")
        source = bundle / "resources" / "weights" / encoder / "model.safetensors"
        card = bundle / "resources" / "third-party" / f"{encoder}-model-card.md"
        if (source.is_symlink() or not source.is_file()
                or source.stat().st_size != item["bytes"] or _hash(source) != sha
                or not card.is_file() or _hash(card) != item["model_card_sha256"]):
            raise ValueError("Offline model bytes or provenance changed")
        target = (data / "cache" / "huggingface" / "hub"
                  / ("models--timm--" + name) / "snapshots" / revision / "model.safetensors")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            result["existing" if _hash(target) == sha else "corrupt_cache"].append(encoder)
            continue
        pending = target.with_suffix(".pending")
        try:
            with source.open("rb") as input_stream, pending.open("wb") as output:
                shutil.copyfileobj(input_stream, output, 1024 * 1024)
            if _hash(pending) != sha:
                raise ValueError("Offline model copy changed")
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)
        result["installed"].append(encoder)
    return result
