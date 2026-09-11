"""Atomic JSON exchange with bounded retries for Windows sharing violations."""

import json
import os
from pathlib import Path
import time
import threading
import uuid


# Fixed stripes bound memory even when a long-running queue exchanges millions
# of result files. Reads and replacements of the same path must not overlap on
# Windows: delete sharing alone does not guarantee replacement will succeed.
_locks = tuple(threading.RLock() for _ in range(64))


def _path_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    return _locks[hash(key) % len(_locks)]


def sharing_retry(action):
    for attempt in range(40):
        try:
            return action()
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.025)


def read_json(path):
    with _path_lock(path):
        return sharing_retry(lambda: json.loads(_read_text(Path(path))))


def _read_text(path):
    return path.read_text("utf-8")


def write_json(path, value, *, durable=False):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        with _path_lock(path):
            sharing_retry(lambda: temporary.replace(path))
    finally:
        temporary.unlink(missing_ok=True)
