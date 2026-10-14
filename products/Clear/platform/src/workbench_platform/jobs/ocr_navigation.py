"""Issue a native OCR navigation ticket for a verified project link."""

from __future__ import annotations

import http.client
import json
import re
from urllib.parse import urlencode, urlsplit

from workbench_platform.bridge.context import ResolvedContext

ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
TICKET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
ROUTE = re.compile(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")


class NavigationUnavailable(ValueError):
    def __init__(self):
        super().__init__("OCR navigation is unavailable")


def _port(origin: str) -> int:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
                or not 1 <= port <= 65535 or parsed.netloc != f"127.0.0.1:{port}"
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username):
            raise ValueError()
        return port
    except (ValueError, TypeError):
        raise NavigationUnavailable() from None


def issue_navigation(context: ResolvedContext, *, platform_origin: str, launch_nonce: str,
                     timeout: float = 3.0) -> str:
    if (not isinstance(context, ResolvedContext) or context.app_id != "ocr"
            or not all(ID.fullmatch(value) for value in (
                context.instance_id, context.link_id, context.workspace_id))
            or not NATIVE_ID.fullmatch(context.module_project_id)
            or not isinstance(context.return_route, str) or not ROUTE.fullmatch(context.return_route)
            or len(context.return_route) > 257 or not isinstance(launch_nonce, str)
            or not ID.fullmatch(launch_nonce) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10):
        raise NavigationUnavailable()
    native_port = _port(context.base_url)
    platform_port = _port(platform_origin)
    body = json.dumps({"app_id": "ocr", "instance_id": context.instance_id,
                       "link_id": context.link_id, "workspace_id": context.workspace_id,
                       "project_id": context.module_project_id,
                       "return_route": context.return_route,
                       "platform_origin": f"http://127.0.0.1:{platform_port}"},
                      separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", native_port, timeout=timeout)
    try:
        connection.request("POST", "/api/platform/navigation/issue", body=body,
                           headers={"Content-Type": "application/json", "X-Workbench-Launch-Nonce": launch_nonce})
        response = connection.getresponse()
        if response.status != 201 or int(response.getheader("Content-Length", "0")) > 512:
            raise NavigationUnavailable()
        raw = response.read(513)
        if len(raw) > 512:
            raise NavigationUnavailable()
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"ticket"}
                or not isinstance(value["ticket"], str) or not TICKET.fullmatch(value["ticket"])):
            raise NavigationUnavailable()
        return f"http://127.0.0.1:{native_port}/#{urlencode({'platform_ticket': value['ticket']})}"
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, http.client.HTTPException):
        raise NavigationUnavailable() from None
    finally:
        connection.close()
