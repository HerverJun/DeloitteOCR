"""Summarize existing assertions only; pending integration is never marked passed."""
import hashlib
import json
from pathlib import Path
import re
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "audit/ocr-agent-20260921-langgraph"


def write(name, value):
    (AUDIT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    test_results = {}
    for name in ("store", "provider", "service-parity", "policy", "connection"):
        path = AUDIT / f"{name}-tests.log"
        raw = path.read_bytes()
        text = raw.decode("utf-16" if raw.startswith(b"\xff\xfe") else "utf-8", errors="replace")
        count = re.search(r"Ran (\d+) tests? in ([\d.]+)s", text)
        passed = bool(count and re.search(r"\nOK\s*$", text))
        test_results[name] = {"status": "pass" if passed else "fail", "tests": int(count[1]) if count else None,
                              "seconds": float(count[2]) if count else None, "log": str(path.relative_to(ROOT)).replace("\\", "/"),
                              "sha256": hashlib.sha256(raw).hexdigest()}
    write("foundation-checks.json", {"updated": datetime.now(timezone.utc).isoformat(), "suites": test_results,
                                    "scope": "B00/B01/B04 and incremental B02/B03/B05; not full E01-E72 acceptance"})
    write("migration-receipt.json", {"schema": 13, "previous_schema": 12, "status": test_results["store"]["status"],
                                    "evidence": test_results["store"], "user_database_migrated": False})
    write("store-contract-results.json", test_results["store"])
    write("provider-contract-results.json", test_results["provider"])
    write("service-parity-results.json", test_results["service-parity"])
    write("scope-policy-results.json", test_results["policy"])
    write("connection-probe-results.json", {"mock": test_results["connection"], "real": {"status": "not_tested", "reason": "No independently configured and authorized controller supplied; visual credentials not accessed"}})
    write("credential-redaction-results.json", {"evidence": test_results["connection"], "scope": "Synthetic keys, actual same-user Windows DPAPI; new Windows user not tested"})
    write("checkpoint-ownership.json", {
        "business": "workbench.sqlite3 schema 13: operations, requests, grants, events, run projection",
        "graph": "agent-checkpoints.sqlite3 official async saver: messages and execution position",
        "distributed_atomic_commit": False, "duplicate_message_tables": False,
        "backup": "Checkpoints.backup quiesces registered graph activity then holds file_lock -> Store.lock; backup API and atomic directory publication",
        "restore": "Offline, validated complete database pair into new directory only; document/artifact files require separate file snapshot",
        "delete": "Business session cascade emits durable tombstone; official adelete_thread then acknowledges tombstone; retries are idempotent",
        "evidence": test_results["store"], "lifecycle_integration": "pending D06",
    })
    state = json.loads((AUDIT / "task-state.json").read_text("utf-8"))
    status = {"B00": ("done", ["langgraph-compatibility.json", "langgraph-replay-probe.json", "framework-lock-candidate.json"]),
              "B01": ("done", ["migration-receipt.json", "store-contract-results.json", "checkpoint-ownership.json"]),
              "B02": ("in_progress", ["service-parity-results.json"]),
              "B03": ("in_progress", ["scope-policy-results.json"]),
              "B04": ("done", ["provider-contract-results.json"]),
              "B05": ("in_progress", ["connection-probe-results.json", "credential-redaction-results.json"])}
    for task in state["tasks"]:
        if task["id"] in status:
            task["status"], task["evidence"] = status[task["id"]]
    state["updated"] = datetime.now(timezone.utc).isoformat()
    write("task-state.json", state)
    baseline = json.loads((AUDIT / "baseline.json").read_text("utf-8"))
    frozen = {p: h for p, h in baseline["files"].items() if p.startswith("audit/ocr-agent-20260920-p0/") or p in {
        "src/ocr_workbench/agent/contracts.py", "docs/ocr-agent-development-plan-20260920.md",
        "docs/ocr-agent-development-tasks-20260920.json", "docs/ocr-agent-acceptance-plan-20260920.md"}}
    changed = [p for p, h in frozen.items() if not (ROOT / p).exists() or hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h]
    write("frozen-preservation.json", {"status": "pass" if not changed else "fail", "checked_files": len(frozen), "changed": changed})
    if changed or any(v["status"] != "pass" for v in test_results.values()):
        raise SystemExit("Foundation evidence failed")
    print("Recorded foundation evidence; remaining tasks stay pending/in_progress")


if __name__ == "__main__":
    main()
