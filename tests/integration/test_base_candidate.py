"""Candidate verification detects changed program bytes while allowing data writes."""

import hashlib
import json

import pytest

from scripts.build_base_candidate import verify


def test_candidate_manifest_rejects_program_tampering(tmp_path):
    candidate = tmp_path / "candidate"
    files = {}
    for name in ("launch.cmd", "app/apps/portal/dist/index.html",
                 "runtimes/platform/python.exe", "runtimes/data/python.exe",
                 "dependency-inventory.json"):
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = name.encode()
        target.write_bytes(content)
        files[name] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (candidate / "candidate-manifest.json").write_text(
        json.dumps({"format": 1, "qualification": "engineering_preview", "files": files}),
        encoding="utf-8")
    (candidate / "data").mkdir()
    (candidate / "data" / "user.txt").write_text("later user data", encoding="utf-8")
    assert verify(candidate)["files"] == len(files)
    (candidate / "launch.cmd").write_bytes(b"modified launcher")
    with pytest.raises(ValueError, match="changed"):
        verify(candidate)
