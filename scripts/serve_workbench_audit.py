"""Isolated UI audit using recorded OCR plus explicit synthetic edge cases."""
import argparse
import json
import shutil
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.service import create_app
from ocr_workbench.imaging import add_image
from ocr_workbench.tables import parse_tables


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--resume", action="store_true", help="Reopen this audit's existing seed and edits")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=args.resume)
    if args.resume and not (out / "seed.json").is_file():
        raise ValueError("Resume requires an existing audit seed")
    token = "isolated-workbench-audit"
    app = create_app(ROOT, out / "data", token, start_queue=False)
    app.state.queue.status = lambda: {"healthy": True, "alive": True, "state": "running", "task_id": None, "engine": None, "loaded": False}
    app.state.fusion_queue.status = app.state.queue.status
    if args.resume:
        app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True))
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
        return
    store = app.state.store
    project = store.project("隐藏问题隔离测试")
    second = store.project("切换项目测试")
    seeds = {}
    cases = [
        ("真实GLM表格", None),
        ("混排双表", "标题\n<table><tr><td>00123</td><td>旧金额</td></tr><tr><td>00234</td><td>100.00</td></tr></table>\n中间说明\n| 编号 | 名称 |\n| --- | --- |\n| 00001 | 项目二 |\n尾注"),
        ("纯文字", "合同编号：00001234567890123456\n甲方：审计测试\n金额：100.00\n备注：保留原始编号"),
        ("引号换行", '<table><tr><td>编号</td><td>备注</td></tr><tr><td>00001</td><td>第一行<br>第二行</td></tr><tr><td>00002</td><td>&quot;quoted&quot;</td></tr></table>'),
        ("旧版本结果", "旧版本识别内容"),
        ("未识别", ""),
    ]
    for index, (name, text) in enumerate(cases):
        image = out / f"input-{index}.png"
        shutil.copyfile(ROOT / "fixtures/table.png", image)
        photo = add_image(store, project["id"], name + ".png", image)
        seeds[name] = {"photo": photo}
        if not text and text is not None:
            continue
        if text is None:
            original = json.loads((ROOT / "audit/airgap-release/inference/00-glm-table/result.json").read_text("utf-8"))
        else:
            original = {"status": "success", "engine": "glm", "text": text, "tables": parse_tables(text), "blocks": [], "elapsed_seconds": 0.1, "load_seconds": 0.1, "image": {"width": 1400, "height": 1000}}
        original["project_image_version"] = photo["active_version"]
        task = store.enqueue(project["id"], [photo["active_version"]], ["glm"])[0]
        store.claim()
        store.complete(task, original)
        seeds[name]["result"] = store.one("tasks", task)["result_id"]
        if name == "旧版本结果":
            from ocr_workbench.imaging import transform
            transform(store, photo["active_version"], {"kind": "rotate", "degrees": 90})
    image = out / "second.png"
    shutil.copyfile(ROOT / "fixtures/table.png", image)
    photo = add_image(store, second["id"], "第二项目.png", image)
    seed = {"project": project["id"], "second_project": second["id"], "seeds": seeds, "base": f"http://127.0.0.1:{args.port}", "token": token}
    (out / "seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2), "utf-8")
    app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True))
    print(f"Isolated audit ready: {seed['base']}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
