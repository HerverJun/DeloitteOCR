"""Set up and run a model-free OCR workbench preview from a source checkout."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DEPENDENCIES = ("fastapi", "uvicorn", "PIL", "pillow_heif", "cv2", "numpy", "httpx", "openpyxl", "multipart", "psutil")


def storage_path(path):
    requested = Path(path).absolute()
    resolved = requested.resolve()
    if requested.drive.upper() != "F:" or resolved.drive.upper() != "F:":
        raise ValueError("Source preview files must remain on the local F drive")
    return resolved


def configure_storage(root):
    """Set child-process locations before imports, setup, or workspace creation."""
    root = storage_path(root)
    locations = {
        "TEMP": root / "temp", "TMP": root / "temp", "TMPDIR": root / "temp",
        "APPDATA": root / "cache/appdata", "LOCALAPPDATA": root / "cache/localappdata",
        "XDG_CACHE_HOME": root / "cache/xdg", "PIP_CACHE_DIR": root / "cache/pip",
        "npm_config_cache": root / "cache/npm", "HF_HOME": root / "cache/huggingface",
        "TORCH_HOME": root / "cache/torch", "PYTHONPYCACHEPREFIX": root / "cache/pycache",
        "MPLCONFIGDIR": root / "cache/matplotlib",
    }
    locations = {key: storage_path(path) for key, path in locations.items()}
    for path in set(locations.values()):
        path.mkdir(parents=True, exist_ok=True)
    os.environ.update({key: str(path) for key, path in locations.items()})
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(locations["TEMP"])
    return root


def prerequisites(*, need_frontend=True):
    errors = []
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
        errors.append("Windows x64 and Python 3.12 are required")
    missing = [name for name in DEPENDENCIES if importlib.util.find_spec(name) is None]
    if missing:
        errors.append("Missing Python packages: " + ", ".join(missing))
    if need_frontend and not (FRONTEND / "dist/index.html").is_file():
        errors.append("Frontend is not built; run: npm ci --prefix frontend, then npm run build --prefix frontend")
    if not (ROOT / "fixtures/printed.png").is_file():
        errors.append("Source fixtures are missing")
    return errors


def setup():
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
        raise RuntimeError("Run setup with Python 3.12 on Windows x64")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise RuntimeError("Node.js and npm are required to build the frontend")
    environment = storage_path(ROOT / ".venv")
    storage_path(FRONTEND)
    storage_path(FRONTEND / "node_modules")
    storage_path(FRONTEND / "dist")
    python = environment / "Scripts/python.exe"
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "-r", str(ROOT / "config/service-requirements.txt")], check=True)
    subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)
    subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)
    print("Setup complete. Run: .\\.venv\\Scripts\\python.exe scripts\\run_from_source.py smoke")


def app_for(data, token):
    sys.path.insert(0, str(ROOT / "src"))
    from ocr_workbench.service import create_app

    web = FRONTEND / "dist" if (FRONTEND / "dist/index.html").is_file() else None
    return create_app(ROOT, storage_path(data), token, start_queue=False,
                      review_only=True, agent_enabled=False, web_root=web)


def smoke(runtime_root):
    from fastapi.testclient import TestClient

    token = secrets.token_urlsafe(32)
    auth = {"Authorization": "Bearer " + token}
    with tempfile.TemporaryDirectory(prefix="ocr-source-smoke-", dir=runtime_root / "temp") as temporary:
        app = app_for(Path(temporary) / "data", token)
        with TestClient(app) as client:
            health = client.get("/api/health").json()
            if health["status"] != "ready" or not health["review_only"]:
                raise RuntimeError("Source preview did not enter model-free review mode")
            project_response = client.post("/api/projects", json={"name": "Source smoke"}, headers=auth)
            project_response.raise_for_status()
            project = project_response.json()
            with (ROOT / "fixtures/printed.png").open("rb") as image:
                imported = client.post(f"/api/projects/{project['id']}/images",
                                       files={"files": ("printed.png", image, "image/png")}, headers=auth)
            imported.raise_for_status()
            payload = imported.json()
            if payload["errors"] or len(payload["images"]) != 1:
                raise RuntimeError("Image import failed: " + json.dumps(payload, ensure_ascii=False))
            version_id = payload["images"][0]["active_version"]
            rotated = client.post(f"/api/versions/{version_id}/transform",
                                  json={"kind": "rotate", "degrees": 90}, headers=auth)
            rotated.raise_for_status()
            new_version = rotated.json()
            image_response = client.get(f"/api/versions/{new_version['id']}/image", headers=auth)
            image_response.raise_for_status()
            if not image_response.headers.get("content-type", "").startswith("image/png"):
                raise RuntimeError("Transformed image is not readable")
            snapshot = client.get(f"/api/projects/{project['id']}", headers=auth)
            snapshot.raise_for_status()
            if not snapshot.json().get("images"):
                raise RuntimeError("Imported image is missing from the project")
        print(json.dumps({"status": "passed", "mode": "model_free", "checks": [
            "health", "project", "image_import", "rotate", "image_readback", "project_readback"],
            "model_inference": "not_run"}))


def serve(data, port, runtime_root, browser_executable=None):
    import msvcrt
    import uvicorn

    data = storage_path(data)
    data.mkdir(parents=True, exist_ok=True)
    lock_path = data / "source-preview.lock"
    with lock_path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("This source preview data directory is already in use") from error
        token = secrets.token_urlsafe(32)
        app = app_for(data, token)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            address = f"http://127.0.0.1:{listener.getsockname()[1]}/#token={token}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
            app.state.shutdown = lambda: setattr(server, "should_exit", True)
            print("Source preview: " + address, flush=True)
            print("Data directory: " + str(data), flush=True)
            if browser_executable:
                profile = storage_path(runtime_root / "browser/profile")
                cache = storage_path(runtime_root / "browser/cache")
                downloads = storage_path(runtime_root / "browser/downloads")
                preferences = storage_path(profile / "Default/Preferences")
                for directory in (storage_path(profile / "Default"), cache, downloads):
                    directory.mkdir(parents=True, exist_ok=True)
                preferences.write_text(
                    json.dumps({"download": {"default_directory": str(downloads)}}), encoding="utf-8")

                def open_when_ready():
                    while not server.started and not server.should_exit:
                        time.sleep(0.05)
                    if server.started:
                        subprocess.Popen([
                            str(browser_executable), f"--user-data-dir={profile}",
                            f"--disk-cache-dir={cache}", "--no-first-run",
                            "--no-default-browser-check", "--disable-gpu",
                            "--disable-gpu-compositing", address,
                        ])

                threading.Thread(target=open_when_ready, daemon=True).start()
            server.run(sockets=[listener])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=ROOT / "build/source-preview",
                        help="F-drive location for caches, temporary files and application state")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Install service dependencies and build the frontend")
    commands.add_parser("check", help="Check source preview prerequisites")
    commands.add_parser("smoke", help="Exercise model-free project and image APIs")
    preview = commands.add_parser("serve", help="Start the React workbench without model inference")
    preview.add_argument("--data", type=Path, default=ROOT / "build/source-preview-data")
    preview.add_argument("--port", type=int, default=0)
    browser = preview.add_mutually_exclusive_group()
    browser.add_argument("--no-browser", action="store_true", help="Print the local URL (default)")
    browser.add_argument("--browser-executable", type=Path,
                         help="Open Chromium or Edge using an isolated F-drive profile")
    args = parser.parse_args()
    if args.command == "setup":
        configure_storage(args.runtime_root)
        setup()
        return
    errors = prerequisites(need_frontend=args.command != "smoke")
    if args.command == "check":
        print(json.dumps({"status": "ready" if not errors else "missing_prerequisites",
                          "python": sys.version.split()[0], "frontend_built": (FRONTEND / "dist/index.html").is_file(),
                          "errors": errors}, ensure_ascii=False, indent=2))
        raise SystemExit(bool(errors))
    if errors:
        raise RuntimeError("; ".join(errors) + "; run setup first")
    if args.command == "serve" and args.browser_executable and not args.browser_executable.is_file():
        raise ValueError("Browser executable does not exist")
    runtime_root = configure_storage(args.runtime_root)
    if args.command == "smoke":
        smoke(runtime_root)
    else:
        serve(args.data, args.port, runtime_root, args.browser_executable)


if __name__ == "__main__":
    main()
