"""Isolated, real-HTTP platform and native application acceptance fixture."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
RUN_DEFAULT = Path("E:/project/platform-dev-runs/dev-20260925-001")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_tree(source, destination):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))


SERVER = '''import os, sys
from pathlib import Path

root = Path(__file__).resolve().parent
(root / 'startup-status.txt').write_text('entered', encoding='utf-8')
sys.path.insert(0, str(root / 'src'))
app_id = os.environ['WORKBENCH_APP_ID']
data = root.parent.parent / 'native-data' / app_id
data.mkdir(parents=True, exist_ok=True)
local = root.parent.parent / 'native-local'
local.mkdir(parents=True, exist_ok=True)
os.environ['LOCALAPPDATA'] = str(local)
os.environ['USERPROFILE'] = str(local)
if app_id == 'classification':
    os.environ['ICP_DATA_DIR'] = str(data)
    from image_platform.cli import main
    sys.argv = ['image-platform', 'serve']
else:
    from ocr_workbench.service import main
    sys.argv = ['ocr-workbench', '--bundle', str(root), '--data', str(data),
                '--token-file', str(root / 'token.txt'), '--review-only']
(root / 'startup-status.txt').write_text('native-main', encoding='utf-8')
try:
    main()
except BaseException:
    import traceback
    (root / 'startup-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    raise
'''


def bundle(run, fixture, config, app_id):
    source = run / "workspaces" / app_id
    root = fixture / "bundles" / app_id
    root.mkdir(parents=True)
    environment = (Path("E:/project/image-classification-platform/.venv") if app_id == "classification"
                   else run / "workspaces/ocr-test-venv")
    executable = root / "launch.cmd"
    executable.write_bytes((f'@echo off\r\n"{environment / "Scripts/python.exe"}" -u "%~dp0server.py"\r\n').encode("ascii"))
    (root / "server.py").write_text(SERVER, encoding="utf-8")
    copy_tree(source / "src" / ("image_platform" if app_id == "classification" else "ocr_workbench"),
              root / "src" / ("image_platform" if app_id == "classification" else "ocr_workbench"))
    if app_id == "classification":
        copy_tree(source / "schemas", root / "schemas")
        copy_tree(source / "apps/web/dist", root / "apps/web/dist")
    else:
        copy_tree(source / "config", root / "config")
        copy_tree(source / "frontend/dist", root / "web")
        (root / "token.txt").write_text("acceptance-token-with-at-least-thirty-two-characters", encoding="utf-8")
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "bundle.manifest.json")
    declaration = {"app_id": app_id, "files": [
        {"path": path.relative_to(root).as_posix(), "sha256": digest(path)} for path in files]}
    (root / "bundle.manifest.json").write_text(json.dumps(declaration), encoding="utf-8")
    record = {"app_id": app_id, "installed": True,
              "manifest_sha256": digest(config / f"{app_id}.json"),
              "launch": {"bundle_root": str(root), "executable": str(executable),
                         "working_directory": str(root), "allowed_arguments": [],
                         "executable_sha256": digest(executable),
                         "bundle_manifest_sha256": digest(root / "bundle.manifest.json")}}
    (config / f"{app_id}.install.json").write_text(json.dumps(record), encoding="utf-8")
    return len(files)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, default=RUN_DEFAULT)
    args = parser.parse_args()
    run = args.run_root.resolve()
    fixture = run / "portal" / ("acceptance-" + uuid.uuid4().hex)
    config = fixture / "config" / "apps"
    config.mkdir(parents=True)
    from workbench_platform.catalog.registry import AppRegistry, DEFAULT_CONFIG
    from workbench_platform.catalog import routes

    for app_id in ("ocr", "classification"):
        shutil.copy2(DEFAULT_CONFIG / f"{app_id}.json", config / f"{app_id}.json")
    counts = {app_id: bundle(run, fixture, config, app_id) for app_id in ("ocr", "classification")}
    routes._registry = AppRegistry(config, install_root=fixture / "bundles")
    if set(routes._registry.installations) != {"ocr", "classification"}:
        raise RuntimeError(f"Trusted installation invalid: {routes._registry.failures}")
    (fixture / "platform-data").mkdir()
    os.environ["WORKBENCH_DATA_DIR"] = str(fixture / "platform-data")
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from workbench_platform.app import app
    import uvicorn

    dist = ROOT / "apps/portal/dist"
    if not (dist / "index.html").is_file():
        raise RuntimeError("Portal distribution has not been built")
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="portal-assets")

    @app.get("/__acceptance/stop/{app_id}")
    def stop_native(app_id: str):
        if app_id not in {"ocr", "classification"}:
            return {"stopped": False}
        service = app.state.launch_service
        try:
            receipt = service.current(app_id)
        except Exception:
            return {"stopped": False}
        app.state.supervisor.stop(receipt.launch_id)
        return {"stopped": True}

    @app.get("/__acceptance/diagnostics")
    def diagnostics():
        supervisor = app.state.supervisor
        return {key: {"done": value.future.done(),
                      "error": str(value.future.exception()) if value.future.done() and value.future.exception() else None}
                for key, value in supervisor._entries.items()}

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))

    @app.get("/__acceptance/shutdown")
    def shutdown():
        server.should_exit = True
        return {"stopping": True}

    @app.get("/__acceptance/download")
    def download():
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("T017,real-http-browser\n", headers={
            "Content-Disposition": 'attachment; filename="t017-readback.csv"'})

    @app.get("/{route:path}")
    def portal(route: str):
        if route and not (route == "apps" or route.startswith("apps/") or route in {
                "workspaces", "assets", "sources", "jobs", "analysis", "settings"}):
            from fastapi import HTTPException
            raise HTTPException(404)
        return FileResponse(dist / "index.html")

    print(json.dumps({"port": listener.getsockname()[1], "fixture": str(fixture),
                      "bundled_files": counts}), flush=True)
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
