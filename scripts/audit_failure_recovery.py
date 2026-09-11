"""A damaged task input must not block the next image; restored bytes can retry."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.task_queue import TaskQueue

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_application import until

p = argparse.ArgumentParser()
p.add_argument("--bundle", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
if a.output.exists() and any(a.output.iterdir()):
    raise ValueError("Fresh evidence directory required")
a.output.mkdir(parents=True, exist_ok=True)
store = Store(a.output / "项目 中文 空格")
project = store.project("真实失败恢复")["id"]
photos = []
for index, name in enumerate(["恢复.png", "后续.png"]):
    temporary = a.output / f"import-{index}.png"
    shutil.copy2(a.bundle / "fixtures/printed.png", temporary)
    photos.append(add_image(store, project, name, temporary))
damaged = store.file(store.one("versions", photos[0]["active_version"])["path"])
original = damaged.read_bytes()
queue = TaskQueue(store, a.bundle)
report = {
    "passed": False,
    "method": "Corrupt only an isolated test project version before recognition; restore identical bytes before retry. No model/output mocks.",
}
try:
    damaged.write_bytes(b"intentionally damaged test input")
    queue.start()
    ids = store.enqueue(project, [p["active_version"] for p in photos], ["ppocr"])
    queue.wake.set()
    until(
        lambda: all(
            store.one("tasks", key)["status"] in {"failed", "succeeded"} for key in ids
        ),
        180,
    )
    first, second = [store.one("tasks", key) for key in ids]
    assert first["status"] == "failed" and second["status"] == "succeeded"
    report["failed_task"] = first
    stable_id = second["result_id"]
    report["subsequent_task"] = second
    assert "00123456789012345678" in store.result(stable_id)["original"]["text"]
    damaged.write_bytes(original)
    report["restored_sha256"] = hashlib.sha256(damaged.read_bytes()).hexdigest()
    assert (
        report["restored_sha256"]
        == store.one("versions", photos[0]["active_version"])["sha256"]
    )
    queue.action(project, "retry", [first["id"]])
    until(lambda: store.one("tasks", first["id"])["status"] == "succeeded", 180)
    recovered = store.one("tasks", first["id"])
    assert (
        "00123456789012345678"
        in store.result(recovered["result_id"])["original"]["text"]
    )
    assert store.one("tasks", second["id"])["result_id"] == stable_id
    assert len(store.rows("SELECT * FROM results")) == 2
    report["recovered_task"] = recovered
    report["passed"] = True
finally:
    damaged.write_bytes(original)
    queue.stop()
    report["queue_stopped"] = not queue.thread or not queue.thread.is_alive()
    (a.output / "failure-recovery.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
print(
    json.dumps(
        {"passed": report["passed"], "report": str(a.output / "failure-recovery.json")}
    ),
    flush=True,
)
