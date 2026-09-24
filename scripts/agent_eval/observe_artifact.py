"""Read-only F05 observation of one published artifact in an isolated workspace.

This intentionally produces only artifact.format and artifact.hash_verified. The
caller selects an actual run/artifact ID; task definitions and expected values are
never inputs. Other predicates require separate independent observers.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import zipfile

from .score_attempts import IncompleteEvidence


_ID = re.compile(r"[a-f0-9]{32}\Z")
_FILE = re.compile(r"export(?:-partial)?\.(xlsx|txt|md|json|pdf)\Z")


def _require(condition, message):
    if not condition:
        raise IncompleteEvidence(message)


def _json(value, name):
    try:
        return json.loads(value)
    except (TypeError, ValueError) as error:
        raise IncompleteEvidence(f"invalid {name}") from error


@contextmanager
def _readonly_database(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        try:
            yield db
        finally:
            # An open read transaction keeps the SQLite file locked on Windows.
            db.rollback()


def _check_format(path, format_name):
    if format_name == "pdf":
        with path.open("rb") as stream:
            _require(stream.read(5) == b"%PDF-", "artifact content is not PDF")
    elif format_name == "xlsx":
        try:
            with zipfile.ZipFile(path) as archive:
                _require({"[Content_Types].xml", "xl/workbook.xml"} <= set(archive.namelist())
                         and archive.testzip() is None, "artifact content is not XLSX")
        except zipfile.BadZipFile as error:
            raise IncompleteEvidence("artifact content is not XLSX") from error
    elif format_name == "json":
        with path.open("r", encoding="utf-8-sig") as stream:
            _json(stream.read(), "JSON artifact content")
    else:  # txt/md exports are UTF-8 text.
        with path.open("r", encoding="utf-8-sig") as stream:
            _require("\x00" not in stream.read(), "artifact content is not text")


def observe_artifact(workspace_root: Path, *, project_id: str, run_id: str, artifact_id: str) -> dict:
    """Verify DB binding, publication event, receipt and file bytes without writes.

    Raises IncompleteEvidence on absent or inconsistent evidence. An artifact ID is
    explicit so that multiple exports cannot silently select a convenient one.
    The workspace must be a caller-provisioned isolated development/synthetic run.
    """
    _require(all(isinstance(key, str) and _ID.fullmatch(key)
                 for key in (project_id, run_id, artifact_id)), "invalid observation IDs")
    root = Path(workspace_root).resolve(strict=True)
    db_path = root / "workbench.sqlite3"
    _require(db_path.is_file() and not db_path.is_symlink(), "missing workspace database")
    try:
        with _readonly_database(db_path) as db:
            rows = db.execute("""SELECT a.*, r.session_id, r.last_event_seq, o.state AS operation_state,
                o.project_id AS operation_project, o.result AS operation_result,
                c.id AS call_id, c.tool AS call_tool,
                c.canonical_args AS call_args
                FROM agent_artifacts a
                JOIN agent_runs r ON r.id=a.run_id
                JOIN agent_sessions s ON s.id=r.session_id
                JOIN agent_operations o ON o.id=a.operation_id AND o.run_id=r.id
                JOIN agent_calls c ON c.operation_id=o.id AND c.run_id=r.id
                WHERE a.id=? AND a.project_id=? AND a.run_id=? AND s.project_id=?""",
                (artifact_id, project_id, run_id, project_id)).fetchall()
            _require(len(rows) == 1, "artifact/run/project/call binding missing or ambiguous")
            row = rows[0]
            _require(row["operation_project"] == project_id and row["operation_state"] == "finished"
                     and row["state"] == "ready" and row["call_tool"] == "export_results",
                     "artifact not a finished ready export")
            operation_result = _json(row["operation_result"], "export operation result")
            _require(isinstance(operation_result, dict)
                     and operation_result.get("artifact_id") == artifact_id
                     and operation_result.get("operation_id") == row["operation_id"],
                     "export operation does not identify artifact")
            _require(row["pinned"] or row["expires"] > time.time(), "artifact expired")
            events = db.execute("""SELECT seq,payload FROM agent_events
                WHERE session_id=? AND run_id=? AND event_key=? AND type='artifact_ready'""",
                (row["session_id"], run_id, artifact_id + ":ready")).fetchall()
            _require(len(events) == 1 and events[0]["seq"] <= row["last_event_seq"],
                     "missing committed artifact publication event")
            event = _json(events[0]["payload"], "artifact event")
            _require(event == {"artifact_id": artifact_id, "state": "ready", "bytes": row["bytes"]},
                     "artifact event does not match DB")
            manifest = _json(row["manifest"], "DB artifact manifest")
            args = _json(row["call_args"], "export call arguments")
            _require(isinstance(manifest, dict) and isinstance(args, dict)
                     and manifest.get("project_id") == project_id
                     and manifest.get("run_id") == run_id
                     and manifest.get("format") == args.get("format")
                     and manifest.get("format") in {"xlsx", "txt", "md", "json", "pdf"},
                     "export manifest and call disagree")
            relative = row["relative_path"]
            _require(isinstance(relative, str) and not Path(relative).is_absolute()
                     and "\\" not in relative, "invalid artifact relative path")
            folder = root / "agent-artifacts" / project_id / artifact_id
            _require(not folder.is_symlink() and not folder.is_junction()
                     and not folder.parent.is_symlink() and not folder.parent.is_junction(),
                     "artifact directory is a reparse point")
            receipt_path = folder / "manifest.json"
            _require(not receipt_path.is_symlink() and not receipt_path.is_junction(),
                     "artifact receipt is a reparse point")
            receipt = _json(receipt_path.read_text("utf-8"), "published artifact receipt")
            _require(isinstance(receipt, dict) and receipt.get("artifact_id") == artifact_id
                     and receipt.get("project_id") == project_id and receipt.get("manifest") == manifest,
                     "published receipt and DB disagree")
            filename = receipt.get("file")
            match = _FILE.fullmatch(filename) if isinstance(filename, str) else None
            _require(match is not None and match.group(1) == manifest["format"],
                     "published format and manifest disagree")
            target = folder / filename
            _require(not target.is_symlink() and not target.is_junction() and target.is_file(),
                     "missing or redirected artifact file")
            _require(target.relative_to(root).as_posix() == relative,
                     "DB artifact path and published file disagree")
            _require(receipt.get("sha256") == row["sha256"] and receipt.get("bytes") == row["bytes"]
                     and receipt.get("mime") == row["mime"]
                     and type(row["bytes"]) is int and row["bytes"] >= 0,
                     "published receipt metadata and DB disagree")
            digest = hashlib.sha256()
            size = 0
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            _require(size == row["bytes"] and digest.hexdigest() == row["sha256"],
                     "artifact bytes and hash disagree")
            _check_format(target, manifest["format"])
            return {"artifact": {"format": manifest["format"], "hash_verified": True}}
    except (OSError, sqlite3.Error, UnicodeError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, IncompleteEvidence):
            raise
        raise IncompleteEvidence(f"artifact evidence unavailable: {error}") from error
