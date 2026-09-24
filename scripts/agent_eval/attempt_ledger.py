"""Package-external F05 schedule and append-once execution accounting.

No fixture is opened here, no model is called, and an execution receipt is never a
score or a model qualification. Callers supply the frozen task definitions explicitly.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets

from scripts.agent_eval.score_attempts import IncompleteEvidence


SCHEMA = "f05-attempt-ledger-v1"
TASK_ID = re.compile(r"S0[1-6]-\d{2}\Z")


def _required(obj, key):
    if not isinstance(obj, dict) or key not in obj or obj[key] is None:
        raise IncompleteEvidence(f"missing {key}")
    return obj[key]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _roster(tasks, contract, split):
    if split not in ("development", "holdout"):
        raise IncompleteEvidence("unknown split")
    execution = _required(contract, "execution")
    if (type(_required(execution, "heldout_tasks")) is not int or execution["heldout_tasks"] != 24
            or type(_required(execution, "repeats_per_configuration")) is not int
            or execution["repeats_per_configuration"] != 3
            or type(_required(execution, "total_runs")) is not int or execution["total_runs"] != 72):
        raise IncompleteEvidence("24 x 3 frozen execution contract required")
    roster = []
    for task in tasks:
        task_id = _required(task, "id")
        scenario = _required(task, "scenario")
        if (not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id)
                or scenario != task_id[:3] or task.get("split") != split):
            raise IncompleteEvidence("invalid task identity or mixed split")
        roster.append({"task_id": task_id, "scenario": scenario})
    if (len(roster) != 24 or len({item["task_id"] for item in roster}) != 24
            or Counter(item["scenario"] for item in roster) != Counter({f"S{i:02d}": 4 for i in range(1, 7)})):
        raise IncompleteEvidence("24 unique tasks, four per S01-S06, required")
    return sorted(roster, key=lambda item: item["task_id"])


def create_schedule(tasks, contract, output_root: Path, *, split: str):
    """Allocate 72 distinct empty workspaces; refuse an existing ledger root."""
    roster = _roster(tasks, contract, split)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    workspace_root = root / "workspaces"
    workspace_root.mkdir()
    (root / "outcomes").mkdir()
    slots = []
    for task in roster:
        for repeat in range(1, 4):
            name = f'{task["task_id"]}-r{repeat}-{secrets.token_hex(12)}'
            workspace = workspace_root / name
            workspace.mkdir(exist_ok=False)
            slots.append({**task, "repeat": repeat, "workspace_id": name,
                          "workspace_path": str(workspace)})
    manifest = {"schema": SCHEMA, "split": split, "contract_sha256": _digest(contract),
                "roster_sha256": _digest(roster), "created_utc": datetime.now(timezone.utc).isoformat(),
                "slots": slots}
    with (root / "schedule.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest


def _load(root, tasks, contract):
    root = Path(root).resolve(strict=True)
    manifest = json.loads((root / "schedule.json").read_text(encoding="utf-8"))
    roster = _roster(tasks, contract, _required(manifest, "split"))
    if (manifest.get("schema") != SCHEMA or manifest.get("contract_sha256") != _digest(contract)
            or manifest.get("roster_sha256") != _digest(roster)):
        raise IncompleteEvidence("schedule contract or roster changed")
    slots = _required(manifest, "slots")
    expected = {(item["task_id"], repeat): item["scenario"]
                for item in roster for repeat in range(1, 4)}
    if not isinstance(slots, list) or len(slots) != 72:
        raise IncompleteEvidence("incomplete 72-slot schedule")
    observed = {}
    ids = set()
    for slot in slots:
        key = (_required(slot, "task_id"), _required(slot, "repeat"))
        name = _required(slot, "workspace_id")
        path = Path(_required(slot, "workspace_path"))
        if (key in observed or key not in expected or slot.get("scenario") != expected[key]
                or not isinstance(name, str) or not re.fullmatch(
                    re.escape(key[0]) + rf"-r{key[1]}-[0-9a-f]{{24}}", name)
                or name in ids or path != root / "workspaces" / name
                or not path.is_dir() or path.is_symlink()
                or path.resolve(strict=True) != path):
            raise IncompleteEvidence("duplicate, missing, reused or unsafe workspace")
        observed[key] = slot
        ids.add(name)
    if set(observed) != set(expected):
        raise IncompleteEvidence("missing scheduled attempt")
    return root, manifest, observed


def record_attempt(root: Path, tasks, contract, task_id: str, repeat: int, outcome: dict):
    """Write one terminal receipt exclusively. A retry cannot replace this slot."""
    root, manifest, slots = _load(root, tasks, contract)
    if type(repeat) is not int or (task_id, repeat) not in slots:
        raise IncompleteEvidence("unknown scheduled attempt")
    slot = slots[task_id, repeat]
    if not isinstance(outcome, dict) or any(key in outcome for key in
            ("success", "score", "qualification_status", "all_gates_met", "observation",
             "schema", "split", "task_id", "repeat", "workspace_id", "workspace_path")):
        raise IncompleteEvidence("execution receipt cannot contain a score or qualification")
    _validate_outcome(outcome)
    receipt = {**outcome, "schema": SCHEMA, "split": manifest["split"],
               "task_id": task_id, "repeat": repeat, "workspace_id": slot["workspace_id"],
               "workspace_path": slot["workspace_path"]}
    contents = json.dumps(receipt, ensure_ascii=False, indent=2)
    path = root / "outcomes" / f"{task_id}-r{repeat}.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(contents)
    return receipt


def _validate_outcome(outcome):
    if any(key in outcome for key in ("success", "score", "qualification_status", "all_gates_met", "observation")):
        raise IncompleteEvidence("execution receipt cannot contain a score or qualification")
    mode = _required(outcome, "provider_mode")
    status = _required(outcome, "provider_status")
    execution_status = _required(outcome, "execution_status")
    if (mode not in ("real", "synthetic") or status not in ("received", "error")
            or (status == "error" and execution_status not in ("provider_error", "setup_error"))
            or (status == "received" and execution_status !=
                ("executed" if mode == "real" else "synthetic_executed"))):
        raise IncompleteEvidence("invalid provider/execution status")
    if mode == "real":
        for key in ("model_id", "endpoint_category", "config_revision", "input_hashes"):
            value = _required(outcome, key)
            if not value or (key in ("run_identity", "input_hashes") and not isinstance(value, dict)):
                raise IncompleteEvidence(f"missing real execution {key}")
        if status == "received" and (not isinstance(_required(outcome, "run_identity"), dict)
                                     or not outcome["run_identity"]):
            raise IncompleteEvidence("missing real execution run_identity")
        # A connection failure may occur before a run ID exists; retain it anyway.
        if status == "error" and outcome.get("run_identity") is not None and not isinstance(outcome["run_identity"], dict):
            raise IncompleteEvidence("invalid run_identity")
    if status == "error" and not _required(outcome, "error"):
        raise IncompleteEvidence("provider error reason required")
    for key in ("started_utc", "finished_utc"):
        value = _required(outcome, key)
        if not isinstance(value, str) or not value.strip():
            raise IncompleteEvidence(f"invalid {key}")


def summarize(root: Path, tasks, contract):
    """Count all scheduled slots, including errors and absent receipts; never grant support."""
    root, manifest, slots = _load(root, tasks, contract)
    known = {f"{task_id}-r{repeat}.json" for task_id, repeat in slots}
    actual = {path.name for path in (root / "outcomes").iterdir()}
    if not actual <= known:
        raise IncompleteEvidence("unknown outcome file")
    counts = Counter()
    for (task_id, repeat), slot in slots.items():
        path = root / "outcomes" / f"{task_id}-r{repeat}.json"
        if not path.exists():
            counts["pending"] += 1
            continue
        if path.is_symlink() or not path.is_file():
            raise IncompleteEvidence("unsafe outcome file")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("schema") != SCHEMA:
            raise IncompleteEvidence("unknown outcome schema")
        for key, expected in (("task_id", task_id), ("repeat", repeat),
                              ("workspace_id", slot["workspace_id"]),
                              ("workspace_path", slot["workspace_path"]),
                              ("split", manifest["split"])):
            if type(receipt.get(key)) is not type(expected) or receipt[key] != expected:
                raise IncompleteEvidence("outcome identity does not match schedule")
        _validate_outcome(receipt)
        counts["recorded"] += 1
        counts[receipt["provider_mode"]] += 1
        counts["provider_errors" if receipt["provider_status"] == "error" else "provider_received"] += 1
    return {"split": manifest["split"], "scheduled": 72, "recorded": counts["recorded"],
            "pending": counts["pending"], "real": counts["real"], "synthetic": counts["synthetic"],
            "provider_errors": counts["provider_errors"],
            "provider_received": counts["provider_received"],
            "attempt_denominator": 72, "qualification_status": "not_attested"}
