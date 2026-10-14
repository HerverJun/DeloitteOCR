"""Install a real complete engine package through HTTP, infer and roll back."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_application import Application, until

p = argparse.ArgumentParser()
p.add_argument("--bundle", type=Path, required=True)
p.add_argument("--package", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
if a.output.exists() and any(a.output.iterdir()):
    raise ValueError("Fresh output required")
a.output.mkdir(parents=True)
app = Application(a.bundle.resolve(), a.output.resolve())
report = {"passed": False, "checks": []}


def check(name, value=True):
    assert value, name
    report["checks"].append(name)
    print(name, flush=True)


try:
    app.start()
    project = app.api("/projects", "POST", {"name": "引擎更新验收"})["id"]
    photo = app.import_file(project, a.bundle / "fixtures/printed.png")["images"][0]
    old = app.enqueue(project, [photo["active_version"]], ["ppocr"])
    app.action(project, "cancel", old)
    until(lambda: app.api("/state")["queue"]["engine"] is None, 30)
    with a.package.open("rb") as file:
        staged = app.api(
            "/engine-packages/stage",
            "POST",
            file,
            headers={
                "Authorization": "Bearer " + app.token,
                "Content-Type": "application/zip",
                "Content-Length": str(a.package.stat().st_size),
            },
            timeout=600,
        )
    check("complete real runtime and weights staged and hashed")
    check(
        "staging does not change active engine",
        app.api("/state")["engines"]["ppocr"]["package_id"] == "builtin",
    )
    activation = app.api("/engine-packages/activate", "POST", staged, timeout=600)
    report["activation"] = activation
    check(
        "activation after real dependency and model inference checks",
        activation["activated"],
    )
    app.action(project, "retry", old)
    old_task = until(lambda: app.completed(project, old), 180)[0]
    check(
        "existing task retains its original engine package",
        old_task["status"] == "succeeded" and old_task["engine_package"] == "builtin",
    )
    new = app.enqueue(project, [photo["active_version"]], ["ppocr"])
    task = until(lambda: app.completed(project, new), 180)[0]
    check(
        "new task runs installed package",
        task["status"] == "succeeded" and task["engine_package"] == staged["id"],
    )
    result = app.api("/results/" + task["result_id"])
    check(
        "installed package returns actual identifier",
        "00123456789012345678" in result["original"]["text"],
    )
    check(
        "result traces installed package",
        result["original"]["engine_package"]["id"] == staged["id"],
    )
    report["result"] = result["original"]
    app.api(
        "/engine-packages/switch",
        "POST",
        {"engine": "ppocr", "package_id": "builtin"},
        timeout=600,
    )
    check(
        "rollback selects bundled engine",
        app.api("/state")["engines"]["ppocr"]["package_id"] == "builtin",
    )
    check(
        "rollback keeps historical result",
        app.api("/results/" + task["result_id"])["original"] == result["original"],
    )
    restored = app.enqueue(project, [photo["active_version"]], ["ppocr"])
    back = until(lambda: app.completed(project, restored), 180)[0]
    check(
        "real inference succeeds after rollback",
        back["status"] == "succeeded" and back["engine_package"] == "builtin",
    )
    app.stop()
    app.start()
    check(
        "restart retains rollback and installed old package",
        app.api("/state")["engines"]["ppocr"]["package_id"] == "builtin"
        and any(
            p["id"] == staged["id"] for p in app.api("/engine-packages")["installed"]
        ),
    )
    app.stop()
    report["passed"] = True
except BaseException as error:
    report["error"] = repr(error)
    raise
finally:
    try:
        app.stop()
    finally:
        (a.output / "engine-update.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
        )
