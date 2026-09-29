"""Build a hash-locked engine ZIP, optionally using existing local GLM models."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


WEIGHTS = {".safetensors", ".gguf", ".pdiparams", ".bin", ".pt", ".pth", ".onnx"}
SKIP_DIRS = {".git", ".cache", ".huggingface", "__pycache__"}


def model_files(root):
    if not root.is_dir():
        raise ValueError(f"Model directory does not exist: {root}")
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts)
    )
    if not any(path.suffix.lower() in WEIGHTS for path in files):
        raise ValueError(f"Model directory has no weight file: {root}")
    return files


def build(args):
    bundle = args.bundle.resolve()
    output = args.output.resolve()
    spec = json.loads((bundle / "config/engines.json").read_text("utf-8"))[args.engine]
    if output.exists():
        raise ValueError("Use a new engine package filename")
    if output.is_relative_to(bundle):
        raise ValueError("Output ZIP must be outside the source bundle")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", args.id):
        raise ValueError("Invalid engine package ID")
    if not 1 <= len(args.version) <= 120:
        raise ValueError("Invalid engine package version")
    if (args.glm_model_dir or args.layout_model_dir) and args.engine != "glm":
        raise ValueError("Local GLM model directories require --engine glm")
    if args.layout_model_dir and not args.glm_model_dir:
        raise ValueError("--layout-model-dir requires --glm-model-dir")

    overrides = {}
    if args.glm_model_dir:
        overrides["GLM-OCR"] = args.glm_model_dir.resolve()
        if args.layout_model_dir:
            overrides["PP-DocLayoutV3_safetensors"] = args.layout_model_dir.resolve()
    if overrides and "PP-DocLayoutV3_safetensors" not in overrides:
        layout = bundle / "models/PP-DocLayoutV3_safetensors"
        if not layout.is_dir():
            raise ValueError("GLM also needs --layout-model-dir or a bundled PP-DocLayoutV3_safetensors model")

    required = [
        bundle / "app/ocr_workbench/engine_host.py",
        bundle / "app/ocr_workbench/offline.py",
        bundle / "runtimes" / args.engine / "python.exe",
        bundle / "runtimes" / args.engine / "python312._pth",
        bundle / "runtimes" / args.engine / "python312.dll",
        bundle / "runtimes" / args.engine / "python312.zip",
        bundle / "locks" / f"{args.engine}.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("Complete offline engine runtime is required: " + ", ".join(missing))

    sources = {}
    for name in spec["models"]:
        source = overrides.get(name, bundle / "models" / name)
        files = model_files(source)
        if name in overrides:
            if not (source / "config.json").is_file():
                raise ValueError(f"Model config.json is missing: {source}")
            if name == "GLM-OCR" and not (source / "preprocessor_config.json").is_file():
                raise ValueError(f"GLM processor files are missing: {source}")
            if name == "GLM-OCR" and not any((source / filename).is_file()
                                              for filename in ("tokenizer.json", "tokenizer.model")):
                raise ValueError(f"GLM tokenizer files are missing: {source}")
        elif not (source / "source-manifest.json").is_file():
            raise ValueError(f"Model provenance is missing: {source}")
        if output.is_relative_to(source):
            raise ValueError("Output ZIP must be outside the model directories")
        sources[name] = (source, files)

    entries = []
    for root in [bundle / "app", bundle / "runtimes" / args.engine]:
        entries.extend((path.relative_to(bundle).as_posix(), path) for path in root.rglob("*")
                       if path.is_file() and "__pycache__" not in path.parts)
    if args.engine == "hunyuan":
        root = bundle / "runtimes/llama"
        entries.extend((path.relative_to(bundle).as_posix(), path) for path in root.rglob("*") if path.is_file())
        license_path = bundle / "licenses/llama.cpp-LICENSE"
        if license_path.is_file():
            entries.append((license_path.relative_to(bundle).as_posix(), license_path))
    entries.extend((path.relative_to(bundle).as_posix(), path) for path in (bundle / "config").glob("*")
                   if path.is_file() and path.name != "engines.json")
    entries.extend((path.relative_to(bundle).as_posix(), path) for path in (bundle / "locks").glob("*")
                   if path.is_file() and path.stem in {args.engine, "models"}
                   and not (overrides and path.name == "models.json"))
    for name, (source, files) in sources.items():
        entries.extend((f"models/{name}/{path.relative_to(source).as_posix()}", path)
                       for path in files if name not in overrides or path.name != "source-manifest.json")

    if len({name.casefold() for name, _ in entries}) != len(entries):
        raise ValueError("Package has duplicate or case-conflicting paths")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "protocol": "file-ipc-v1", "id": args.id,
        "engine": args.engine, "name": spec["name"], "version": args.version, "files": [],
    }
    partial = output.with_suffix(".zip.part")

    def add_bytes(archive, relative, data):
        archive.writestr(relative, data)
        manifest["files"].append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True, compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            config = json.dumps({args.engine: spec}, ensure_ascii=False, indent=2).encode("utf-8")
            add_bytes(archive, "config/engines.json", config)
            model_digests = {name: hashlib.sha256() for name in overrides}
            for relative, path in sorted(entries):
                digest = hashlib.sha256()
                size = 0
                info = zipfile.ZipInfo.from_file(path, relative)
                info.compress_type = (zipfile.ZIP_STORED if path.suffix.lower() in
                                      {".dll", ".pyd", ".pdiparams", ".safetensors", ".gguf"}
                                      else zipfile.ZIP_DEFLATED)
                info._compresslevel = 1
                with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                    while chunk := source.read(4 * 1024**2):
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                sha = digest.hexdigest()
                manifest["files"].append({"path": relative, "bytes": size, "sha256": sha})
                for name in overrides:
                    prefix = f"models/{name}/"
                    if relative.startswith(prefix):
                        model_digests[name].update(relative[len(prefix):].encode("utf-8") + b"\0" + bytes.fromhex(sha))
            model_locks = {}
            for name, (source, _) in sources.items():
                if name in overrides:
                    record = {"repo": "local/" + name, "revision": "local-sha256-" + model_digests[name].hexdigest()}
                    add_bytes(archive, f"models/{name}/source-manifest.json",
                              json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
                else:
                    record = json.loads((source / "source-manifest.json").read_text("utf-8"))
                model_locks[name] = {"repo": record["repo"], "revision": record["revision"]}
            if overrides:
                add_bytes(archive, "locks/models.json", json.dumps(model_locks, ensure_ascii=False, indent=2).encode("utf-8"))
            archive.writestr("engine-package.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output.with_suffix(".sha256").write_text(digest + "  " + output.name + "\n", "ascii")
    print(json.dumps({"path": str(output), "files": len(manifest["files"]) + 1,
                      "bytes": output.stat().st_size, "sha256": digest}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True, help="Prepared offline app and engine runtime")
    parser.add_argument("--engine", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--glm-model-dir", type=Path, help="Existing local GLM-OCR model directory")
    parser.add_argument("--layout-model-dir", type=Path, help="Existing local PP-DocLayoutV3_safetensors directory")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
