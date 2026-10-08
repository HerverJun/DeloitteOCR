"""F-drive-only, immutable evaluation artifacts; never imports product services."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORMAT = "specialized-agent-eval-v1"


def fpath(value):
    path = Path(value).resolve()
    if path.drive.upper() != "F:":
        raise ValueError(f"Project writes/read bindings must remain on F: {path}")
    return path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


def frozen_bytes(path, payload):
    path = fpath(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Frozen bytes differ; use a new output: {path}")
    else:
        path.write_bytes(payload)


def frozen_json(path, value):
    frozen_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2,
                                 allow_nan=False) + "\n").encode("utf-8"))


def indexed(rows, key, description):
    answer = {}
    for row in rows:
        if row.get(key) is None or row[key] in answer:
            raise ValueError(f"Missing/duplicate {description}: {row.get(key)}")
        answer[row[key]] = row
    return answer
