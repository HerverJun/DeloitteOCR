"""Optional classifier weights are pinned and seeded without network access."""

import hashlib
import json
import os

import pytest

from scripts.build_base_candidate import _classification_bundle
from scripts.candidate_class_weights import install_weights


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture(tmp_path):
    source = tmp_path / "source"
    for name in ("src/image_platform/__init__.py", "schemas/project.schema.json",
                 "apps/web/dist/index.html"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    (source / "src/image_platform/training.py").write_text(
        "ENCODERS = {'resnet18': {'name': 'resnet18.a1_in1k', 'revision': '" + "a" * 40
        + "', 'sha256': '" + sha(b"fixed-weight") + "'}}", encoding="utf-8")
    card = source / "resources/third-party/resnet18-model-card.md"
    card.parent.mkdir(parents=True)
    card.write_bytes(b"model card")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"python")
    (runtime / "python312.zip").write_bytes(b"stdlib")
    site = tmp_path / "site"
    site.mkdir()
    weights = tmp_path / "weights"
    payload = weights / "resnet18/model.safetensors"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"fixed-weight")
    entry = {"name": "resnet18.a1_in1k", "revision": "a" * 40,
             "sha256": sha(payload.read_bytes()), "bytes": payload.stat().st_size,
             "license": "apache-2.0", "model_card_sha256": sha(card.read_bytes())}
    (weights / "manifest.json").write_text(
        json.dumps({"format": 1, "models": {"resnet18": entry}}), encoding="utf-8")
    return source, runtime, site, weights


def test_bundle_rejects_changed_weight_before_publication(tmp_path):
    source, runtime, site, weights = fixture(tmp_path)
    stage = tmp_path / "candidate"
    _classification_bundle(stage, source, site, runtime, weights)
    bundle = stage / "bundles/classification"
    assert (bundle / "resources/weights/resnet18/model.safetensors").read_bytes() == b"fixed-weight"
    assert "resources/weights/resnet18/model.safetensors" in {
        item["path"] for item in json.loads((bundle / "bundle.manifest.json").read_text())["files"]}
    (weights / "resnet18/model.safetensors").write_bytes(b"changed")
    with pytest.raises(ValueError, match="unavailable"):
        _classification_bundle(tmp_path / "bad", source, site, runtime, weights)


def test_seeded_weight_uses_local_cache_and_detects_corruption(tmp_path, monkeypatch):
    source, runtime, site, weights = fixture(tmp_path)
    bundle_root = tmp_path / "candidate"
    _classification_bundle(bundle_root, source, site, runtime, weights)
    bundle = bundle_root / "bundles/classification"
    monkeypatch.setenv("HF_HUB_OFFLINE", "")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "")
    data = tmp_path / "user-data"
    first = install_weights(bundle, data)
    assert first["installed"] == ["resnet18"]
    target = (data / "cache/huggingface/hub/models--timm--resnet18.a1_in1k/snapshots"
              / ("a" * 40) / "model.safetensors")
    assert target.read_bytes() == b"fixed-weight"
    assert install_weights(bundle, data)["existing"] == ["resnet18"]
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    target.write_bytes(b"corrupt-user-cache")
    assert install_weights(bundle, data)["corrupt_cache"] == ["resnet18"]
    assert target.read_bytes() == b"corrupt-user-cache"
