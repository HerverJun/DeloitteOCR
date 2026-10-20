"""Stable receipt encoding and execution fences shared by every entry point."""
from datetime import datetime, timezone
import hashlib
import json


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


class Conflict(ValueError):
    pass


class RunSuperseded(Conflict):
    """The executing generation lost authority; never continue its tool batch."""
