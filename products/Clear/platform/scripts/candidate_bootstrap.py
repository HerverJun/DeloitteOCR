"""Start a relocatable candidate with trusted, local optional-app records."""

from __future__ import annotations

import hashlib
import http.client
from datetime import datetime, timezone
import json
import msvcrt
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
from contextlib import contextmanager

from workbench_platform.supervisor.processes import (
    require_f_path, storage_environment, process_creation_time, _process_active, _listener_pid,
)


APPS = ("ocr", "classification")
CACHE_POLICY = 1
ENTRY_FILES = ("bundle.manifest.json", "launch.cmd", "server.py",
               "runtimes/control/python.exe", "runtimes/control/python312._pth")


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path = require_f_path(path)
    temporary = path.with_name(path.name + ".new")
    require_f_path(temporary)
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


class CandidateBusy(ValueError):
    pass


class _FileLease:
    """OS-owned lease; a crash releases it without a stale-lock reset."""
    def __init__(self, path):
        self.path, self.stream = require_f_path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def close(self):
        if self.stream is not None:
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()
            self.stream = None


def _paths(root):
    root = require_f_path(root, "Program directory")
    anchor = require_f_path(root / "data", "Candidate state directory")
    configured = os.environ.get("WORKBENCH_INSTALL_DATA_DIR")
    data = require_f_path(Path(configured) if configured else anchor, "User data directory")
    if (data == root or root.is_relative_to(data)
            or (data.is_relative_to(root) and data != anchor)):
        raise ValueError("User data must not be the program directory")
    port = int(os.environ.get("WORKBENCH_PORT") or "8765")
    if not 1 <= port <= 65535:
        raise ValueError("WORKBENCH_PORT must be between 1 and 65535")
    # Check all mutable descendants before pointer/configuration publication.
    for path in (anchor / "install-data.json", anchor / "startup-verification.json",
                 anchor / "candidate-instance.json", data / "config/apps", data / "ocr-token.txt"):
        require_f_path(path)
    for app_id in APPS:
        require_f_path(data / "config/apps" / f"{app_id}.json")
        require_f_path(data / "config/apps" / f"{app_id}.install.json")
    return root, anchor, data, port


def _reuse_instance(root, anchor, data, port):
    try:
        record = json.loads((anchor / "candidate-instance.json").read_text(encoding="utf-8"))
        if (record["program_root"] != str(root) or record["data_root"] != str(data)
                or record["port"] != port):
            raise CandidateBusy("This package is active with another data directory or port")
        if (not _process_active(record["pid"], record["creation_time"])
                or _listener_pid(port) != record["pid"]):
            raise CandidateBusy("Candidate startup identity is not yet verifiable")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", "/api/platform/v1/candidate-identity")
            response = connection.getresponse()
            if response.status != 200 or json.loads(response.read(8193)) != record:
                raise CandidateBusy("Candidate page identity could not be verified")
        finally:
            connection.close()
        return f"http://127.0.0.1:{port}/"
    except CandidateBusy:
        raise
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        raise CandidateBusy("Candidate is starting or its identity needs verification; no configuration changed") from None


@contextmanager
def candidate_instance(root):
    root, anchor, data, port = _paths(root)
    lease = _FileLease(anchor / "candidate-instance.lock")
    listener = None
    try:
        if not lease.acquire():
            yield {"reused_url": _reuse_instance(root, anchor, data, port)}
            return
        listener = socket.socket()
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Keep the reserved socket for uvicorn, eliminating the check/bind race.
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        record = {"program_root": str(root), "data_root": str(data), "port": port,
                  "pid": os.getpid(), "creation_time": process_creation_time(os.getpid()),
                  "nonce": secrets.token_urlsafe(32)}
        yield {"record": record, "listener": listener, "anchor": anchor}
    finally:
        if listener:
            listener.close()
        lease.close()


def _sealed_bundle(root: Path, app_id: str, sealed: dict, progress=None) -> bool:
    prefix = f"bundles/{app_id}/"
    entries = [(name, identity) for name, identity in sealed.items() if name.startswith(prefix)]
    if not entries:
        return False
    bundle = root / "bundles" / app_id
    started = time.monotonic()
    reported = started
    if progress:
        progress(0, len(entries), None)
    for index, (name, identity) in enumerate(entries, 1):
        member = root / name
        if progress and (index == 1 or identity.get("bytes", 0) >= 16 * 1024 * 1024
                         or time.monotonic() - reported >= 0.5):
            progress(index - 1, len(entries), name.removeprefix(prefix))
            reported = time.monotonic()
        if (member.is_symlink() or not member.resolve().is_relative_to(bundle.resolve())
                or not member.is_file() or _hash(member) != identity.get("sha256")):
            return False
        size = identity.get("bytes")
        if size is not None and member.stat().st_size != size:
            return False
        if progress and (index == len(entries) or time.monotonic() - reported >= 0.5):
            progress(index, len(entries), name.removeprefix(prefix))
            reported = time.monotonic()
        if len(entries) >= 1000 and (index == 1 or index % 5000 == 0 or index == len(entries)):
            print(f"Verifying {app_id}: {index:,}/{len(entries):,} ({time.monotonic() - started:.0f}s)",
                  flush=True)
    return True


def _quick_bundle(root: Path, app_id: str, sealed: dict) -> bool:
    """Check declarations and necessary entry points; never scan all payloads."""
    bundle = root / "bundles" / app_id
    for relative in ENTRY_FILES:
        name = f"bundles/{app_id}/{relative}"
        if name not in sealed:
            if relative in {"bundle.manifest.json", "launch.cmd"}:
                return False
            continue
        member = bundle / relative
        if (member.is_symlink() or not member.resolve().is_relative_to(bundle.resolve())
                or not member.is_file() or _hash(member) != sealed[name].get("sha256")):
            return False
    return True


def _entry_hashes(app_id: str, sealed: dict) -> dict:
    return {relative: sealed[f"bundles/{app_id}/{relative}"]["sha256"] for relative in ENTRY_FILES
            if f"bundles/{app_id}/{relative}" in sealed}


def _installation_record(root: Path, app_id: str, sealed: dict, initial: dict) -> dict:
    bundle = root / "bundles" / app_id
    return {"app_id": app_id, "installed": True, "manifest_sha256": initial["manifest_sha256"],
            "launch": {"bundle_root": str(bundle.resolve()),
                       "executable": str((bundle / "launch.cmd").resolve()),
                       "working_directory": str(bundle.resolve()), "allowed_arguments": [],
                       "executable_sha256": sealed[f"bundles/{app_id}/launch.cmd"]["sha256"],
                       "bundle_manifest_sha256": sealed[f"bundles/{app_id}/bundle.manifest.json"]["sha256"]}}


def _verified_record(root: Path, app_id: str, sealed: dict, initial: dict, progress=None) -> dict:
    if _quick_bundle(root, app_id, sealed) and _sealed_bundle(root, app_id, sealed, progress):
        return _installation_record(root, app_id, sealed, initial)
    return {**initial, "reason": "应用文件校验失败，请检查程序包后重新完整校验"}


def _read_successes(path: Path, identity: dict) -> dict:
    try:
        if path.is_symlink():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or any(value.get(key) != item for key, item in identity.items())
                or not isinstance(value.get("apps"), dict)):
            return {}
        return {app_id: item for app_id, item in value["apps"].items() if app_id in APPS}
    except (OSError, ValueError, TypeError):
        return {}


def configure(root: Path, *, background: bool = False, force_verify: bool = False,
              _instance_owned: bool = False):
    _, anchor, _, _ = _paths(root)
    instance = _FileLease(anchor / "candidate-instance.lock")
    configuration = _FileLease(anchor / "candidate-config.lock")
    def finished():
        configuration.close()
        instance.close()
    try:
        if not _instance_owned and not instance.acquire():
            raise CandidateBusy("Candidate is active; configuration was not changed")
        if not configuration.acquire():
            raise CandidateBusy("Candidate configuration is already being prepared")
        return _configure(root, background=background, force_verify=force_verify, _finished=finished)
    except BaseException:
        finished()
        raise


def _configure(root: Path, *, background: bool = False, force_verify: bool = False, _finished=lambda: None):
    root, anchor, data, _ = _paths(root)
    environment = storage_environment(data)
    os.environ.update(environment)
    tempfile.tempdir = None
    anchor.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    _write_json(anchor / "install-data.json", {"data_root": str(data)})
    config = data / "config" / "apps"
    config.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (root / "candidate-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    sealed = manifest.get("files", {})
    if manifest.get("format") not in {2, 3} or not isinstance(sealed, dict):
        raise ValueError("Candidate manifest is unavailable")
    cache_path = anchor / "startup-verification.json"
    identity = {"schema": 1, "policy": CACHE_POLICY, "program_root": str(root),
                "candidate_sha256": hashlib.sha256(manifest_bytes).hexdigest()}
    forced = force_verify or os.environ.get("WORKBENCH_FORCE_VERIFY") == "1"
    if forced:
        # A failed forced check must never leave an old success reusable.
        cache_path.unlink(missing_ok=True)
    successes = {} if forced else _read_successes(cache_path, identity)
    records, pending, available, cached, totals = {}, [], [], set(), {}
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
            available.append(app_id)
            totals[app_id] = sum(name.startswith(f"bundles/{app_id}/") for name in sealed)
            saved = successes.get(app_id)
            try:
                reuse = (isinstance(saved, dict) and saved.get("full_verified") is True
                         and type(saved.get("checked_files")) is int
                         and saved["checked_files"] == totals[app_id]
                         and isinstance(saved.get("verified_at"), str)
                         and datetime.fromisoformat(saved["verified_at"]).tzinfo is not None
                         and _quick_bundle(root, app_id, sealed))
            except (OSError, ValueError, TypeError, KeyError):
                reuse = False
            if reuse:
                record = _installation_record(root, app_id, sealed, record)
                cached.add(app_id)
            else:
                record["reason"] = "正在校验应用文件"
                pending.append(app_id)
                successes.pop(app_id, None)
        else:
            successes.pop(app_id, None)
        records[app_id] = record
        # Rebuild trusted launch paths for this data directory, even on reuse.
        _write_json(config / f"{app_id}.install.json", record)
    from workbench_platform.catalog import routes
    from workbench_platform.catalog.registry import AppRegistry

    registry = AppRegistry(config, install_root=root / "bundles",
                           verifying=tuple(available) if background and pending else ())
    for app_id in available:
        registry.update_verification(app_id, phase="cached" if app_id in cached else "waiting",
            checked_files=0, total_files=totals[app_id], message=(
                "已复用首次完整校验记录" + ("，等待其他应用完成" if pending else "")
                if app_id in cached else "等待完整校验，完成后将保存成功记录"))
    if not pending:
        for app_id in cached:
            registry.trust_startup_verification(app_id, _entry_hashes(app_id, sealed))
    routes._registry = registry
    os.environ["WORKBENCH_DATA_DIR"] = str(data)
    os.environ["WORKBENCH_DATA_PYTHON"] = str(root / "runtimes" / "data" / "python.exe")
    os.environ['WORKBENCH_FORENSICS_PYTHON'] = str(root / 'runtimes/forensics/python.exe')
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
                def progress(checked, total, current_file):
                    registry.update_verification(app_id, phase="checking", checked_files=checked,
                        total_files=total, current_file=current_file, message="正在完整校验应用文件")
                try:
                    records[app_id] = _verified_record(root, app_id, sealed, records[app_id], progress)
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                    records[app_id] = {**records[app_id], "reason":
                        f"应用文件校验失败（{type(error).__name__}），请检查程序包后重新完整校验"}
                previous = registry.verification(app_id)
                passed = records[app_id]["installed"]
                registry.update_verification(app_id, phase="passed" if passed else "failed",
                    checked_files=totals[app_id] if passed else previous.checked_files,
                    total_files=totals[app_id], current_file=None if passed else previous.current_file,
                    message="完整校验已完成，等待发布启动状态" if passed else records[app_id]["reason"])
            for app_id, record in records.items():
                _write_json(config / f"{app_id}.install.json", record)
            # Publish once, after every app has a result, so a later catalog
            # generation cannot invalidate an app launched earlier in startup.
            registry.reload(verification_complete=True)
            for app_id in available:
                if registry.installations[app_id].installed:
                    registry.trust_startup_verification(app_id, _entry_hashes(app_id, sealed))
            for app_id in pending:
                if records[app_id]["installed"] and registry.installations[app_id].installed:
                    successes[app_id] = {"full_verified": True, "checked_files": totals[app_id],
                                         "verified_at": datetime.now(timezone.utc).isoformat()}
                    registry.update_verification(app_id, phase="passed", checked_files=totals[app_id],
                        total_files=totals[app_id], message="首次完整校验已通过，下次启动将复用此记录")
            for app_id in cached:
                registry.update_verification(app_id, phase="cached", checked_files=0,
                    total_files=totals[app_id], message="已复用首次完整校验记录")
            try:
                _write_json(cache_path, {**identity, "apps": successes})
            except OSError:
                for app_id in pending:
                    if records[app_id]["installed"]:
                        registry.update_verification(app_id, phase="passed", checked_files=totals[app_id],
                            total_files=totals[app_id], message="完整校验已通过，但成功记录保存失败，下次启动将重新校验")
                print("Could not save startup verification; next start will check again", flush=True)
        except Exception as error:
            if not background:
                raise
            registry.reload(verification_failed=True)
            for app_id in available:
                previous = registry.verification(app_id)
                registry.update_verification(app_id, phase="failed", checked_files=previous.checked_files,
                    total_files=totals[app_id], message="校验结果未能发布，请检查数据目录后重新启动")
            print(f"Optional-app verification could not finish: {type(error).__name__}", flush=True)
        finally:
            _finished()

    if background and pending:
        threading.Thread(target=verify_packages, name="candidate-verification", daemon=True).start()
    elif pending or not background:
        verify_packages()
    else:
        _finished()
    return registry


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    with candidate_instance(root) as instance:
        if "reused_url" in instance:
            print("Verified existing candidate: " + instance["reused_url"], flush=True)
            return
        configure(root, background=True, _instance_owned=True)
        import uvicorn
        from workbench_platform.app import app
        record = instance["record"]
        @app.get("/api/platform/v1/candidate-identity", include_in_schema=False)
        def candidate_identity():
            return record
        # app.py registers the portal catchall during import. This runtime-only
        # identity route must precede it so duplicate starts can verify ownership.
        app.router.routes.insert(0, app.router.routes.pop())
        _write_json(instance["anchor"] / "candidate-instance.json", record)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=record["port"]))
        server.run(sockets=[instance["listener"]])


if __name__ == "__main__":
    main()
