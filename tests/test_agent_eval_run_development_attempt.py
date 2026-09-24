"""F05 execution probe boundaries; no sealed tasks, truth or real provider."""

import asyncio
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.agent_eval.run_development_attempt import _development_input, execute


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "build/ocr-agent-20260920-p0/fixtures-final/development/tasks.json"
BUNDLE = ROOT / "build/document-workflow/bundle"


def test_development_only_input_and_document_hash():
    task, fixture, source, hashes = _development_input(TASKS, "S01-01")
    assert task["split"] == "development" and fixture == "development/FX01"
    assert source.is_file() and len(hashes["document_sha256"]) == 64
    with pytest.raises(ValueError, match="only development"):
        _development_input(TASKS.parent.parent / "holdout/tasks.json", "S01-01")
    with pytest.raises(ValueError, match="invalid task id"):
        _development_input(TASKS, "../FX01")


@pytest.mark.skipif(not (BUNDLE / "runtimes/pdf/python.exe").exists(), reason="PDF runtime absent")
def test_two_attempts_have_distinct_persisted_agent_identities(tmp_path):
    # The test environment must have the Agent runtime dependencies; no network is used.
    pytest.importorskip("langgraph")
    pytest.importorskip("pillow_heif")
    first = asyncio.run(execute(TASKS, "S01-01", 1, tmp_path, BUNDLE))
    second = asyncio.run(execute(TASKS, "S01-01", 2, tmp_path, BUNDLE))
    assert first["execution_status"] == second["execution_status"] == "synthetic_executed"
    assert first["workspace_id"] != second["workspace_id"]
    assert first["run_identity"]["run_id"] != second["run_identity"]["run_id"]
    for receipt in (first, second):
        assert receipt["provider_mode"] == "synthetic"
        assert receipt["real_model_qualification"] == "not_tested"
        assert "observation" not in receipt and "expected" not in receipt
        workspace = Path(receipt["workspace_path"])
        saved = json.loads((workspace.parent / "execution.json").read_text("utf-8"))
        assert saved == receipt
        db = sqlite3.connect((workspace / "workbench.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            assert db.execute("SELECT id,status FROM agent_runs").fetchall() == [
                (receipt["run_identity"]["run_id"], "completed")]
            assert db.execute("SELECT COUNT(*) FROM agent_model_requests WHERE state='received'").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM agent_events WHERE type='tool_finished'").fetchone()[0] == 1
        finally:
            db.close()
