"""Build a complete, hash-locked independent engine package from a verified bundle."""

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

p = argparse.ArgumentParser()
p.add_argument("--bundle", type=Path, required=True)
p.add_argument("--engine", required=True)
p.add_argument("--id", required=True)
p.add_argument("--version", required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
spec = json.loads((a.bundle / "config/engines.json").read_text("utf-8"))[a.engine]
if a.output.exists():
    raise ValueError("Use a new engine package filename")
a.output.parent.mkdir(parents=True, exist_ok=True)
roots = [
    a.bundle / "app",
    a.bundle / "runtimes" / a.engine,
    *[a.bundle / "models" / name for name in spec["models"]],
]
if a.engine == "hunyuan":
    roots.append(a.bundle / "runtimes/llama")
paths = [
    path
    for root in roots
    for path in root.rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
]
paths.extend(
    path
    for path in (a.bundle / "config").glob("*")
    if path.is_file() and path.name != "engines.json"
)
paths.extend(
    path for path in (a.bundle / "locks").glob("*") if path.stem in {a.engine, "models"}
)
license = a.bundle / "licenses/llama.cpp-LICENSE"
if a.engine == "hunyuan" and license.exists():
    paths.append(license)
manifest = {
    "schema_version": 1,
    "protocol": "file-ipc-v1",
    "id": a.id,
    "engine": a.engine,
    "name": spec["name"],
    "version": a.version,
    "files": [],
}
partial = a.output.with_suffix(".zip.part")
with zipfile.ZipFile(
    partial, "w", allowZip64=True, compression=zipfile.ZIP_DEFLATED, compresslevel=1
) as archive:
    config = json.dumps({a.engine: spec}, ensure_ascii=False, indent=2).encode()
    archive.writestr("config/engines.json", config)
    manifest["files"].append(
        {
            "path": "config/engines.json",
            "bytes": len(config),
            "sha256": hashlib.sha256(config).hexdigest(),
        }
    )
    for path in sorted(set(paths)):
        relative = path.relative_to(a.bundle).as_posix()
        digest = hashlib.sha256()
        size = 0
        info = zipfile.ZipInfo.from_file(path, relative)
        info.compress_type = (
            zipfile.ZIP_STORED
            if path.suffix in {".dll", ".pyd", ".pdiparams", ".safetensors", ".gguf"}
            else zipfile.ZIP_DEFLATED
        )
        info._compresslevel = 1
        with path.open("rb") as source, archive.open(
            info, "w", force_zip64=True
        ) as output:
            while chunk := source.read(4 * 1024**2):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        manifest["files"].append(
            {"path": relative, "bytes": size, "sha256": digest.hexdigest()}
        )
    archive.writestr(
        "engine-package.json", json.dumps(manifest, ensure_ascii=False, indent=2)
    )
partial.replace(a.output)
with a.output.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
a.output.with_suffix(".sha256").write_text(
    digest + "  " + a.output.name + "\n", "ascii"
)
print(
    json.dumps(
        {
            "path": str(a.output),
            "files": len(paths) + 1,
            "bytes": a.output.stat().st_size,
            "sha256": digest,
        }
    )
)
