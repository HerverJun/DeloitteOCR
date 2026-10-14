"""F05 ledger tests use only frozen development definitions, never sealed items."""

import json
from pathlib import Path

import pytest

from scripts.agent_eval.attempt_ledger import create_schedule, record_attempt, summarize
from scripts.agent_eval.score_attempts import IncompleteEvidence


ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT = ROOT / "build/ocr-agent-20260920-p0/fixtures-final/development/tasks.json"
CONTRACT = ROOT / "scripts/agent_eval/scoring-contract.json"


@pytest.fixture
def inputs():
    tasks = json.loads(DEVELOPMENT.read_text(encoding="utf-8"))
    selected = [task for task in tasks if int(task["id"][-2:]) <= 4]
    assert len(selected) == 24
    return selected, json.loads(CONTRACT.read_text(encoding="utf-8"))


def outcome(*, mode="real", status="received"):
    result = {"provider_mode": mode, "provider_status": status,
              "execution_status": "executed" if mode == "real" else "synthetic_executed",
              "started_utc": "2026-09-24T00:00:00+00:00",
              "finished_utc": "2026-09-24T00:00:01+00:00"}
    if mode == "real":
        result.update(model_id="test-identity", endpoint_category="local",
                      config_revision="test-revision", run_identity={"run_id": "test-run"},
                      input_hashes={"document": "test-hash"})
    if status == "error":
        result.update(execution_status="provider_error", error="test connection failure")
    return result


def test_schedule_72_unique_fresh_workspaces_and_incomplete_status(tmp_path, inputs):
    tasks, contract = inputs
    root = tmp_path / "ledger"
    manifest = create_schedule(tasks, contract, root, split="development")
    assert len(manifest["slots"]) == 72
    assert len({slot["workspace_id"] for slot in manifest["slots"]}) == 72
    assert all(Path(slot["workspace_path"]).is_dir() for slot in manifest["slots"])
    assert summarize(root, tasks, contract) == {
        "split": "development", "scheduled": 72, "recorded": 0, "pending": 72,
        "real": 0, "synthetic": 0, "provider_errors": 0, "provider_received": 0,
        "attempt_denominator": 72, "qualification_status": "not_attested"}
    with pytest.raises(FileExistsError):
        create_schedule(tasks, contract, root, split="development")
    with pytest.raises(IncompleteEvidence, match="mixed split"):
        create_schedule(tasks, contract, tmp_path / "wrong", split="holdout")


def test_duplicate_or_incomplete_roster_refused_before_workspace_creation(tmp_path, inputs):
    tasks, contract = inputs
    with pytest.raises(IncompleteEvidence, match="24 unique"):
        create_schedule(tasks[:-1], contract, tmp_path / "short", split="development")
    assert not (tmp_path / "short").exists()
    with pytest.raises(IncompleteEvidence, match="24 unique"):
        create_schedule(tasks[:-1] + [tasks[0]], contract, tmp_path / "duplicate", split="development")
    assert not (tmp_path / "duplicate").exists()


def test_provider_failure_retained_and_no_retry_overwrite(tmp_path, inputs):
    tasks, contract = inputs
    root = tmp_path / "ledger"
    create_schedule(tasks, contract, root, split="development")
    task_id = tasks[0]["id"]
    connection_error = outcome(status="error")
    del connection_error["run_identity"]  # Failure may precede run creation.
    first = record_attempt(root, tasks, contract, task_id, 1, connection_error)
    assert first["provider_status"] == "error"
    with pytest.raises(FileExistsError):
        record_attempt(root, tasks, contract, task_id, 1, outcome())
    second = record_attempt(root, tasks, contract, task_id, 2, outcome())
    assert first["workspace_id"] != second["workspace_id"]
    summary = summarize(root, tasks, contract)
    assert (summary["provider_errors"], summary["recorded"], summary["pending"],
            summary["attempt_denominator"]) == (1, 2, 70, 72)
    assert summary["qualification_status"] == "not_attested"


def test_synthetic_never_scores_and_unsafe_receipts_refused(tmp_path, inputs):
    tasks, contract = inputs
    root = tmp_path / "ledger"
    create_schedule(tasks, contract, root, split="development")
    task_id = tasks[0]["id"]
    with pytest.raises(IncompleteEvidence, match="score or qualification"):
        record_attempt(root, tasks, contract, task_id, 1,
                       {**outcome(mode="synthetic"), "success": True})
    with pytest.raises(IncompleteEvidence, match="score or qualification"):
        record_attempt(root, tasks, contract, task_id, 1,
                       {**outcome(), "workspace_id": "forged"})
    with pytest.raises(IncompleteEvidence, match="missing model_id"):
        record_attempt(root, tasks, contract, task_id, 1,
                       {key: value for key, value in outcome().items() if key != "model_id"})
    record_attempt(root, tasks, contract, task_id, 1, outcome(mode="synthetic"))
    assert summarize(root, tasks, contract)["synthetic"] == 1
    assert summarize(root, tasks, contract)["qualification_status"] == "not_attested"


def test_manifest_or_workspace_tamper_refused(tmp_path, inputs):
    tasks, contract = inputs
    root = tmp_path / "ledger"
    create_schedule(tasks, contract, root, split="development")
    altered = [dict(task) for task in tasks]
    altered[0]["id"] = "S01-09"
    with pytest.raises(IncompleteEvidence, match="roster changed"):
        summarize(root, altered, contract)
    manifest_path = root / "schedule.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["slots"][0]["workspace_path"] = str(tmp_path / "elsewhere")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IncompleteEvidence, match="unsafe workspace"):
        summarize(root, tasks, contract)
