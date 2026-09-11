"""Read every archived byte and compare it with the packaged SHA-256 manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import zipfile

p = argparse.ArgumentParser()
p.add_argument("archive", type=Path)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
started = last = time.monotonic()
count = total = 0
with zipfile.ZipFile(a.archive) as archive:
    names = archive.namelist()
    assert len(names) == len(set(names)), "Duplicate archive entries"
    manifest_bytes = archive.read("OfflineOCR/manifest.json")
    manifest = json.loads(manifest_bytes)
    expected = {"OfflineOCR/" + r["path"] for r in manifest["files"]}
    assert set(names) == expected | {"OfflineOCR/manifest.json"}, "Manifest inventory differs"
    for record in manifest["files"]:
        digest = hashlib.sha256()
        size = 0
        with archive.open("OfflineOCR/" + record["path"]) as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        assert size == record["bytes"], record["path"]
        assert digest.hexdigest() == record["sha256"], record["path"]
        count += 1
        total += size
        if time.monotonic() - last >= 20:
            print(json.dumps({"verified_files": count, "verified_bytes": total}), flush=True)
            last = time.monotonic()
report = {"passed": True, "archive": a.archive.name, "files": count, "bytes": total,
          "bundle_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
          "scope": "Every ZIP entry decompressed with CRC verification and independently SHA-256 compared; exact inventory, no duplicate entries",
          "seconds": time.monotonic() - started}
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
print(json.dumps(report), flush=True)
