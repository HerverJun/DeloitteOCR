"""One-use, workspace-bound context for trusted local application adapters.

The platform mounts the router separately. Native credentials and destinations
are supplied only by server-registered adapters, never by an HTTP request.
"""

from __future__ import annotations

import http.client
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from packages.contracts.models import ContextIssueRequest, OpenContext
from workbench_platform.store import WorkspaceStore
from workbench_platform.supervisor.processes import (
    LaunchFailure, Supervisor, _identity, _listener_pid, _loopback_port, _process_active,
)


class BridgeDenied(ValueError):
    """A fixed, public failure; never include a supplied payload or secret."""

    def __init__(self, code: str = "VALIDATION_FAILED"):
        super().__init__("Context is unavailable")
        self.code = code


@dataclass(frozen=True)
class ResolvedContext:
    workspace_id: str
    app_id: str
    instance_id: str
    link_id: str
    module_project_id: str
    role: str
    return_route: str
    base_url: str


@dataclass(frozen=True)
class NativeRoute:
    method: str
    path: str
    allowed_roles: frozenset[str] | None = None

    def __post_init__(self):
        if self.method not in {"GET", "POST"} or not self.path.startswith("/") or any(
            character in self.path for character in ("?", "#", "\\", "\r", "\n")
        ) or ".." in self.path.split("/"):
            raise ValueError("Native adapter route must be a fixed relative path")
        roles = self.allowed_roles
        if roles is None:
            roles = frozenset({"owner", "editor", "viewer"} if self.method == "GET" else {"owner", "editor"})
        if not isinstance(roles, frozenset) or not roles or not roles <= {"owner", "editor", "viewer"}:
            raise ValueError("Native adapter roles must be a trusted fixed set")
        object.__setattr__(self, "allowed_roles", roles)


@dataclass(frozen=True)
class NativeAdapter:
    routes: Mapping[str, NativeRoute]
    # A trusted server callback may retrieve native session credentials. Its
    # return value is never included in context, URL, response, or exception.
    headers: Callable[[ResolvedContext], Mapping[str, str]]


@dataclass(frozen=True)
class NativeResponse:
    status: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class _Issued:
    context: OpenContext
    workspace_id: str
    module_project_id: str
    role: str
    link_revision: int


class ContextBridge:
    def __init__(self, store: WorkspaceStore, supervisor: Supervisor, *,
                 adapters: Mapping[str, NativeAdapter] | None = None,
                 ttl_seconds: int = 60, clock: Callable[[], datetime] | None = None):
        if not 1 <= ttl_seconds <= 300:
            raise ValueError("Context lifetime must be bounded")
        self.store, self.supervisor = store, supervisor
        self.adapters = dict(adapters or {})
        self.ttl_seconds = ttl_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._issued: dict[str, _Issued] = {}

    def _live(self, app_id: str, instance_id: str):
        """A cached launch receipt alone does not prove a live listener."""
        supervisor = self.supervisor
        with supervisor._lock:
            snapshot = supervisor.registry._snapshot
            if supervisor._closed or app_id not in snapshot.manifests or supervisor._current.get(app_id) != (snapshot.revision, instance_id):
                raise BridgeDenied("APP_UNAVAILABLE")
            for entry in supervisor._entries.values():
                if not entry.future.done() or entry.future.exception() is not None:
                    continue
                receipt = entry.future.result()
                if (receipt.app_id, receipt.instance_id) != (app_id, instance_id) or entry.identity[2] != snapshot.revision:
                    continue
                try:
                    port = _loopback_port(receipt.base_url)
                    if (not _process_active(receipt.pid, receipt.creation_time)
                            or _listener_pid(port) != receipt.pid
                            or (receipt.ownership == "owned" and (entry.job is None or not entry.job.contains(receipt.pid)))):
                        raise BridgeDenied("APP_UNAVAILABLE")
                    if _identity(port, app_id, instance_id, receipt.handshake_nonce) != receipt.version:
                        raise BridgeDenied("APP_UNAVAILABLE")
                except (LaunchFailure, OSError, ValueError) as error:
                    raise BridgeDenied("APP_UNAVAILABLE") from error
                return receipt
        raise BridgeDenied("APP_UNAVAILABLE")

    def _link(self, workspace_id: str, app_id: str, link_id: str):
        try:
            if self.store.get_workspace(workspace_id) is None:
                raise BridgeDenied()
            matching = [link for link in self.store.list_links(workspace_id)
                        if link.link_id == link_id and link.app_id == app_id]
        except (KeyError, ValueError) as error:
            raise BridgeDenied() from error
        if len(matching) != 1:
            raise BridgeDenied()
        return matching[0]

    def issue(self, request: ContextIssueRequest, *, workspace_id: str) -> OpenContext:
        if request.asset_set_id is not None or request.artifact_id is not None:
            # T030+ stores will provide authoritative ownership; client IDs
            # are not an authorization source.
            raise BridgeDenied()
        link = self._link(workspace_id, request.app_id, request.link_id)
        self._live(request.app_id, request.instance_id)
        now = self.clock()
        if now.tzinfo is None:
            raise RuntimeError("Bridge clock must be timezone-aware")
        expiry = now.astimezone(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        with self._lock:
            self._issued = {key: value for key, value in self._issued.items()
                            if datetime.fromisoformat(value.context.expires_at.replace("Z", "+00:00")) > now}
            if len(self._issued) >= 4096:
                raise BridgeDenied("APP_UNAVAILABLE")
            nonce = "n" + uuid.uuid4().hex
            context = OpenContext(schema_version="1.0", app_id=request.app_id,
                                  instance_id=request.instance_id, link_id=request.link_id,
                                  asset_set_id=None, artifact_id=None, nonce=nonce,
                                  expires_at=expiry.isoformat().replace("+00:00", "Z"),
                                  return_route=request.return_route)
            self._issued[nonce] = _Issued(context.model_copy(deep=True), workspace_id, link.module_project_id,
                                          link.role, link.revision)
        return context

    def consume(self, context: OpenContext, *, workspace_id: str) -> ResolvedContext:
        # Hold the ledger lock through the live/link recheck and removal: two
        # concurrent consumers cannot both use the same context.
        with self._lock:
            issued = self._issued.get(context.nonce)
            if issued is None or issued.context != context or issued.workspace_id != workspace_id:
                raise BridgeDenied()
            now = self.clock()
            if datetime.fromisoformat(context.expires_at.replace("Z", "+00:00")) <= now:
                self._issued.pop(context.nonce, None)
                raise BridgeDenied("CONTEXT_EXPIRED")
            link = self._link(workspace_id, context.app_id, context.link_id)
            if (link.module_project_id, link.role, link.revision) != (
                    issued.module_project_id, issued.role, issued.link_revision):
                self._issued.pop(context.nonce, None)
                raise BridgeDenied()
            receipt = self._live(context.app_id, context.instance_id)
            self._issued.pop(context.nonce)
            return ResolvedContext(workspace_id, context.app_id, context.instance_id,
                                   context.link_id, link.module_project_id, link.role,
                                   context.return_route, receipt.base_url)

    def forward(self, context: OpenContext, *, workspace_id: str, route_name: str,
                body: bytes = b"") -> NativeResponse:
        """Use a server-registered route and credentials over direct loopback.

        A response is bounded and redirects are returned as data, never followed.
        This is an adapter primitive, not a user-supplied proxy endpoint. An
        adapter must construct/validate project and asset identities in body
        from ResolvedContext before calling this method.
        """
        adapter = self.adapters.get(context.app_id)
        if adapter is None or route_name not in adapter.routes or type(body) is not bytes or len(body) > 1_048_576:
            raise BridgeDenied()
        route = adapter.routes[route_name]
        if route.method == "GET" and body:
            raise BridgeDenied()
        resolved = self.consume(context, workspace_id=workspace_id)
        if resolved.role not in route.allowed_roles:
            raise BridgeDenied()
        try:
            headers = dict(adapter.headers(resolved))
        except Exception:
            # Callback exceptions can contain native credentials. Never expose
            # their text or chained traceback through this library boundary.
            raise BridgeDenied("APP_UNAVAILABLE") from None
        if not all(key in {"Authorization", "Content-Type", "Origin"} and isinstance(value, str)
                   and "\r" not in value and "\n" not in value for key, value in headers.items()):
            raise BridgeDenied()
        # A native service could leave and another process could bind its old
        # port after the context was consumed. Recheck immediately before use.
        receipt = self._live(resolved.app_id, resolved.instance_id)
        if receipt.base_url != resolved.base_url:
            raise BridgeDenied("APP_UNAVAILABLE")
        port = _loopback_port(resolved.base_url)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(route.method, route.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(1_048_577)
            if len(payload) > 1_048_576:
                raise BridgeDenied("APP_UNAVAILABLE")
            return NativeResponse(response.status, response.getheader("Content-Type", ""), payload)
        except (OSError, TimeoutError, http.client.HTTPException, UnicodeError, ValueError):
            # Native response/status text and invalid credential encodings can
            # contain secrets. Suppress the original exception chain entirely.
            raise BridgeDenied("APP_UNAVAILABLE") from None
        finally:
            connection.close()
