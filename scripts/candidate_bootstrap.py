"""Start a relocatable candidate with trusted, local optional-app records."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time


APPS = ("ocr", "classification")


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _sealed_bundle(root: Path, app_id: str, sealed: dict) -> bool:
    prefix = f"bundles/{app_id}/"
    entries = [(name, identity) for name, identity in sealed.items() if name.startswith(prefix)]
    if not entries:
        return False
    bundle = root / "bundles" / app_id
    started = time.monotonic()
    for index, (name, identity) in enumerate(entries, 1):
        member = root / name
        if (member.is_symlink() or not member.resolve().is_relative_to(bundle.resolve())
                or not member.is_file() or _hash(member) != identity.get("sha256")):
            return False
        size = identity.get("bytes")
        if size is not None and member.stat().st_size != size:
            return False
        if len(entries) >= 1000 and (index == 1 or index % 5000 == 0 or index == len(entries)):
            print(f"Verifying {app_id}: {index:,}/{len(entries):,} ({time.monotonic() - started:.0f}s)",
                  flush=True)
    return True


def _verified_record(root: Path, app_id: str, sealed: dict, initial: dict) -> dict:
    record = {**initial, "reason": "可选应用包校验失败"}
    bundle = root / "bundles" / app_id
    relative_manifest = f"bundles/{app_id}/bundle.manifest.json"
    relative_executable = f"bundles/{app_id}/launch.cmd"
    bundle_manifest, executable = bundle / "bundle.manifest.json", bundle / "launch.cmd"
    expected_manifest = sealed[relative_manifest]["sha256"]
    expected_executable = sealed[relative_executable]["sha256"]
    if (_hash(bundle_manifest) == expected_manifest
            and _hash(executable) == expected_executable
            and _sealed_bundle(root, app_id, sealed)):
        record = {"app_id": app_id, "installed": True,
                  "manifest_sha256": initial["manifest_sha256"],
                  "launch": {"bundle_root": str(bundle.resolve()),
                             "executable": str(executable.resolve()),
                             "working_directory": str(bundle.resolve()),
                             "allowed_arguments": [],
                             "executable_sha256": expected_executable,
                             "bundle_manifest_sha256": expected_manifest}}
    return record


def configure(root: Path, *, background: bool = False):
    root = root.resolve()
    anchor = root / "data"
    anchor.mkdir(exist_ok=True)
    configured = os.environ.get("WORKBENCH_INSTALL_DATA_DIR")
    data = Path(configured).resolve() if configured else anchor
    if (data == root or root.is_relative_to(data)
            or (data.is_relative_to(root) and data != anchor)):
        raise ValueError("User data must not be the program directory")
    data.mkdir(exist_ok=True)
    _write_json(anchor / "install-data.json", {"data_root": str(data)})
    config = data / "config" / "apps"
    config.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((root / "candidate-manifest.json").read_text(encoding="utf-8"))
    sealed = manifest.get("files", {})
    if manifest.get("format") not in {2, 3} or not isinstance(sealed, dict):
        raise ValueError("Candidate manifest is unavailable")
    records, pending = {}, []
    for app_id in APPS:
        source = root / "app" / "config" / "apps" / f"{app_id}.json"
        target = config / f"{app_id}.json"
        target.write_bytes(source.read_bytes())
        record = {"app_id": app_id, "installed": False,
                  "manifest_sha256": _hash(target), "reason": "可选应用包未安装"}
        bundle = root / "bundles" / app_id
        relative_manifest = f"bundles/{app_id}/bundle.manifest.json"
        relative_executable = f"bundles/{app_id}/launch.cmd"
        bundle_manifest, executable = bundle / "bundle.manifest.json", bundle / "launch.cmd"
        if (bundle.is_dir() and bundle_manifest.is_file() and executable.is_file()
                and relative_manifest in sealed and relative_executable in sealed):
            record["reason"] = "正在校验应用文件"
            pending.append(app_id)
        records[app_id] = record
        # Reset prior launch records before serving the portal. No previous
        # successful check is reused across starts.
        _write_json(config / f"{app_id}.install.json", record)
    from workbench_platform.catalog import routes
    from workbench_platform.catalog.registry import AppRegistry

    registry = AppRegistry(config, install_root=root / "bundles",
                           verifying=tuple(pending) if background else ())
    routes._registry = registry
    os.environ["WORKBENCH_DATA_DIR"] = str(data)
    os.environ["WORKBENCH_DATA_PYTHON"] = str(root / "runtimes" / "data" / "python.exe")
    os.environ["WORKBENCH_PORTAL_DIST"] = str(root / "app" / "apps" / "portal" / "dist")
    os.environ["PYTHONUTF8"] = "1"
    if (root / "bundles" / "ocr").is_dir():
        token = data / "ocr-token.txt"
        if not token.exists():
            token.write_text(secrets.token_urlsafe(36), encoding="utf-8")
        os.environ["WORKBENCH_OCR_TOKEN_FILE"] = str(token)
    def verify_packages():
        try:
            for app_id in pending:
                try:
                    records[app_id] = _verified_record(root, app_id, sealed, records[app_id])
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    records[app_id] = {**records[app_id], "reason": "可选应用包校验失败"}
            for app_id, record in records.items():
                _write_json(config / f"{app_id}.install.json", record)
            # Publish once, after every app has a result, so a later catalog
            # generation cannot invalidate an app launched earlier in startup.
            registry.reload(verification_complete=True)
        except Exception as error:
            if not background:
                raise
            registry.reload(verification_failed=True)
            print(f"Optional-app verification could not finish: {type(error).__name__}", flush=True)

    if background and pending:
        threading.Thread(target=verify_packages, name="candidate-verification", daemon=True).start()
    else:
        verify_packages()
    return registry


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    configure(root, background=True)
    port = int(os.environ.get("WORKBENCH_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("WORKBENCH_PORT must be between 1 and 65535")
    import uvicorn

    uvicorn.run("workbench_platform.app:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
