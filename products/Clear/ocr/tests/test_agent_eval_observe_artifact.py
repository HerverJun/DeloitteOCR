"""F05 observer tests use an isolated persisted SQLite/event/file workspace."""

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time

import pytest

from scripts.agent_eval.observe_artifact import observe_artifact
from scripts.agent_eval.score_attempts import IncompleteEvidence


PROJECT = "1" * 32
RUN = "2" * 32
ARTIFACT = "3" * 32
SESSION = "4" * 32
OPERATION = "5" * 32


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "workbench.sqlite3"
        folder = root / "agent-artifacts" / PROJECT / ARTIFACT
        folder.mkdir(parents=True)
        body = b"synthetic export data\n"
        (folder / "export.txt").write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()
        manifest = {"project_id": PROJECT, "run_id": RUN, "format": "txt", "results": [],
                    "coverage": {"source": "explicit_results", "failed_pages": []}}
        receipt = {"artifact_id": ARTIFACT, "project_id": PROJECT, "file": "export.txt",
                   "sha256": sha, "bytes": len(body), "mime": "text/plain", "manifest": manifest}
        (folder / "manifest.json").write_text(json.dumps(receipt), "utf-8")
        with closing(sqlite3.connect(database)) as db, db:
            # Read columns match the product's v13 agent tables. The fixture is
            # persisted, but needs neither product package nor model runtime.
            db.executescript("""
                CREATE TABLE agent_sessions(id TEXT, project_id TEXT);
                CREATE TABLE agent_runs(id TEXT, session_id TEXT, last_event_seq INTEGER);
                CREATE TABLE agent_operations(id TEXT, run_id TEXT, project_id TEXT, state TEXT, result TEXT);
                CREATE TABLE agent_calls(id TEXT, run_id TEXT, operation_id TEXT,
                                         tool TEXT, canonical_args TEXT);
                CREATE TABLE agent_artifacts(id TEXT, run_id TEXT, project_id TEXT,
                    operation_id TEXT, relative_path TEXT, sha256 TEXT, bytes INTEGER,
                    mime TEXT, manifest TEXT, state TEXT, expires REAL, pinned INTEGER);
                CREATE TABLE agent_events(session_id TEXT, run_id TEXT, seq INTEGER,
                                          event_key TEXT, type TEXT, payload TEXT);
            """)
            db.execute("INSERT INTO agent_sessions VALUES(?,?)", (SESSION, PROJECT))
            db.execute("INSERT INTO agent_runs VALUES(?,?,?)", (RUN, SESSION, 7))
            db.execute("INSERT INTO agent_operations VALUES(?,?,?,?,?)", (
                OPERATION, RUN, PROJECT, "finished",
                json.dumps({"operation_id": OPERATION, "artifact_id": ARTIFACT})))
            db.execute("INSERT INTO agent_calls VALUES(?,?,?,?,?)", (
                "6" * 32, RUN, OPERATION, "export_results", json.dumps({"format": "txt"})))
            db.execute("INSERT INTO agent_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                ARTIFACT, RUN, PROJECT, OPERATION,
                (folder / "export.txt").relative_to(root).as_posix(), sha, len(body), "text/plain",
                json.dumps(manifest), "ready", time.time() + 3600, 0))
            db.execute("INSERT INTO agent_events VALUES(?,?,?,?,?,?)", (
                SESSION, RUN, 7, ARTIFACT + ":ready", "artifact_ready",
                json.dumps({"artifact_id": ARTIFACT, "state": "ready", "bytes": len(body)})))
        yield root


def observe(root, *, project=PROJECT, artifact=ARTIFACT):
    return observe_artifact(root, project_id=project, run_id=RUN, artifact_id=artifact)


def test_persisted_db_event_and_file_yield_only_verified_artifact_fields(workspace):
    assert observe(workspace) == {"artifact": {"format": "txt", "hash_verified": True}}
    with pytest.raises(IncompleteEvidence, match="binding"):
        observe(workspace, project="9" * 32)
    with pytest.raises(IncompleteEvidence, match="binding"):
        observe(workspace, artifact="0" * 32)


def test_missing_event_refuses_even_with_intact_export(workspace):
    with closing(sqlite3.connect(workspace / "workbench.sqlite3")) as db, db:
        db.execute("DELETE FROM agent_events")
    with pytest.raises(IncompleteEvidence, match="publication event"):
        observe(workspace)


def test_unlinked_operation_result_refuses(workspace):
    with closing(sqlite3.connect(workspace / "workbench.sqlite3")) as db, db:
        db.execute("UPDATE agent_operations SET result=?", (json.dumps({"artifact_id": "9" * 32}),))
    with pytest.raises(IncompleteEvidence, match="operation does not identify artifact"):
        observe(workspace)


def test_tampered_file_refuses(workspace):
    target = workspace / "agent-artifacts" / PROJECT / ARTIFACT / "export.txt"
    target.write_bytes(b"forged")
    with pytest.raises(IncompleteEvidence, match="bytes and hash"):
        observe(workspace)


def test_matching_hash_of_binary_text_still_refuses_format(workspace):
    folder = workspace / "agent-artifacts" / PROJECT / ARTIFACT
    target = folder / "export.txt"
    body = b"binary\x00data"
    target.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    receipt_path = folder / "manifest.json"
    receipt = json.loads(receipt_path.read_text("utf-8"))
    receipt.update(sha256=digest, bytes=len(body))
    receipt_path.write_text(json.dumps(receipt), "utf-8")
    with closing(sqlite3.connect(workspace / "workbench.sqlite3")) as db, db:
        db.execute("UPDATE agent_artifacts SET sha256=?, bytes=?", (digest, len(body)))
        db.execute("UPDATE agent_events SET payload=?", (json.dumps({
            "artifact_id": ARTIFACT, "state": "ready", "bytes": len(body)}),))
    with pytest.raises(IncompleteEvidence, match="content is not text"):
        observe(workspace)


def test_false_format_and_staging_refuse(workspace):
    with closing(sqlite3.connect(workspace / "workbench.sqlite3")) as db, db:
        manifest = json.loads(db.execute("SELECT manifest FROM agent_artifacts").fetchone()[0])
        manifest["format"] = "json"
        db.execute("UPDATE agent_artifacts SET manifest=?", (json.dumps(manifest),))
    with pytest.raises(IncompleteEvidence, match="manifest and call"):
        observe(workspace)
    with closing(sqlite3.connect(workspace / "workbench.sqlite3")) as db, db:
        db.execute("UPDATE agent_artifacts SET state='staging'")
    with pytest.raises(IncompleteEvidence, match="finished ready export"):
        observe(workspace)
