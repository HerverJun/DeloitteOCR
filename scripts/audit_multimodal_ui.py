"""Isolated UI acceptance with real review persistence and explicitly synthetic inference."""
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
from PIL import Image, ImageDraw, ImageFont
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.imaging import add_image
from ocr_workbench.multimodal_store import complete_review, prepare_review
import ocr_workbench.multimodal_runtime as runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    state = {"available": True, "hold": False, "errors": []}
    token = "isolated-multimodal-ui-fixture"

    def fixture_config(bundle, profile_id=None):
        profile_id = profile_id or "audit-vision"
        return {"profile_id": profile_id, "model": {"id": profile_id},
                "config_sha256": "synthetic-ui-only-not-model-evidence", "prompt_version": "fixture"}

    def fixture_readiness(bundle, config):
        missing = config["profile_id"] == "audit-missing"
        return {"ready": state["available"] and not missing, "profiles": ["audit-vision", "audit-missing"],
                "label": "不可用模型（验收夹具）" if missing else "视觉审校模型（验收夹具）",
                "reason": "验收夹具：模型资源尚未就绪" if missing or not state["available"] else "",
                "identity": {"synthetic": True}}

    runtime.load_config = fixture_config
    runtime.review_readiness = fixture_readiness
    from ocr_workbench.service import create_app
    app = create_app(ROOT, out / "workspace", token, start_queue=False)
    store = app.state.store
    project = store.project("视觉审校交互验收")
    image = Image.new("RGB", (900, 700), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 30)
    for index, text in enumerate(["Invoice 00001", "Amount 001.05", "Name Revenue"]):
        draw.text((45, 35 + index * 65), text, font=font, fill="#203c2b")
    values = [["Item", "Amount"], ["Revenue", "001.05"]]
    cells = []
    for row, row_values in enumerate(values):
        for column, value in enumerate(row_values):
            x, y = 45 + column * 380, 270 + row * 100
            polygon = [[x, y], [x + 380, y], [x + 380, y + 100], [x, y + 100]]
            draw.rectangle((x, y, x + 380, y + 100), outline="#365e42", width=2)
            draw.text((x + 18, y + 30), value, font=font, fill="#203c2b")
            cells.append({"row": row, "column": column, "row_span": 1, "column_span": 1,
                          "text": "001.00" if value == "001.05" else value, "polygon": polygon})
    path = out / "synthetic-invoice.png"
    image.save(path)
    photo = add_image(store, project["id"], "视觉审校验收.png", path)
    version = store.one("versions", photo["active_version"])
    task_id = store.enqueue(project["id"], [version["id"]], ["ppocr"])[0]
    assert store.claim()["id"] == task_id
    text = "Invoice 00001\nAmount 001.00\nName Revenve"
    raw = {"engine": "ppocr", "text": text,
           "tables": [{"rows": 2, "columns": 2, "cells": cells}],
           "blocks": [{"kind": "text", "text": value, "confidence": .8,
                       "polygon": [[40, 30+i*65], [750, 30+i*65], [750, 80+i*65], [40, 80+i*65]]}
                      for i, value in enumerate(text.splitlines())],
           "image": {"width": 900, "height": 700}, "project_image_version": version["id"]}
    store.complete(task_id, raw)
    result_id = store.one("tasks", task_id)["result_id"]

    @app.post("/api/audit/reviewer")
    def configure_fixture(body: dict):
        for key in ("available", "hold"):
            if key in body:
                state[key] = bool(body[key])
        return deepcopy(state)

    stop = threading.Event()

    def synthesize():
        try:
            while not stop.wait(.12):
                if state["hold"]:
                    continue
                task = store.claim()
                if not task:
                    continue
                if task["kind"] != "multimodal":
                    raise RuntimeError("Unexpected inference task in UI fixture")
                snapshot = prepare_review(store, task["id"])
                items = []
                for target in snapshot["targets"]:
                    before = target["before"]
                    after = before.replace("001.00", "001.05").replace("Revenve", "Revenue")
                    decision = "replace" if after != before else "uncertain" if before == "Amount" else "keep"
                    items.append({"target_id": target["id"], "decision": decision, "after": after,
                                  "reason": "固定验收响应：对照局部图片检查文字；此数据不代表真实模型效果。"})
                complete_review(store, task["id"], {"items": items, "summary": "固定UI验收建议",
                    "identity": {"synthetic": True}, "timing": {"elapsed_ms": 1}})
        except BaseException as error:
            state["errors"].append(repr(error))

    app.state.queue.status = lambda: {"healthy": True, "alive": True, "state": "running", "task_id": None, "engine": None, "loaded": False}
    app.state.fusion_queue.status = app.state.queue.status
    app.router.routes[:] = [route for route in app.router.routes if not (getattr(route, "path", None) == "" and type(route).__name__ == "Mount")]
    app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", access_log=False))
    service_thread = threading.Thread(target=server.run)
    fixture_thread = threading.Thread(target=synthesize)
    service_thread.start()
    completed = None
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("UI service startup failed")
            time.sleep(.03)
        fixture_thread.start()
        seed = {"base": "http://127.0.0.1:" + str(server.servers[0].sockets[0].getsockname()[1]),
                "token": token, "project": project["id"], "result": result_id, "version": version["id"], "image": photo["id"],
                "scope": "Synthetic model catalog and deterministic inference; real API, storage, edits, history and exports. No model accuracy, speed, or GPU claim."}
        (out / "seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2), "utf-8")
        completed = subprocess.run(["node", str(ROOT / "scripts/audit_multimodal_ui.mjs"), str(out)], cwd=ROOT)
    finally:
        stop.set()
        if fixture_thread.is_alive():
            fixture_thread.join(timeout=10)
        server.should_exit = True
        service_thread.join(timeout=15)
        app.state.documents.stop()
        (out / "service-state.json").write_text(json.dumps({"fixture_errors": state["errors"],
            "service_closed": not service_thread.is_alive(), "fixture_worker_closed": not fixture_thread.is_alive()}, indent=2), "utf-8")
    if service_thread.is_alive() or fixture_thread.is_alive() or state["errors"]:
        raise RuntimeError("UI fixture did not complete or shut down cleanly")
    sys.exit(completed.returncode if completed else 1)


if __name__ == "__main__":
    main()
