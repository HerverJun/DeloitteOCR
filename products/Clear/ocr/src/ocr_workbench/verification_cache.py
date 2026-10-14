"""Local verification receipts, serialized per installation across processes."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time

from ocr_workbench.atomic_files import read_json, write_json


POLICY_VERSION = 1


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class VerificationCache:
    def __init__(self, root, *, kind="bundle", directory=None):
        self.root = Path(root).resolve()
        self.installation = os.path.normcase(str(self.root))
        self.manifest = self.root / (
            "manifest.json" if kind == "bundle" else "engine-package.json"
        )
        directory = Path(directory) if directory is not None else (
            Path(os.environ["LOCALAPPDATA"]) / "OfflineOCR/Verification"
        )
        key = hashlib.sha256((kind + ":" + self.installation).encode()).hexdigest()
        self.path = directory / (key + ".json")
        self.lock_path = directory / (key + ".lock")

    def identity(self):
        # Only the manifest is read on a cache hit, never the payload inventory.
        return hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    @contextmanager
    def locked(self, waiting=lambda: None):
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            announced = False
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (13, 11, 36):
                        raise
                    if not announced:
                        waiting()
                        announced = True
                    time.sleep(0.1)
            try:
                yield self
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def lookup(self, digest, level):
        try:
            record = read_json(self.path)
        except FileNotFoundError:
            return None, "first_start"
        except (OSError, ValueError):
            return None, "invalid_receipt"
        if not isinstance(record, dict):
            return None, "invalid_receipt"
        if record.get("installation") != self.installation:
            return None, "installation_changed"
        if record.get("manifest_sha256") != digest:
            return None, "version_changed"
        if record.get("policy_version") != POLICY_VERSION:
            return None, "policy_changed"
        if record.get("status") != "passed":
            return None, "unfinished_check"
        if record.get("level") not in ("full", "core"):
            return None, "invalid_receipt"
        try:
            datetime.fromisoformat(record["verified_at"])
        except (KeyError, TypeError, ValueError):
            return None, "invalid_receipt"
        if level == "full" and record["level"] != "full":
            return None, "recognition_enabled"
        return record, "cached"

    def invalidate(self, digest):
        write_json(self.path, self._record(digest, "checking"), durable=True)

    def _record(self, digest, status, **extra):
        return {
            "installation": self.installation,
            "manifest_sha256": digest,
            "policy_version": POLICY_VERSION,
            "status": status,
            **extra,
        }

    def save(self, digest, level):
        if self.identity() != digest:
            raise ValueError("校验期间文件清单发生变化，请等待更新完成后重试")
        record = self._record(digest, "passed", level=level, verified_at=timestamp())
        write_json(self.path, record, durable=True)
        return record
