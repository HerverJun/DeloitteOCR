"""Own the packaged launcher and external Edge audit; close both on completion."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import json
import socket
import secrets
import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_application import Application, until

p = argparse.ArgumentParser()
p.add_argument("--build-root", type=Path, required=True)
p.add_argument("--features-only", action="store_true")
p.add_argument("--output", type=Path, help="New evidence directory; leaves earlier audit evidence intact")
p.add_argument("--data", type=Path, help="Isolated project directory shared by core and feature audits")
p.add_argument("--diagnostic-service-only", action="store_true", help="Only debug dialog behavior; cannot produce full acceptance evidence")
a = p.parse_args()
root = a.build_root.resolve()
audit_output = a.output.resolve() if a.output else root / ("ui-feature-audit" if a.features_only else "ui-audit")
if a.output and audit_output.exists() and any(audit_output.iterdir()):
    raise ValueError("Fresh UI evidence directory required")
app = Application(root / "bundle", root / "ui-audit-launcher")
app.data = a.data.resolve() if a.data else root / "ui-project"
diagnostic_token = None
try:
    diagnostic = {}
    if a.diagnostic_service_only:
        if not a.features_only or not os.environ.get("OCR_DIALOG_ONLY"):
            raise ValueError("Direct-service mode is restricted to dialog diagnostics")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token_file = app.data / "launcher/session-token.txt"
        app.token = secrets.token_hex(24)
        with token_file.open("x", encoding="utf-8") as stream:
            stream.write(app.token)
        diagnostic_token = token_file
        app.base = f"http://127.0.0.1:{port}"
        app.process = subprocess.Popen([str(root / "bundle/runtimes/service/python.exe"), "-B", "-X", "utf8", "-I", "-m", "ocr_workbench.service", "--bundle", str(root / "bundle"), "--data", str(app.data), "--port", str(port), "--token-file", str(token_file)], creationflags=subprocess.CREATE_NO_WINDOW)
        app.state = {"service_pid": app.process.pid}
        def ready():
            try:
                return app.api("/health")["status"] == "ready"
            except OSError:
                return False
        until(ready, 30)
        state_file = root / "dialog-diagnostic-state.json"
        state_file.write_text(json.dumps({"data": str(app.data), "port": port}), "utf-8")
        diagnostic["OCR_STATE_FILE"] = str(state_file)
    else:
        app.start()
    result = subprocess.run(
        [
            "node",
            str(
                Path(__file__).resolve().parents[1]
                / (
                    "frontend/scripts/ui-features.mjs"
                    if a.features_only
                    else "frontend/scripts/ui-audit.mjs"
                )
            ),
        ],
        env={**os.environ, "OCR_BUILD_ROOT": str(root), "OCR_UI_OUTPUT": str(audit_output), "OCR_STATE_FILE": str(app.data / "launcher/launcher-state.json"), **diagnostic},
    )
    if result.returncode == 0 and not a.diagnostic_service_only:
        receipt = audit_output / "result.json"
        report = json.loads(receipt.read_text("utf-8"))
        report["bundle_manifest_sha256"] = hashlib.sha256((root / "bundle/manifest.json").read_bytes()).hexdigest()
        report["health"] = app.health
        receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    raise SystemExit(result.returncode)
finally:
    try:
        app.stop()
    finally:
        if diagnostic_token and (not app.process or app.process.poll() is not None):
            diagnostic_token.unlink(missing_ok=True)
