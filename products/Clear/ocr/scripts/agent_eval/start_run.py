"""Record a new execution receipt without changing frozen plans or prior audits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
RUN = "ocr-agent-20260921-langgraph"


def main():
    audit = ROOT / "audit" / RUN
    audit.mkdir(exist_ok=True)
    target = audit / "baseline.json"
    if target.exists():
        raise SystemExit("Baseline already exists; do not overwrite")
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    ).decode().split("\0")
    hashes = {}
    for relative in paths:
        path = ROOT / relative
        if relative and path.is_file() and not relative.startswith(("research/", "output/")):
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    target.write_text(json.dumps({
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT).decode().strip(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        "platform": platform.platform(), "files": hashes,
        "p0_receipt": "audit/ocr-agent-20260920-p0/task-state.json",
        "fixtures": "build/ocr-agent-20260920-p0/fixtures-final",
        "sealed_tasks_opened": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (audit / "initial-workspace-status.txt").write_bytes(subprocess.check_output(["git", "status", "--short"], cwd=ROOT))
    plan = json.loads((ROOT / "docs/ocr-agent-development-tasks-20260920.json").read_text("utf-8"))
    (audit / "task-state.json").write_text(json.dumps({
        "plan_version": 2, "run": RUN, "tasks": [
            {"id": t["id"], "title": t["title"], "status": "inherited_done" if t["phase"] == "P0" else "pending",
             "evidence": ["audit/ocr-agent-20260920-p0/task-state.json"] if t["phase"] == "P0" else []}
            for t in plan["tasks"]]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(hashes)} baseline hashes at {target}")


if __name__ == "__main__":
    main()
