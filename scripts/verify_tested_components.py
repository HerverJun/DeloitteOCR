"""Bind frozen functional tests to a final archive after documentation updates."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tested-manifest", type=Path, required=True)
    p.add_argument("--inference-manifest", type=Path, help="Earlier standalone-adapter evaluation; service HTTP metadata is outside that execution path")
    p.add_argument("--final-manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    protected = {"app", "config", "fixtures", "launcher", "locks", "models", "runtimes", "web"}
    manifests = [json.loads(path.read_text("utf-8")) for path in [a.tested_manifest, a.final_manifest]]
    files = []
    for manifest in manifests:
        records = manifest["files"]
        if len({r["path"] for r in records}) != len(records):
            raise ValueError("Duplicate manifest entries")
        files.append({r["path"]: (r["bytes"], r["sha256"]) for r in records})
    changes = []
    relevant = []
    for path in sorted(set(files[0]) | set(files[1])):
        is_protected = path.split("/")[0] in protected or path.endswith(".cmd")
        if is_protected:
            relevant.append(path)
        if files[0].get(path) != files[1].get(path):
            changes.append({"path": path, "protected": is_protected, "before": files[0].get(path), "after": files[1].get(path)})
    report = {
        "passed": bool(relevant) and not any(c["protected"] for c in changes),
        "scope": "Compares manifests only. Full archive SHA/CRC verification is separately required. Application/config/fixtures/launcher/locks/models/runtimes/web/command entry points must be byte-identical; changed documents, audit receipts and tools are enumerated, not silently treated as tested.",
        "tested_manifest_sha256": hashlib.sha256(a.tested_manifest.read_bytes()).hexdigest(),
        "final_manifest_sha256": hashlib.sha256(a.final_manifest.read_bytes()).hexdigest(),
        "protected_files": len(relevant),
        "changes": changes,
    }
    if a.inference_manifest:
        earlier = json.loads(a.inference_manifest.read_text("utf-8"))["files"]
        old = {r["path"]: (r["bytes"], r["sha256"]) for r in earlier}
        if len(old) != len(earlier):
            raise ValueError("Duplicate inference manifest entries")
        changes = []
        for path in sorted(set(old) | set(files[1])):
            # The standalone EngineAdapter evaluation never imports the HTTP
            # service. Its final bytes are covered by the application manifest.
            protected_inference = (path.split("/")[0] in protected or path.endswith(".cmd")) and path != "app/ocr_workbench/service.py"
            if old.get(path) != files[1].get(path):
                changes.append({"path": path, "protected": protected_inference, "before": old.get(path), "after": files[1].get(path)})
        report["inference"] = {
            "scope": "Standalone adapter benchmark and fidelity. Only HTTP service.py is outside this path; it must instead match the later full application test manifest above.",
            "manifest_sha256": hashlib.sha256(a.inference_manifest.read_bytes()).hexdigest(),
            "passed": bool(old) and not any(c["protected"] for c in changes),
            "changes": changes,
        }
        report["passed"] &= report["inference"]["passed"]
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": report["passed"], "protected_files": len(relevant), "changed_files": len(changes)}))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
