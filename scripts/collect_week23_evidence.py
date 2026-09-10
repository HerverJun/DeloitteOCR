"""Curate reproducible receipts and synthetic results, excluding live session tokens."""

import argparse
import json
from pathlib import Path
import shutil
import sqlite3

p = argparse.ArgumentParser()
p.add_argument("--build", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=True)
files = {
    "application-audit-01/application-audit.json": "application-audit.json",
    "resident-fixed/resident.json": "resident-sessions.json",
    "os-offline-app-fixed/os-network-probes.json": "offline/network-probes.json",
    "os-offline-app-fixed/os-isolation-result.json": "offline/result.json",
    "os-offline-app-fixed/inference/application-audit.json": "offline/application.json",
    "dependency-audit.json": "dependency-audit.json",
    "relocation-audit/application-audit.json": "relocation.json",
    "ui-audit/result.json": "browser/result.json",
    "ui-audit/01-import.png": "browser/01-import.png",
    "ui-audit/02-comparison.png": "browser/02-comparison.png",
    "ui-audit/03-table.png": "browser/03-table.png",
    "ui-audit/04-perspective.png": "browser/04-perspective.png",
    "ui-audit/05-laptop.png": "browser/05-laptop.png",
}
for fmt in ["xlsx", "txt", "md", "json"]:
    files["ui-audit/edited." + fmt] = "browser/edited." + fmt
for origin, dest in files.items():
    source = a.build / origin
    if not source.is_file():
        raise FileNotFoundError(source)
    target = a.output / dest
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
db = sqlite3.connect(a.build / "application-audit-01/项目 中文 空格/workbench.sqlite3")
db.row_factory = sqlite3.Row
results = a.output / "real-results"
results.mkdir(exist_ok=True)
for row in db.execute(
    "SELECT t.id,t.engine,i.name,r.original FROM results r JOIN tasks t ON t.id=r.task_id JOIN images i ON i.id=t.image_id"
):
    (results / (row["engine"] + "-" + row["id"] + ".json")).write_text(
        row["original"], "utf-8"
    )
db.close()
legacy = []
for engine in ["ppocr", "paddlevl", "glm", "hunyuan"]:
    result = json.loads(
        (a.build / "legacy-cli-smoke" / engine / "result.json").read_text("utf-8")
    )
    assert result["status"] == "success" and "00123456789012345678" in result["text"]
    legacy.append(
        {
            "engine": engine,
            "status": result["status"],
            "tables": len(result["tables"]),
            "seconds": result["elapsed_seconds"],
        }
    )
(a.output / "legacy-cli.json").write_text(
    json.dumps({"passed": True, "results": legacy}, indent=2), "utf-8"
)
print(
    f"Curated {len(files)} receipts/exports/images and real result JSON; no live tokens or user database copied"
)
