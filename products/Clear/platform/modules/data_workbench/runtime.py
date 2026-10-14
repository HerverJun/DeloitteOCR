"""Start one DuckDB worker per job from a trusted, hash-verified source.

The caller supplies an authorized asset root and its recorded content hash.
This module does not expose SQL, database paths, or worker configuration to a
browser. Each job has a private database and staging directory; one runtime
instance admits only one writer at a time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FORMATS = {".csv", ".parquet", ".xlsx"}
_MAX_INPUT = 64 * 1024 * 1024


class DataRuntimeError(ValueError):
    """A fixed public failure; paths and worker diagnostics are withheld."""


class AnalysisCancelled(DataRuntimeError):
    pass


@dataclass(frozen=True)
class AnalysisResult:
    job_id: str
    source_sha256: str
    rows: int
    columns: tuple[str, ...]
    database_path: Path
    report_path: Path


def _relative(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise DataRuntimeError("Invalid source identity")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if posix.is_absolute() or windows.drive or any(part in {"", ".", ".."} for part in posix.parts):
        raise DataRuntimeError("Invalid source identity")
    return Path(*posix.parts)


def _trusted_file(root: Path, relative: str) -> Path:
    parts = _relative(relative).parts
    path = root.joinpath(*parts)
    for component in (root, *[root.joinpath(*parts[:i]) for i in range(1, len(parts) + 1)]):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise DataRuntimeError("Source is unavailable")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise DataRuntimeError("Source is unavailable") from None
    if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.suffix.lower() not in _FORMATS:
        raise DataRuntimeError("Source is unavailable")
    return resolved


class DataRuntime:
    def __init__(self, *, authorized_root: str | Path, jobs_root: str | Path,
                 python_executable: str | Path = sys.executable, max_seconds: float = 60):
        self.authorized_root = Path(authorized_root).resolve(strict=True)
        self.jobs_root = Path(jobs_root).resolve(strict=True)
        self.python_executable = Path(python_executable).resolve(strict=True)
        if (not self.authorized_root.is_dir() or not self.jobs_root.is_dir()
                or not self.python_executable.is_file() or not 1 <= max_seconds <= 600):
            raise DataRuntimeError("Runtime configuration is unavailable")
        self.max_seconds = max_seconds
        self._writer = threading.Lock()

    def analyze(self, *, job_id: str, source: str, source_sha256: str,
                cancel: threading.Event | None = None) -> AnalysisResult:
        if (not isinstance(job_id, str) or not _ID.fullmatch(job_id)
                or not isinstance(source_sha256, str) or not _SHA.fullmatch(source_sha256)):
            raise DataRuntimeError("Invalid job identity")
        if not self._writer.acquire(blocking=False):
            raise DataRuntimeError("Analysis worker is busy")
        try:
            return self._analyze(job_id, source, source_sha256, cancel)
        finally:
            self._writer.release()

    def _analyze(self, job_id: str, source: str, source_sha256: str,
                 cancel: threading.Event | None) -> AnalysisResult:
        incoming = _trusted_file(self.authorized_root, source)
        folder = self.jobs_root / job_id
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            raise DataRuntimeError("Job identity already exists") from None
        except OSError:
            raise DataRuntimeError("Analysis is unavailable") from None
        staged = folder / ("input" + incoming.suffix.lower())
        digest = hashlib.sha256()
        size = 0
        try:
            with incoming.open("rb") as source_stream, staged.open("xb") as target:
                while block := source_stream.read(1024 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise AnalysisCancelled("Analysis cancelled")
                    size += len(block)
                    if size > _MAX_INPUT:
                        raise DataRuntimeError("Source exceeds analysis limit")
                    digest.update(block)
                    target.write(block)
            if digest.hexdigest() != source_sha256:
                raise DataRuntimeError("Source content changed")
            request = {"job_id": job_id, "source": str(staged), "source_sha256": source_sha256,
                       "database": str(folder / "analysis.duckdb"), "temp": str(folder / "temp"),
                       "report": str(folder / "report.tmp")}
            child = subprocess.Popen([str(self.python_executable), "-m", "modules.data_workbench.worker"],
                                     cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            assert child.stdin is not None
            child.stdin.write(json.dumps(request, separators=(",", ":")).encode("utf-8"))
            child.stdin.close()
            started = time.monotonic()
            try:
                while child.poll() is None:
                    if cancel is not None and cancel.is_set():
                        raise AnalysisCancelled("Analysis cancelled")
                    if time.monotonic() - started > self.max_seconds:
                        raise DataRuntimeError("Analysis timed out")
                    time.sleep(0.05)
                report_path = folder / "report.json"
                temporary = folder / "report.tmp"
                if child.returncode != 0 or not temporary.is_file() or temporary.stat().st_size > 65536:
                    raise DataRuntimeError("Analysis worker failed")
                raw = temporary.read_bytes()
                report = json.loads(raw)
                if (report.get("job_id") != job_id or report.get("source_sha256") != source_sha256
                        or type(report.get("rows")) is not int or report["rows"] < 0
                        or not isinstance(report.get("columns"), list)
                        or any(not isinstance(name, str) for name in report["columns"])
                        or report.get("external_access") is not False
                        or report.get("extension_autoinstall") is not False
                        or report.get("extension_autoload") is not False):
                    raise DataRuntimeError("Analysis worker response is invalid")
                os.replace(temporary, report_path)
                return AnalysisResult(job_id, source_sha256, report["rows"], tuple(report["columns"]),
                                      folder / "analysis.duckdb", report_path)
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)
        except (OSError, ValueError) as error:
            if isinstance(error, DataRuntimeError):
                raise
            raise DataRuntimeError("Analysis is unavailable") from None
        finally:
            staged.unlink(missing_ok=True)
