"""One-use local platform navigation context, never an identity for writes."""

from dataclasses import dataclass
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
ROUTE = re.compile(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
TICKET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def local_origin(value):
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
    workspace_id: str
    link_id: str
    module_project_id: str
    asset_id: str | None
    return_url: str

    @classmethod
    def from_trusted_launch(cls, value, *, instance_id, store):
        required = {"app_id", "instance_id", "workspace_id", "link_id", "module_project_id",
                    "asset_id", "return_route", "platform_origin"}
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("Invalid navigation context")
        for key in ("app_id", "instance_id", "workspace_id", "link_id", "module_project_id"):
            if not isinstance(value[key], str) or not ID.fullmatch(value[key]):
                raise ValueError("Invalid navigation identity")
        asset_id = value["asset_id"]
        if asset_id is not None and (not isinstance(asset_id, str) or not ID.fullmatch(asset_id)):
            raise ValueError("Invalid asset identity")
        route = value["return_route"]
        if (value["app_id"] != "classification" or value["instance_id"] != instance_id
                or not isinstance(route, str) or len(route) > 257 or not ROUTE.fullmatch(route)):
            raise ValueError("Invalid navigation route")
        # Native project and asset membership come from SQLite, not the
        # platform workspace ID or a client's claimed project name.
        try:
            store.project(value["module_project_id"])
        except LookupError:
            raise ValueError("Native project is unavailable") from None
        if asset_id is not None and store.one(
                "SELECT id FROM assets WHERE id=? AND project_id=? AND archived=0",
                (asset_id, value["module_project_id"])) is None:
            raise ValueError("Asset does not belong to native project")
        target = local_origin(value["platform_origin"]) + route + "?" + urlencode({"workspace": value["workspace_id"]})
        return cls("classification", instance_id, value["workspace_id"], value["link_id"],
                   value["module_project_id"], asset_id, target)


class NavigationTickets:
    def __init__(self, *, instance_id, store, ttl_seconds=60):
        if not isinstance(instance_id, str) or not ID.fullmatch(instance_id) or not 1 <= ttl_seconds <= 300:
            raise ValueError("Invalid launch configuration")
        self.instance_id, self.store, self.ttl_seconds = instance_id, store, ttl_seconds
        self._lock = threading.RLock()
        self._tickets = {}

    def issue(self, value):
        navigation = Navigation.from_trusted_launch(value, instance_id=self.instance_id, store=self.store)
        with self._lock:
            now = time.monotonic()
            self._tickets = {key: item for key, item in self._tickets.items() if item[0] > now}
            if len(self._tickets) >= 256:
                raise ValueError("Navigation capacity reached")
            ticket = secrets.token_urlsafe(32)
            self._tickets[ticket] = (now + self.ttl_seconds, navigation)
        return ticket

    def consume(self, ticket):
        if not TICKET.fullmatch(ticket):
            raise KeyError("Navigation ticket unavailable")
        with self._lock:
            item = self._tickets.pop(ticket, None)
        if item is None or item[0] <= time.monotonic():
            raise KeyError("Navigation ticket unavailable")
        # A native project/asset can be deleted between issuance and opening.
        navigation = item[1]
        self.store.project(navigation.module_project_id)
        if navigation.asset_id is not None and self.store.one(
                "SELECT id FROM assets WHERE id=? AND project_id=? AND archived=0",
                (navigation.asset_id, navigation.module_project_id)) is None:
            raise KeyError("Navigation ticket unavailable")
        return navigation
