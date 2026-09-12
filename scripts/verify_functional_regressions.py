"""Run browser regressions against current source, synthetic OCR and isolated SQLite.

Build frontend first. Use the service Python runtime, then pass a fresh --output
directory. No model workers or existing user projects are opened.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.imaging import add_image
from ocr_workbench.service import create_app
from ocr_workbench.tables import parse_tables


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    token = "functional-regression-isolated-local-token"
    app = create_app(ROOT, out / "data", token, start_queue=False)
    # Synthetic idle state keeps recognition UI testable without starting models.
    app.state.queue.status = lambda: {"healthy": True, "alive": True, "state": "running", "task_id": None, "engine": None, "loaded": False}
    store = app.state.store
    first, second = store.project("回归项目一"), store.project("回归项目二")
    seeds = {}
    for label, project in [("A", first), ("B", first), ("C", second), ("TEXT", first), ("MIXED", first)]:
        source = out / (label + ".png")
        Image.new("RGB", (400, 300), "white").save(source)
        photo = add_image(store, project["id"], source.name, source)
        text = f"<table><tr><td>{label}11</td><td>{label}12</td></tr><tr><td>{label}21</td><td>{label}22</td></tr></table>"
        if label == "TEXT":
            text = "NO_TABLE_IMAGE_CONTENT"
        elif label == "MIXED":
            text = "BEFORE\n| MD_HEADER |\n| --- |\n| MD_VALUE |\nBETWEEN\n<table><tr><td>HTML_VALUE</td></tr></table>\nAFTER"
        original = {"status": "success", "engine": "ppocr", "text": text,
                    "tables": parse_tables(text), "blocks": [], "elapsed_seconds": 0.1,
                    "load_seconds": 0.1, "project_image_version": photo["active_version"],
                    "image": {"width": 400, "height": 300}}
        task = store.enqueue(project["id"], [photo["active_version"]], ["ppocr"])[0]
        store.claim()
        store.complete(task, original)
        result = store.result(store.one("tasks", task)["result_id"])
        edit = deepcopy(result["edited"])
        edit["text"] = label + " edited\n" + text
        store.save(result["id"], edit, result["revision"])
        seeds[label] = {"photo": photo, "result": result["id"]}
    seed = {"project": first["id"], "second_project": second["id"], "seeds": seeds,
            "token": token, "base": f"http://127.0.0.1:{args.port}"}
    Image.new("RGB", (400, 300), "white").save(out / "import.png")
    (out / "seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2), "utf-8")
    app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not worker.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Regression server failed to start")
            time.sleep(0.05)
        result = subprocess.run(["node", str(ROOT / "scripts/verify_functional_regressions.mjs"), str(out)], cwd=ROOT)
        return result.returncode
    finally:
        server.should_exit = True
        worker.join(timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
