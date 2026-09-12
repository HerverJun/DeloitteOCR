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
    parser.add_argument("--layout-only", action="store_true")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    token = "functional-regression-isolated-local-token"
    app = create_app(ROOT, out / "data", token, start_queue=False)
    # No model worker: report a synthetic idle queue for normal UI scenarios.
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
    # Add a second result for A and an unrecognized document, all synthetic.
    original_a = store.result(seeds["A"]["result"])["original"]
    alternate = deepcopy(original_a)
    alternate["text"] = "ALTERNATE_UNADOPTED"
    alt_task = store.enqueue(first["id"], [seeds["A"]["photo"]["active_version"]], ["glm"])[0]
    store.claim()
    store.complete(alt_task, alternate)
    seeds["A"]["alternate"] = store.one("tasks", alt_task)["result_id"]
    with store.transaction() as db:
        db.execute("INSERT OR REPLACE INTO selections VALUES(?,?)", (seeds["A"]["photo"]["id"], seeds["A"]["result"]))
    source = out / "UNRECOGNIZED.png"
    Image.new("RGB", (400, 300), "white").save(source)
    seeds["EMPTY"] = {"photo": add_image(store, first["id"], source.name, source)}
    empty_project = store.project("首次使用空项目")
    large = deepcopy(store.result(seeds["B"]["result"])["edited"])
    large["tables"] = [{"rows":33,"columns":8,"cells":[{"row":r,"column":c,"row_span":1,"column_span":1,"text":f"{r:03d}-{c:02d}"} for r in range(33) for c in range(8)]}]
    result_b = store.result(seeds["B"]["result"])
    store.save(result_b["id"], large, result_b["revision"])
    seed = {"empty_project": empty_project["id"], "project": first["id"], "second_project": second["id"], "seeds": seeds,
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
        result = subprocess.run(["node", str(ROOT / "scripts/verify_audit_fixes.mjs"), str(out)] + (["--layout-only"] if args.layout_only else []), cwd=ROOT)
        return result.returncode
    finally:
        server.should_exit = True
        worker.join(timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
