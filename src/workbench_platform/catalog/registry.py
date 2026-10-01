"""Load only trusted installation records and their hash-pinned manifests.

No application package, model, launcher or proxy is imported or contacted here.
An installation record is local operator configuration; HTTP requests never
provide its path or execution fields. Actual launch/probes belong to T012.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from packages.contracts.models import AppHealth, AppManifest, AppManifestPage, Health, PageQuery

KNOWN_APPS = ("classification", "ocr")
SHA = re.compile(r"^[a-f0-9]{64}$")
ARG = re.compile(r"^[A-Za-z0-9_./:=+@-]+$")
FILE_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config/apps"
PLATFORM_VERSION = "0.1.0"
VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class SnapshotExpired(ValueError):
    pass


class CatalogConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Installation:
    app_id: str
    installed: bool
    reason: str
    executable: Path | None = None
    working_directory: Path | None = None
    bundle_manifest_sha256: str | None = None
    executable_sha256: str | None = None
    allowed_arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogSnapshot:
    manifests: Mapping[str, AppManifest]
    installations: Mapping[str, Installation]
    failures: Mapping[str, str]
    revision: int
    verifying: frozenset[str] = frozenset()


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CatalogConfigError("Duplicate trusted configuration key")
        value[key] = item
    return value


def _inside(root: Path, candidate: Path) -> Path:
    if not candidate.is_absolute():
        raise CatalogConfigError("Installation path must be absolute")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise CatalogConfigError("Installation path escapes trusted bundle root")
    return resolved


def _bundle_path(value: str) -> tuple[str, ...]:
    """Validate a portable, bundle-relative declaration without reading its payload."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise CatalogConfigError("Invalid bundle manifest path")
    parts = value.split("/")
    if any(part in ("", ".", "..") or not FILE_PART.fullmatch(part) for part in parts):
        raise CatalogConfigError("Invalid bundle manifest path")
    return tuple(parts)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class AppRegistry:
    def __init__(self, config_dir: Path = DEFAULT_CONFIG, *, install_root: Path | None = None,
                 verifying: tuple[str, ...] = ()):
        self.config_dir = Path(config_dir).resolve()
        self.install_root = Path(install_root or self.config_dir.parent / "bundles").resolve()
        self._lock = threading.RLock()
        pending = frozenset(verifying)
        if not pending.issubset(KNOWN_APPS):
            raise ValueError("Unknown application verification")
        self._snapshot = CatalogSnapshot(MappingProxyType({}), MappingProxyType({}), MappingProxyType({}), 1,
                                         pending)
        self._observations: dict[str, AppHealth] = {}
        self.reload()

    @property
    def manifests(self) -> Mapping[str, AppManifest]:
        return self._snapshot.manifests

    @property
    def installations(self) -> Mapping[str, Installation]:
        return self._snapshot.installations

    @property
    def failures(self) -> Mapping[str, str]:
        return self._snapshot.failures

    @property
    def snapshot_revision(self) -> int:
        return self._snapshot.revision

    def _file(self, name: str) -> Path:
        path = (self.config_dir / name).resolve()
        if not path.is_relative_to(self.config_dir):
            raise CatalogConfigError("Catalog path escapes trusted directory")
        return path

    def _load_one(self, app_id: str) -> tuple[AppManifest, Installation]:
        record = json.loads(self._file(f"{app_id}.install.json").read_text(encoding="utf-8"),
                            object_pairs_hook=_unique_pairs)
        if not isinstance(record, dict) or record.get("app_id") != app_id or type(record.get("installed")) is not bool:
            raise CatalogConfigError("Invalid trusted installation identity")
        digest = record.get("manifest_sha256")
        if not isinstance(digest, str) or not SHA.fullmatch(digest):
            raise CatalogConfigError("Invalid manifest digest")
        manifest_bytes = self._file(f"{app_id}.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != digest:
            raise CatalogConfigError("Manifest hash differs from trusted installation record")
        manifest = AppManifest.model_validate(json.loads(manifest_bytes, object_pairs_hook=_unique_pairs))
        if manifest.app_id != app_id:
            raise CatalogConfigError("Manifest application identity differs")
        minimum = VERSION.fullmatch(manifest.min_platform_version)
        if minimum is None or tuple(map(int, minimum.groups())) > (0, 1, 0):
            raise CatalogConfigError("Application requires an unsupported platform version")
        if not record["installed"]:
            if set(record) != {"app_id", "manifest_sha256", "installed", "reason"}:
                raise CatalogConfigError("Uninstalled record has unexpected execution fields")
            reason = record["reason"]
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
                raise CatalogConfigError("Invalid installation reason")
            return manifest, Installation(app_id, False, reason)
        if set(record) != {"app_id", "manifest_sha256", "installed", "launch"}:
            raise CatalogConfigError("Installed record has unexpected fields")
        launch = record["launch"]
        if not isinstance(launch, dict) or set(launch) != {
            "bundle_root", "executable", "working_directory", "bundle_manifest_sha256",
            "executable_sha256", "allowed_arguments"
        }:
            raise CatalogConfigError("Invalid trusted launch record")
        root = _inside(self.install_root, Path(launch["bundle_root"]))
        executable = _inside(root, Path(launch["executable"]))
        cwd = _inside(root, Path(launch["working_directory"]))
        manifest_sha = launch["bundle_manifest_sha256"]
        executable_sha = launch["executable_sha256"]
        if (not executable.is_file() or not cwd.is_dir() or
                not isinstance(manifest_sha, str) or not SHA.fullmatch(manifest_sha) or
                not isinstance(executable_sha, str) or not SHA.fullmatch(executable_sha)):
            raise CatalogConfigError("Trusted bundle identity missing")
        # The trusted record pins the bundle's *declaration*, not every payload
        # byte. Other payloads are verified at distribution/launch, not on a
        # catalog or home-page request (which must avoid loading model files).
        with (root / "bundle.manifest.json").open("rb") as stream:
            bundle_manifest = stream.read(1_048_577)
        if len(bundle_manifest) > 1_048_576 or hashlib.sha256(bundle_manifest).hexdigest() != manifest_sha:
            raise CatalogConfigError("Trusted bundle manifest hash changed")
        declaration = json.loads(bundle_manifest, object_pairs_hook=_unique_pairs)
        if not isinstance(declaration, dict) or set(declaration) != {"app_id", "files"} or declaration["app_id"] != app_id:
            raise CatalogConfigError("Invalid trusted bundle manifest")
        files = declaration["files"]
        if not isinstance(files, list) or not 1 <= len(files) <= 10000:
            raise CatalogConfigError("Invalid trusted bundle files")
        declared: dict[tuple[str, ...], str] = {}
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise CatalogConfigError("Invalid trusted bundle file")
            path_parts = _bundle_path(item["path"])
            digest = item["sha256"]
            if path_parts in declared or not isinstance(digest, str) or not SHA.fullmatch(digest):
                raise CatalogConfigError("Invalid trusted bundle file identity")
            declared[path_parts] = digest
        relative_exe = executable.relative_to(root).parts
        if declared.get(relative_exe) != executable_sha:
            raise CatalogConfigError("Trusted executable is not declared")
        if _file_sha256(executable) != executable_sha:
            raise CatalogConfigError("Trusted executable hash changed")
        arguments = launch["allowed_arguments"]
        if not isinstance(arguments, list) or len(arguments) > 32 or not all(
                isinstance(item, str) and ARG.fullmatch(item) for item in arguments):
            raise CatalogConfigError("Unsafe trusted argument")
        return manifest, Installation(app_id, True, "Bundle registered; runtime not probed",
                                      executable, cwd, manifest_sha, executable_sha, tuple(arguments))

    def reload(self, *, verification_complete: bool = False, verification_failed: bool = False) -> None:
        manifests, installations, failures = {}, {}, {}
        for app_id in KNOWN_APPS:
            try:
                manifests[app_id], installations[app_id] = self._load_one(app_id)
            except (OSError, ValueError, TypeError, KeyError) as error:
                # Do not leak a path, uploaded value or validation exception to HTTP.
                failures[app_id] = type(error).__name__
        # One immutable pointer swap prevents readers from mixing manifest,
        # install and revision from different reload generations.
        with self._lock:
            pending = self._snapshot.verifying
            if verification_failed or not verification_complete:
                for app_id in pending:
                    if app_id in installations:
                        installations[app_id] = Installation(app_id, False,
                            "应用校验未能完成，请检查数据目录后重新启动" if verification_failed
                            else "正在校验应用文件")
            if verification_complete or verification_failed:
                pending = frozenset()
            revision = self._snapshot.revision + 1
            self._snapshot = CatalogSnapshot(MappingProxyType(manifests), MappingProxyType(installations),
                                             MappingProxyType(failures), revision, pending)
            self._observations.clear()  # A prior ready result never survives reinstallation.

    def list_page(self, query: PageQuery) -> AppManifestPage:
        snapshot = self._snapshot
        if query.cursor is not None and query.snapshot_revision is None:
            raise ValueError("Cursor requires a snapshot revision")
        if query.snapshot_revision is not None and query.snapshot_revision != snapshot.revision:
            raise SnapshotExpired("Catalog snapshot changed")
        keys = sorted(snapshot.manifests)
        start = 0
        if query.cursor is not None:
            if not query.cursor.startswith("after_") or query.cursor[6:] not in keys:
                raise SnapshotExpired("Catalog cursor is unavailable")
            start = keys.index(query.cursor[6:]) + 1
        selected = keys[start:start + query.limit]
        next_cursor = f"after_{selected[-1]}" if selected and start + len(selected) < len(keys) else None
        return AppManifestPage(schema_version="1.0", items=[snapshot.manifests[key] for key in selected],
                               next_cursor=next_cursor, snapshot_revision=snapshot.revision)

    def health(self, app_id: str) -> AppHealth | None:
        with self._lock:
            snapshot = self._snapshot
            manifest = snapshot.manifests.get(app_id)
            if manifest is None:
                return None
            verifying = app_id in snapshot.verifying
            if app_id in self._observations and not verifying:
                return self._observations[app_id]
            install = snapshot.installations[app_id]
        def status(state, reason):
            return Health(schema_version="1.0", state=state, reason=reason)
        if verifying:
            return AppHealth(schema_version="1.0", **{
                name: status("unknown", "正在校验应用文件")
                for name in ("service", "ui", "cpu", "gpu", "models")})
        if not install.installed:
            reason = install.reason
            return AppHealth(schema_version="1.0", service=status("unavailable", reason),
                             ui=status("unavailable", "UI bundle is not installed"),
                             cpu=status("unavailable", "CPU service is not installed"),
                             gpu=status("unavailable" if "gpu" in manifest.resources else "disabled",
                                        "GPU is not available" if "gpu" in manifest.resources else "GPU is not declared"),
                             models=status("unavailable", "Model files are not installed"))
        return AppHealth(schema_version="1.0", service=status("unknown", "Service has not been probed"),
                         ui=status("unknown", "UI has not been probed"),
                         cpu=status("unknown", "CPU has not been probed"),
                         gpu=status("unknown" if "gpu" in manifest.resources else "disabled",
                                    "GPU has not been probed" if "gpu" in manifest.resources else "GPU is not declared"),
                         models=status("unknown", "Models have not been probed"))

    def observe_health(self, app_id: str, observation: AppHealth, *, snapshot_revision: int) -> None:
        """Accept only a probe from this exact catalog generation and app identity."""
        with self._lock:
            if snapshot_revision != self._snapshot.revision:
                raise SnapshotExpired("Catalog snapshot changed during health probe")
            if app_id not in self._snapshot.manifests:
                raise KeyError("Application is not registered")
            self._observations[app_id] = AppHealth.model_validate(observation)
