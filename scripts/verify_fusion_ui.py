"""Exercise fusion UI against real SQLite/CPU fusion and synthetic raw OCR."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
from PIL import Image, ImageDraw
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.service import create_app
from ocr_workbench.imaging import add_image
from ocr_workbench.tables import parse_tables


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8879)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    token = "fusion-regression-local-fixture-token"
    app = create_app(ROOT, out/"data", token, start_queue=False, review_only=True)
    store = app.state.store
    project = store.project("融合浏览器回归")
    photos = []
    for label in ("A", "B"):
        path = out/(label+".png")
        image = Image.new("RGB", (900, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.text((50, 50), "REFERENCE: 00124 / 2026-09-12 / -12.50", fill="black")
        image.save(path)
        photo = add_image(store, project["id"], label+".png", path)
        store.enqueue(project["id"], [photo["active_version"]], ["glm", "paddlevl", "hunyuan"])
        while task := store.claim():
            wrong = task["engine"] == "glm"
            text = "<table><tr><td>Item</td><td>Code</td><td>Amount</td></tr><tr><td>Alpha</td><td>" + ("00123" if wrong else "00124") + "</td><td>-12.50</td></tr><tr><td>Beta</td><td>00987</td><td>" + ("12.50" if wrong else "-12.50") + "</td></tr></table>"
            store.complete(task["id"], {"status": "success", "engine": task["engine"], "text": text,
                           "tables": parse_tables(text), "blocks": [{"kind": "table", "text": text, "confidence": None,
                              "polygon": [[40,40],[850,40],[850,500],[40,500]]}],
                           "project_image_version": photo["active_version"], "image": {"width": 900, "height": 600},
                           "elapsed_seconds": .1, "load_seconds": 0})
        photos.append(photo)
    app.state.fusion_queue.start()
    seed = {"project": project["id"], "photos": photos, "token": token, "base": f"http://127.0.0.1:{args.port}",
            "evidence_scope": "Synthetic original OCR, actual CPU fusion and durable API; no OCR accuracy or human-time claim"}
    (out/"seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2), "utf-8")
    app.mount("/", StaticFiles(directory=ROOT/"frontend/dist", html=True))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic()+15
        while not server.started:
            if time.monotonic() > deadline: raise RuntimeError("Server did not start")
            time.sleep(.05)
        return subprocess.run(["node", str(ROOT/"scripts/verify_fusion_ui.mjs"), str(out)], cwd=ROOT).returncode
    finally:
        server.should_exit = True
        worker.join(timeout=15)
        app.state.fusion_queue.stop()


if __name__ == "__main__":
    raise SystemExit(main())
