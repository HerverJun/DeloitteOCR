"""Short-lived, one-use navigation handoff from a trusted local platform launch."""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
ROUTE = re.compile(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
TICKET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def platform_origin(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid platform origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid platform origin") from None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
            or not 1 <= port <= 65535 or parsed.netloc != f"127.0.0.1:{port}"
            or parsed.path or parsed.query or parsed.fragment or parsed.username):
        raise ValueError("Invalid platform origin")
    return value


@dataclass(frozen=True)
class Navigation:
    app_id: str
    instance_id: str
    link_id: str
    workspace_id: str
    return_url: str

    @classmethod
    def from_trusted_launch(cls, value: dict, *, instance_id: str) -> Navigation:
        if not isinstance(value, dict) or set(value) != {
                "app_id", "instance_id", "link_id", "workspace_id", "return_route", "platform_origin"}:
            raise ValueError("Invalid navigation context")
        for key in ("app_id", "instance_id", "link_id", "workspace_id"):
            if not isinstance(value[key], str) or not ID.fullmatch(value[key]):
                raise ValueError("Invalid navigation identity")
        route = value["return_route"]
        if (value["app_id"] != "ocr" or value["instance_id"] != instance_id
                or not isinstance(route, str) or len(route) > 257 or not ROUTE.fullmatch(route)):
            raise ValueError("Invalid navigation route")
        # Workspace selection is a distinct, trusted identity, never part of
        # OpenContext.return_route. No user-supplied query or URL is forwarded.
        target = platform_origin(value["platform_origin"]) + route + "?" + urlencode({"workspace": value["workspace_id"]})
        return cls(value["app_id"], instance_id, value["link_id"], value["workspace_id"], target)


class NavigationTickets:
    def __init__(self, *, instance_id: str, ttl_seconds: int = 60):
        if not ID.fullmatch(instance_id) or not 1 <= ttl_seconds <= 300:
            raise ValueError("Invalid launch configuration")
        self.instance_id = instance_id
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._tickets: dict[str, tuple[float, Navigation]] = {}

    def issue(self, value: dict) -> str:
        navigation = Navigation.from_trusted_launch(value, instance_id=self.instance_id)
        with self._lock:
            now = time.monotonic()
            self._tickets = {key: item for key, item in self._tickets.items() if item[0] > now}
            if len(self._tickets) >= 256:
                raise ValueError("Navigation capacity reached")
            ticket = secrets.token_urlsafe(32)
            self._tickets[ticket] = (now + self.ttl_seconds, navigation)
        return ticket

    def consume(self, ticket: str) -> Navigation:
        if not TICKET.fullmatch(ticket):
            raise KeyError("Navigation ticket unavailable")
        with self._lock:
            item = self._tickets.pop(ticket, None)
        if item is None or item[0] <= time.monotonic():
            raise KeyError("Navigation ticket unavailable")
        return item[1]
