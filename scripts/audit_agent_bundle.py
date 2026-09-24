"""Audit a frozen experimental bundle without writing anywhere inside it.

The executable checks use bundled Python, an isolated user profile and a PATH
containing only Windows system directories. Network blocking is process-local;
this is not a clean-machine, physical-disconnection or GPU qualification.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
import zipfile

SCRIPT = Path(__file__).resolve()
SENTINEL = "synthetic-package-audit-secret-not-a-real-key"
TESTS = [
    "test_agent_store", "test_agent_recovery", "test_agent_connection",
    "test_agent_graph.GraphTests.test_dynamic_tools_ask_interrupt_resume_and_ordered_pairing",
    "test_agent_graph.GraphTests.test_unknown_model_post_waits_for_explicit_decision",
    "test_agent_artifacts.ArtifactTests.test_publish_crash_reconciles_without_duplicate_and_download_lease",
    "test_agent_artifacts.ArtifactTests.test_expiry_pin_lease_and_delete_crash_recovery",
    "test_store.StoreTests.test_failed_migration_rolls_back_schema_and_version",
    "test_store.StoreTests.test_v4_migration_keeps_consistent_backup_and_all_history",
    "test_batch_maintenance.BatchMaintenanceTests.test_delete_isolated_project_and_edited_results",
    "test_batch_maintenance.BatchMaintenanceTests.test_restart_completes_cleanup_after_database_commit",
]


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_path(root, relative):
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or "\\" in relative or ":" in relative:
        raise ValueError("Unsafe manifest path: " + relative)
    path = root.joinpath(*parts.parts)
    if not path.resolve().is_relative_to(root) or path.is_symlink() or path.is_junction():
        raise ValueError("Unsafe manifest target: " + relative)
    return path


def sanitized_environment(output):
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC", "SYSTEMDRIVE") if key in os.environ}
    system = Path(env.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = str(system / "System32") + os.pathsep + str(system)
    for key, name in (("LOCALAPPDATA", "local"), ("APPDATA", "roaming"), ("USERPROFILE", "profile"), ("TEMP", "temp"), ("TMP", "temp")):
        target = output / "isolated-user" / name
        target.mkdir(parents=True, exist_ok=True)
        env[key] = str(target)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONNOUSERSITE="1",
               LANGSMITH_TRACING="true", LANGCHAIN_TRACING_V2="true",
               LANGSMITH_API_KEY=SENTINEL, LANGSMITH_ENDPOINT="https://tracing-audit.invalid")
    return env


def execute(command, output, label, *, cwd=None, env=None, timeout=300, expected_exit=0):
    started = time.monotonic()
    with (output / (label + ".log")).open("w", encoding="utf-8") as log:
        result = subprocess.run([str(x) for x in command], cwd=cwd or output, env=env,
                                stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    receipt = {"command": [str(x) for x in command], "cwd": str(cwd or output),
               "exit_code": result.returncode, "expected_exit_code": expected_exit, "elapsed_seconds": round(time.monotonic() - started, 3)}
    save(output / (label + "-process.json"), receipt)
    if result.returncode != expected_exit:
        raise RuntimeError(f"{label} failed; inspect {label}.log")
    print(json.dumps({"stage": label, "status": "pass", **receipt}, ensure_ascii=False), flush=True)
    return receipt


def verify_manifest(bundle, output):
    manifest = json.loads((bundle / "manifest.json").read_text("utf-8"))
    records = manifest["files"]
    indexed = {r["path"]: r for r in records}
    if len(indexed) != len(records):
        raise ValueError("Duplicate manifest entries")
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    if actual != set(indexed) | {"manifest.json"}:
        raise ValueError("Manifest membership mismatch: " + repr(sorted(actual ^ (set(indexed) | {"manifest.json"}))))
    before = {}
    started = last = time.monotonic()
    for index, record in enumerate(records):
        path = safe_path(bundle, record["path"])
        stat = path.stat()
        before[record["path"]] = [stat.st_size, stat.st_mtime_ns]
        if stat.st_size != record["bytes"] or sha(path) != record["sha256"]:
            raise ValueError("Manifest content mismatch: " + record["path"])
        if time.monotonic() - last > 15:
            print(json.dumps({"stage": "manifest", "verified": index + 1, "total": len(records)}), flush=True)
            last = time.monotonic()
    receipt = {"status": "pass", "files": len(records), "bytes": sum(r["bytes"] for r in records),
               "manifest_sha256": sha(bundle / "manifest.json"), "elapsed_seconds": round(time.monotonic() - started, 3)}
    save(output / "manifest-verification.json", receipt)
    save(output / "candidate-file-stats-before.json", before)
    return indexed, before, receipt


def verify_source_and_dependencies(bundle, output, indexed):
    source = json.loads((bundle / "source-manifest.json").read_text("utf-8"))
    mapped = 0
    with zipfile.ZipFile(bundle / "source-code.zip") as archive:
        if len(archive.namelist()) != len(set(archive.namelist())) or set(archive.namelist()) != set(source):
            raise ValueError("Source archive member mismatch")
        for name, expected in source.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError("Source archive content mismatch: " + name)
            for origin, target in (("src", "app"), ("config", "config"), ("docs", "docs"), ("scripts", "tools")):
                if name.startswith(origin + "/"):
                    destination = target + name[len(origin):]
                    if indexed[destination]["sha256"] != expected:
                        raise ValueError("Source/runtime mapping mismatch: " + destination)
                    mapped += 1
        # Only explicitly selected synthetic test modules and their Python test
        # imports are extracted. No sealed corpus or evaluation tasks are opened.
        pending = {name.split(".")[0] for name in TESTS}
        extracted = {}
        while pending:
            module = pending.pop()
            if module in extracted:
                continue
            name = "tests/" + module + ".py"
            data = archive.read(name)
            target = output / "frozen-tests" / (module + ".py")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            extracted[module] = source[name]
            for node in ast.walk(ast.parse(data)):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                pending.update(n.split(".")[0] for n in names if n.startswith("test_") and n.split(".")[0] not in extracted)
        # The recovery test launches this fixture as a separate Python process.
        # It is data for the frozen test suite, not an import discovered by AST.
        if "test_agent_recovery" in extracted:
            name = "tests/agent_fixtures/replay_fault_worker.py"
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != source[name]:
                raise ValueError("Frozen process fixture mismatch: " + name)
            target = output / "frozen-tests/agent_fixtures/replay_fault_worker.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            extracted["agent_fixtures/replay_fault_worker"] = source[name]
    lock = json.loads((bundle / "config/runtime-locks/agent.json").read_text("utf-8"))
    license_count = 0
    for package in lock["packages"]:
        wheel = bundle / "wheelhouse/agent" / package["file"]
        if sha(wheel) != package["sha256"]:
            raise ValueError("Wheel mismatch: " + package["name"])
        if not package["licenses"]:
            raise ValueError("Missing complete license: " + package["name"])
        for item in package["licenses"]:
            target = safe_path(bundle, item["path"])
            if sha(target) != item["sha256"] or target.stat().st_size == 0:
                raise ValueError("License mismatch: " + item["path"])
            license_count += 1
        if not any("license" in Path(item["path"]).name.lower() and (bundle / item["path"]).stat().st_size > 100 for item in package["licenses"]):
            raise ValueError("No complete license text: " + package["name"])
    if len(lock["packages"]) != 41 or license_count != 51:
        raise ValueError("Unexpected framework dependency/license count")
    declared = json.loads((bundle / "licenses/agent/manifest.json").read_text("utf-8"))
    if {x["path"]: x["sha256"] for x in declared} != {x["path"]: x["sha256"] for p in lock["packages"] for x in p["licenses"]}:
        raise ValueError("License index differs from lock")
    forbidden = [name for name in indexed if any(part.lower() in {"workspace", "credentials", ".env", "development-machine.json", "agent-checkpoints.sqlite3", "workbench.sqlite3"} for part in PurePosixPath(name).parts)]
    if forbidden:
        raise ValueError("User data paths found: " + repr(forbidden))
    # Check active portable configuration, without treating documentation or
    # third-party source examples as operational credentials.
    def check_config(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"api_key", "access_token", "refresh_token", "credential_ref", "password", "secret"} and child:
                    raise ValueError("Nonempty credential field in active portable config")
                check_config(child)
        elif isinstance(value, list):
            for child in value:
                check_config(child)
    for path in (bundle / "config").rglob("*.json"):
        check_config(json.loads(path.read_text("utf-8")))
    policy = json.loads((bundle / "config/agent-policy.json").read_text("utf-8"))
    assert policy["feature_flags"]["agent_enabled"] is False
    assert lock["tracing"] == "disabled" and lock["pickle_fallback"] is False
    receipt = {"status": "pass", "source_files": len(source), "mapped_runtime_source_files": mapped,
               "source_archive_sha256": sha(bundle / "source-code.zip"), "source_manifest_sha256": sha(bundle / "source-manifest.json"),
               "framework_lock_sha256": sha(bundle / "config/runtime-locks/agent.json"),
               "wheels": len(lock["packages"]), "license_files": license_count, "frozen_test_modules": extracted,
               "user_database_or_credential_paths": forbidden, "active_config_secret_values": 0,
               "secret_scan_scope": "active configuration values and user-data path inventory; no claim of arbitrary binary secret detection"}
    save(output / "source-dependency-verification.json", receipt)
    return receipt


def rebuild_frontend(bundle, output, node, modules):
    root = output / "frontend-rebuild"
    root.mkdir()
    with zipfile.ZipFile(bundle / "source-code.zip") as archive:
        for name in archive.namelist():
            if name.startswith("frontend/"):
                target = safe_path(root, name[len("frontend/"):])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    shutil.copytree(modules, root / "node_modules")
    execute([node, root / "node_modules/typescript/bin/tsc", "-b"], output, "frontend-typescript", cwd=root)
    execute([node, root / "node_modules/vite/bin/vite.js", "build"], output, "frontend-vite", cwd=root)
    actual = {p.relative_to(root / "dist").as_posix(): sha(p) for p in (root / "dist").rglob("*") if p.is_file()}
    expected = {p.relative_to(bundle / "web").as_posix(): sha(p) for p in (bundle / "web").rglob("*") if p.is_file()}
    receipt = {"status": "pass" if actual == expected else "fail", "build_tooling": "developer Node and copied node_modules; runtime startup does not use Node",
               "node_executable": str(node), "node_sha256": sha(node), "expected": expected, "rebuilt": actual,
               "source": "candidate source-code.zip", "node_modules_source": str(modules)}
    save(output / "frontend-identity.json", receipt)
    if actual != expected:
        raise ValueError("Frozen frontend rebuild does not match bundled web")
    return receipt


def worker_setup(args):
    if Path(sys.executable).resolve() != (args.bundle / "runtimes/service/python.exe").resolve():
        raise ValueError("Worker must use candidate bundled Python")
    sys.dont_write_bytecode = True
    # Embedded Python's relative _pth already includes bundle/app. Insert only
    # the same verified app and frozen tests; never insert workspace src.
    sys.path.insert(0, str(args.bundle / "app"))
    if args.installed_dependencies:
        sys.path.insert(0, str(args.installed_dependencies))
    sys.path.insert(0, str(args.output / "frozen-tests"))
    attempts = []
    def audit(event, values):
        if event == "socket.connect":
            address = values[1]
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in {"127.0.0.1", "::1", "localhost"}:
                attempts.append({"event": event, "host": host})
                raise OSError("Package audit blocks external network")
        elif event == "socket.getaddrinfo":
            host = values[0]
            if host not in {None, "127.0.0.1", "::1", "localhost"}:
                attempts.append({"event": event, "host": str(host)})
                raise OSError("Package audit blocks external DNS")
    sys.addaudithook(audit)
    for command in ("node", "npm", "python", "pip"):
        assert shutil.which(command) is None, ("Developer tool is on the sanitized PATH", command)
    import ocr_workbench.service
    origins = {"ocr_workbench.service": str(Path(ocr_workbench.service.__file__).resolve())}
    assert Path(origins["ocr_workbench.service"]).is_relative_to(args.bundle / "app")
    return attempts, origins


async def startup(args):
    import httpx
    from ocr_workbench.service import create_app
    output = args.output / args.worker_label
    output.mkdir(parents=True, exist_ok=True)
    receipt = {}
    for enabled in (False, True):
        data = output / ("enabled" if enabled else "default")
        kwargs = {"agent_enabled": True} if enabled else {}
        app = create_app(args.bundle, data, "synthetic-local-token", start_queue=False, **kwargs)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Authorization": "Bearer synthetic-local-token"}) as client:
                status = (await client.get("/api/agent/status")).json()
                assert status["enabled"] is enabled and status["available"] is enabled, status
                home = await client.get("/")
                assert home.status_code == 200 and "<html" in home.text
                assert (await client.get("/api/health")).status_code == 200
                assert not app.state.store.rows("SELECT * FROM agent_sessions")
                receipt["enabled" if enabled else "default"] = status
                if enabled:
                    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
                    manager = app.state.agent_runtime
                    assert isinstance(manager.checkpoints.saver, AsyncSqliteSaver)
                    assert not manager.checkpoints.saver.serde.pickle_fallback
                    assert len(manager.registry.handlers) == 12
                    assert os.environ["LANGSMITH_TRACING"] == "false"
                    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
                    assert "LANGSMITH_API_KEY" not in os.environ and "LANGSMITH_ENDPOINT" not in os.environ
                    receipt["saver"] = type(manager.checkpoints.saver).__module__ + "." + type(manager.checkpoints.saver).__name__
                    receipt["serializer_pickle_fallback"] = False
                else:
                    assert not (data / "agent-checkpoints.sqlite3").exists()
    receipt["scope"] = "Authenticated ASGI app lifecycle and static UI; queues disabled, no real OCR/controller call"
    return receipt


async def persistent(args):
    from types import SimpleNamespace
    from ocr_workbench.service import create_app
    from ocr_workbench.agent.runtime import AgentRuntime
    from ocr_workbench.agent.providers import normalize_response
    from ocr_workbench.agent.store import digest
    crash = args.worker.startswith("crash-")
    job_mode = args.worker.startswith("crash-job-")
    prefix = "crash-job" if job_mode else "crash" if crash else "persistent"
    identity_file = args.output / (prefix + "-identity.json")
    base = args.output / (prefix + "-process-workspace")
    content = "处理第一页" if job_mode else "先澄清"
    document_id = None
    app = create_app(args.bundle, base, "synthetic-local-token", start_queue=False)
    calls = []
    runtime = None
    async def model(state, run):
        calls.append(state["step"])
        message = {"role": "assistant", "content": "合成包内跨进程恢复完成"}
        if state["step"] == 0:
            message = {"role": "assistant", "tool_calls": [{"id": "ask", "type": "function", "function": {"name": "ask_user", "arguments": json.dumps({"question": "选择一项？", "options": ["文字", "表格"]})}}]}
            if job_mode:
                message = {"role": "assistant", "tool_calls": [{"id": "process", "type": "function", "function": {"name": "process_pages", "arguments": json.dumps({"document_id": document_id, "page_numbers": [1], "mode": "native", "force": True})}}]}
        record, fresh = runtime.agent.begin_model_request(run["project_id"], run["id"], state["generation"], state["step"], digest(message))
        if not fresh:
            return json.loads(record["normalized_response"])
        response = normalize_response("openai_chat_completions", {"choices": [{"message": message, "finish_reason": "tool_calls" if state["step"] == 0 else "stop"}]}, record["request_id"])
        runtime.agent.finish_model_request(run["project_id"], run["id"], state["generation"], record["request_id"], response)
        return response
    runtime = await AgentRuntime(app.state.application_services, policy={"limits": {}},
                                connection=SimpleNamespace(assert_current=lambda revision: None, api_key=SENTINEL), model_override=model).open()
    try:
        if args.worker in {"persist-write", "crash-write", "crash-job-write"}:
            project = app.state.store.project("包内合成恢复项目")["id"]
            if job_mode:
                from PIL import Image
                from ocr_workbench.imaging import add_image
                tiny = base / "synthetic.png"
                Image.new("RGB", (3, 3), "white").save(tiny)
                document_id = add_image(app.state.store, project, "synthetic.png", tiny)["id"]
            session = runtime.agent.create_session(project, {"client_request_id": "create", "title": "恢复验收"})
            run = runtime.agent.create_run(project, session["id"], {"client_request_id": "run", "content": content}, context={}, config={"revision": 1}, limits={})
            if job_mode:
                runtime.policy.grant(project, "scoped_processing", {"document_ids": [document_id], "page_ids": [document_id], "mode": "native", "force": True},
                                     source="user_request", expires=time.time() + 300, run_id=run["id"])
            save(identity_file, {"project_id": project, "session_id": session["id"], "run_id": run["id"], "thread_id": run["graph_thread_id"], "document_id": document_id})
            runtime.schedule(run)
            await asyncio.wait_for(runtime.tasks[run["id"]], 15)
            expected_wait = "waiting_jobs" if job_mode else "waiting_user"
            assert runtime.agent.run(project, run["id"])["status"] == expected_wait
            snapshot = await runtime.graph.aget_state({"configurable": {"thread_id": run["graph_thread_id"]}})
            assert len(snapshot.interrupts) == 1 and calls == [0]
            result = {"status_before_shutdown": expected_wait, "model_steps": calls, "checkpoint_id": snapshot.config["configurable"]["checkpoint_id"], "interrupts": len(snapshot.interrupts)}
            if job_mode:
                assert len(app.state.store.rows("SELECT * FROM document_stages")) == 1
                assert len(app.state.store.rows("SELECT * FROM agent_operations")) == 1
                result.update(document_stages=1, committed_business_operations=1)
            if crash:
                save(args.output / (args.worker_label + "-before-exit.json"), {"status": "ready_for_forced_exit", "pid": os.getpid(), "executable": sys.executable,
                     "application_origin": str(Path(sys.modules["ocr_workbench.agent.runtime"].__file__).resolve()), "exit_code": 91, "runtime_close_called": False,
                     "saver_close_called": False, "window": "business stage committed and waiting_jobs checkpoint durable" if job_mode else "ask_user interrupt persisted with durability=sync; runtime still open", "result": result})
                os._exit(91)
        else:
            identity = json.loads(identity_file.read_text("utf-8"))
            document_id = identity.get("document_id")
            project = identity["project_id"]
            run = runtime.agent.run(project, identity["run_id"])
            assert run["status"] == "interrupted" and calls == []
            config = {"configurable": {"thread_id": identity["thread_id"]}}
            snapshot = await runtime.graph.aget_state(config)
            assert snapshot.interrupts
            await runtime._observe_once()
            assert calls == [], "Restart must not call the controller"
            await runtime.resume_interrupted(project, run["id"], run["generation"], request_id="explicit-continue")
            await runtime.resume_interrupted(project, run["id"], run["generation"], request_id="explicit-continue")
            current = runtime.agent.run(project, run["id"])
            assert current["status"] == ("waiting_jobs" if job_mode else "waiting_user") and calls == []
            if job_mode:
                assert len(app.state.store.rows("SELECT * FROM document_stages")) == 1
                assert len(app.state.store.rows("SELECT * FROM agent_operations")) == 1
                stage = app.state.store.claim_document_stage()
                assert stage is not None
                app.state.store.finish_document_stage(stage["id"], {"synthetic": True})
                await runtime._observe_once()
            else:
                decision = app.state.store.rows("SELECT * FROM agent_decisions WHERE status='pending'")[0]
                answer = {"client_request_id": "explicit-reply", "payload_hash": decision["payload_hash"], "option_id": "option-0"}
                runtime.agent.reply_decision(project, run["id"], decision["id"], answer)
                runtime.agent.reply_decision(project, run["id"], decision["id"], answer)
                runtime.schedule(current, resume=True)
            await asyncio.wait_for(runtime.tasks[run["id"]], 15)
            assert runtime.agent.run(project, run["id"])["status"] == "completed" and calls == [1]
            snapshot = await runtime.graph.aget_state(config)
            assert not snapshot.next
            json.dumps(snapshot.values, ensure_ascii=False)
            assert SENTINEL not in json.dumps(snapshot.values)
            replay = runtime.agent.create_run(project, identity["session_id"], {"client_request_id": "run", "content": content}, context={}, config={"revision": 1}, limits={})
            assert replay["id"] == run["id"]
            assert len(app.state.store.rows("SELECT * FROM agent_runs")) == 1
            assert len(app.state.store.rows("SELECT * FROM agent_model_requests")) == 2
            assert len(app.state.store.rows("SELECT * FROM agent_calls")) == 1
            result = {"status_after_restart": "interrupted", "automatic_model_calls": 0, "explicit_continue_model_calls": 0,
                      "after_explicit_reply": "completed", "model_steps_in_new_process": calls, "strict_json_graph_values": True,
                      "controller": "synthetic override; no real provider", "duplicate_reply_and_send": "one run, one tool call, two model requests",
                      "shutdown_kind": "os._exit(91) after durable interrupt, no runtime/saver close" if crash else "graceful separate Python processes, not forced OS crash"}
            if job_mode:
                assert len(app.state.store.rows("SELECT * FROM document_stages")) == 1
                assert len(app.state.store.rows("SELECT * FROM agent_operations")) == 1
                result.update(after_explicit_reply="not_applicable", after_explicit_continue_and_job_completion="completed", document_stages=1,
                              committed_business_operations=1, duplicate_business_effects=0,
                              business_completion="synthetic queue result; actual OCR engine was not run", duplicate_reply_and_send="duplicate continue and send preserve one submitted stage and one operation")
    finally:
        await runtime.close()
    for file in base.glob("*.sqlite3*"):
        assert SENTINEL.encode() not in file.read_bytes(), "Runtime secret found in persisted database"
    result["synthetic_runtime_secret_absent_from_database_bytes"] = True
    return result


def exact_migration(args):
    """Exercise the actual planned schema-12 boundary with a mid-upgrade fault."""
    from unittest.mock import patch
    from ocr_workbench.store import Store, SCHEMA_VERSION
    root = args.output / (args.worker_label + "-workspace")
    root.mkdir()
    path = root / "workbench.sqlite3"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        for version in range(1, 13):
            Store._migrate(db, version)
        db.execute("INSERT INTO projects(id,name,created,updated) VALUES('preserved-project','迁移前合成项目','before','before')")
        db.execute("PRAGMA user_version=12")
    original = Store._migrate
    def interrupted(db, version):
        if version == 14:
            raise OSError("synthetic schema-14 migration interruption")
        original(db, version)
    with patch.object(Store, "_migrate", staticmethod(interrupted)):
        try:
            Store(root)
        except OSError:
            pass
        else:
            raise AssertionError("Migration fault was not propagated")
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='agent_sessions'").fetchall()
        assert db.execute("SELECT name FROM projects WHERE id='preserved-project'").fetchone()[0] == "迁移前合成项目"
    backups = list((root / "database-backups").glob("*.sqlite3"))
    assert backups
    for backup in backups:
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 12
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT name FROM projects WHERE id='preserved-project'").fetchone()[0] == "迁移前合成项目"
    store = Store(root)
    assert store.schema_version == SCHEMA_VERSION == 14
    assert store.one("projects", "preserved-project")["name"] == "迁移前合成项目"
    return {"initial_schema": 12, "injected_failure_at": 14, "rolled_back_to": 12, "partial_agent_schema_absent": True,
            "backup_count": len(backups), "backup_schema": 12, "retry_schema": 14, "preexisting_project_preserved_in_failed_migration_backup_and_retry": True,
            "scope": "exact schema-12 SQLite migration and rollback; no old release executable run"}


def launcher_worker(args):
    """Run the unmodified packaged opt-in entry with a synthetic session token."""
    import runpy
    import secrets
    import uvicorn
    entry = args.bundle / "tools/start_agent_candidate.py"
    original_run = uvicorn.Server.run
    token = "synthetic-audit-loopback-token-for-service-only"
    def capture(server, sockets=None):
        save(args.output / (args.worker_label + "-endpoint.json"), {"port": sockets[0].getsockname()[1], "pid": os.getpid()})
        return original_run(server, sockets=sockets)
    uvicorn.Server.run = capture
    secrets.token_urlsafe = lambda size: token
    sys.argv = [str(entry), "--bundle", str(args.bundle), "--data", str(args.output / (args.worker_label + "-workspace")), "--no-browser", "--review-only"]
    runpy.run_path(str(entry), run_name="__main__")
    return {"entry": str(entry), "entry_sha256": sha(entry), "server_shutdown": "authenticated POST /api/shutdown", "browser_opened": False,
            "synthetic_session_token": True, "entry_source_modified": False, "queues": "review-only ordinary queue startup; no tasks submitted"}


def worker(args):
    attempts, origins = worker_setup(args)
    if args.worker == "startup":
        result = asyncio.run(startup(args))
    elif args.worker.startswith(("persist-", "crash-")):
        result = asyncio.run(persistent(args))
    elif args.worker == "migration":
        result = exact_migration(args)
    elif args.worker == "launcher":
        result = launcher_worker(args)
    else:
        class RecordedResult(unittest.TextTestResult):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.passed_ids = []
            def addSuccess(self, test):
                self.passed_ids.append(test.id())
                super().addSuccess(test)
        result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(unittest.defaultTestLoader.loadTestsFromNames(TESTS))
        summary = {"tests": result.testsRun, "passed": result.passed_ids, "failures": [str(t) for t, _ in result.failures],
                   "errors": [str(t) for t, _ in result.errors], "skipped": [(str(t), r) for t, r in result.skipped], "successful": result.wasSuccessful()}
        save(args.output / (args.worker_label + "-tests.json"), summary)
        if not result.wasSuccessful() or result.skipped:
            raise AssertionError("Package regression tests failed or skipped")
        result = summary
    for name, module in tuple(sys.modules.items()):
        if name.startswith("ocr_workbench") and getattr(module, "__file__", None):
            origin = Path(module.__file__).resolve()
            assert origin.is_relative_to(args.bundle / "app"), (name, str(origin))
            origins[name] = str(origin)
    lock = json.loads((args.bundle / "config/runtime-locks/agent.json").read_text("utf-8"))
    distributions = {}
    for package in lock["packages"]:
        dist = importlib.metadata.distribution(package["name"])
        origin = Path(dist.locate_file("")).resolve()
        assert dist.version == package["version"]
        assert origin.is_relative_to(args.installed_dependencies or args.bundle / "runtimes/service")
        distributions[package["name"]] = {"version": dist.version, "origin": str(origin)}
    assert not attempts, attempts
    save(args.output / (args.worker_label + "-result.json"), {"status": "pass", "mode": args.worker,
         "python": sys.version, "executable": sys.executable, "path": os.environ["PATH"], "sys_path": sys.path,
         "application_module_origins": origins, "locked_distributions": distributions,
         "external_network_attempts": attempts, "network_guard": "Python socket audit hook; OS network remains enabled", "result": result})


def run_worker(args, bundle, mode, label, environment, installed=None):
    command = [bundle / "runtimes/service/python.exe", "-X", "utf8", "-B", "-I", SCRIPT, "--bundle", bundle, "--output", args.output, "--worker", mode, "--worker-label", label]
    if installed:
        command.extend(["--installed-dependencies", installed])
    return execute(command, args.output, label, env=environment, expected_exit=91 if mode in {"crash-write", "crash-job-write"} else 0)


def install_offline(args, environment, label):
    if args.installer_python is None:
        raise ValueError("Provide --installer-python for the external pip installer; the portable runtime intentionally has no pip")
    installer = args.installer_python.resolve()
    target = args.output / "offline-installed-framework"
    if target.exists():
        raise ValueError("Offline installation target already exists")
    command = [installer, "-X", "utf8", "-B", "-I", "-m", "pip", "--python", args.bundle / "runtimes/service/python.exe", "install",
               "--disable-pip-version-check", "--no-compile", "--no-index", "--ignore-installed", "--require-hashes", "--find-links", args.bundle / "wheelhouse/agent",
               "--target", target, "-r", args.bundle / "config/runtime-locks/agent.txt"]
    result = execute(command, args.output, label, env=environment)
    save(args.output / (label + "-installer.json"), {"external_installer_python": str(installer), "external_installer_python_sha256": sha(installer),
         "installation_target": str(target), "interpreter_used_for_target": str(args.bundle / "runtimes/service/python.exe"),
         "portable_runtime_modified": False, "scope": "external developer pip installs all 41 hash-locked wheels offline into a new isolated target; package startup itself uses no developer Python/Node"})
    return target


def check_live_entry(args, environment, label, bundle=None):
    import urllib.request
    bundle = bundle or args.bundle
    command = [bundle / "runtimes/service/python.exe", "-X", "utf8", "-B", "-I", SCRIPT, "--bundle", bundle, "--output", args.output, "--worker", "launcher", "--worker-label", label]
    endpoint = args.output / (label + "-endpoint.json")
    if endpoint.exists():
        raise ValueError("Launcher endpoint marker already exists")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token = "synthetic-audit-loopback-token-for-service-only"
    started = time.monotonic()
    with (args.output / (label + ".log")).open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(x) for x in command], cwd=args.output, env=environment, stdout=log, stderr=subprocess.STDOUT)
        base = None
        stopped = False
        try:
            while time.monotonic() - started < 25:
                if process.poll() is not None:
                    raise RuntimeError("Experimental entry exited before serving HTTP")
                if endpoint.exists():
                    port = json.loads(endpoint.read_text("utf-8"))["port"]
                    base = "http://127.0.0.1:" + str(port)
                    try:
                        with opener.open(base + "/api/health", timeout=1) as response:
                            response.read()
                            if response.status == 200:
                                break
                    except OSError:
                        pass
                time.sleep(.1)
            else:
                raise TimeoutError("Experimental entry HTTP did not become ready")
            def request(path, *, method="GET"):
                req = urllib.request.Request(base + path, method=method, headers={"Authorization": "Bearer " + token})
                with opener.open(req, timeout=5) as response:
                    return response.status, response.read()
            code, raw = request("/api/agent/status")
            status = json.loads(raw)
            assert code == 200 and status["enabled"] and status["available"], status
            assert request("/")[0] == 200
            assert request("/api/shutdown", method="POST")[0] == 200
            process.wait(timeout=20)
            stopped = True
            assert process.returncode == 0, "Entry shutdown failed"
            result = {"status": "pass", "pid": process.pid, "agent_status": status, "http_health": 200, "http_static_ui": 200,
                      "authenticated_shutdown": 200, "exit_code": process.returncode, "elapsed_seconds": round(time.monotonic() - started, 3),
                      "entry": "unmodified tools/start_agent_candidate.py; token generator replaced only for synthetic local HTTP authentication",
                      "command": [str(x) for x in command]}
            save(args.output / (label + "-http.json"), result)
            return result
        finally:
            if not stopped and process.poll() is None:
                if base:
                    try:
                        req = urllib.request.Request(base + "/api/shutdown", method="POST", headers={"Authorization": "Bearer " + token})
                        opener.open(req, timeout=3).close()
                        process.wait(timeout=15)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)


def supplemental_checks(args):
    """Append stronger checks to a completed audit, preserving original receipts."""
    before = json.loads((args.output / "candidate-file-stats-before.json").read_text("utf-8"))
    manifest = json.loads((args.output / "manifest-verification.json").read_text("utf-8"))
    def unchanged():
        actual = {p.relative_to(args.bundle).as_posix(): [p.stat().st_size, p.stat().st_mtime_ns] for p in args.bundle.rglob("*") if p.is_file() and p.relative_to(args.bundle).as_posix() != "manifest.json"}
        assert before == actual and sha(args.bundle / "manifest.json") == manifest["manifest_sha256"]
    unchanged()
    environment = sanitized_environment(args.output)
    shutil.copy2(SCRIPT, args.output / ("audit-script-" + sha(SCRIPT)[:16] + ".py"))
    stages = []
    installed = install_offline(args, environment, "offline-install-v2")
    run_worker(args, args.bundle, "startup", "offline-installed-startup", environment, installed)
    stages.append("offline-installed-startup-result.json")
    run_worker(args, args.bundle, "crash-write", "forced-crash-write", environment)
    run_worker(args, args.bundle, "crash-read", "forced-crash-read", environment)
    stages.append("forced-crash-read-result.json")
    run_worker(args, args.bundle, "migration", "exact-schema12-fault", environment)
    stages.append("exact-schema12-fault-result.json")
    check_live_entry(args, environment, "experimental-entry-http")
    stages.append("experimental-entry-http-http.json")
    unchanged()
    existing = json.loads((args.output / "bundle-validation.json").read_text("utf-8"))
    remaining = [failure for failure in existing.get("stage_failures", []) if failure["stage"] != "offline-install"]
    result = {"status": "pass", "candidate_modified": False, "audit_script_sha256": sha(SCRIPT), "evidence": stages,
              "supersedes": {"offline-install": "original command assumed bundled pip; external installer drives bundled Python with all 41 required wheel hashes and --no-index"},
              "remaining_failures": remaining,
              "forced_exit_scope": "one os._exit(91) at a persisted user interrupt, followed by explicit continuation/reply and request replay; not the entire crash-window matrix"}
    save(args.output / "supplemental-results.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--relocation-root", type=Path)
    parser.add_argument("--frontend-node", type=Path)
    parser.add_argument("--frontend-node-modules", type=Path)
    parser.add_argument("--installer-python", type=Path)
    parser.add_argument("--worker", choices=["startup", "tests", "persist-write", "persist-read", "crash-write", "crash-read", "crash-job-write", "crash-job-read", "migration", "launcher"])
    parser.add_argument("--worker-label", default="worker")
    parser.add_argument("--installed-dependencies", type=Path)
    parser.add_argument("--resume-verified-manifest", action="store_true", help="After an audit-script failure, reuse the completed full hash pass only if every candidate name/size/mtime and the manifest hash remain identical")
    parser.add_argument("--supplement", action="store_true", help="Append offline reinstallation, forced process exit, exact migration and real HTTP entry checks to an existing audit")
    args = parser.parse_args()
    args.bundle, args.output = args.bundle.resolve(), args.output.resolve()
    if args.worker:
        worker(args)
        return
    if args.supplement:
        supplemental_checks(args)
        return
    if args.output.exists() and not args.resume_verified_manifest:
        raise ValueError("Audit output must be a new directory")
    if not args.relocation_root or not args.frontend_node or not args.frontend_node_modules:
        raise ValueError("Complete audit requires relocation root and frontend rebuild tooling")
    relocation = args.relocation_root.resolve()
    if relocation.exists() or relocation.is_relative_to(args.bundle) or args.output.is_relative_to(args.bundle):
        raise ValueError("Audit paths must be new paths outside the candidate")
    args.output.mkdir(parents=True, exist_ok=args.resume_verified_manifest)
    started = time.monotonic()
    status = {"status": "running", "bundle": str(args.bundle), "audit_script_sha256": sha(SCRIPT), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "limitations": ["same Windows host; clean machine not_tested", "selected runtime/app/config/web relocation; full 29 GB package relocation not_tested",
                              "process-local network denial, not physical disconnection", "synthetic controller; real controller not_tested",
                              "forced exit at persisted user/job interrupts; other forced-exit windows not_tested", "no GPU throughput or long-term growth claim",
                              "no old-release executable rollback claim; migration backups and schema rollback are tested"]}
    if args.resume_verified_manifest:
        previous = json.loads((args.output / "bundle-validation.json").read_text("utf-8"))
        assert previous["status"] == "fail" and previous["bundle"] == str(args.bundle)
        save(args.output / ("previous-failed-attempt-" + str(time.time_ns()) + ".json"), previous)
    shutil.copy2(SCRIPT, args.output / ("audit-script-" + sha(SCRIPT)[:16] + ".py"))
    save(args.output / "bundle-validation.json", status)
    try:
        if args.resume_verified_manifest:
            manifest = json.loads((args.output / "manifest-verification.json").read_text("utf-8"))
            before = json.loads((args.output / "candidate-file-stats-before.json").read_text("utf-8"))
            actual = {p.relative_to(args.bundle).as_posix(): [p.stat().st_size, p.stat().st_mtime_ns] for p in args.bundle.rglob("*") if p.is_file() and p.relative_to(args.bundle).as_posix() != "manifest.json"}
            assert before == actual and sha(args.bundle / "manifest.json") == manifest["manifest_sha256"], "Candidate changed since full hash verification"
            indexed = {r["path"]: r for r in json.loads((args.bundle / "manifest.json").read_text("utf-8"))["files"]}
            save(args.output / "manifest-reuse.json", {"status": "pass", "original_full_hash_receipt": "manifest-verification.json", "unchanged_names_sizes_mtime": len(actual), "manifest_sha256": manifest["manifest_sha256"]})
        else:
            indexed, before, manifest = verify_manifest(args.bundle, args.output)
        identity = verify_source_and_dependencies(args.bundle, args.output, indexed)
        failures = []
        def independent(label, action):
            try:
                return action()
            except Exception as error:
                failures.append({"stage": label, "error_type": type(error).__name__, "error": str(error)})
                save(args.output / "stage-failures.json", failures)
                print(json.dumps(failures[-1]), flush=True)
                return None
        independent("frontend-rebuild", lambda: rebuild_frontend(args.bundle, args.output, args.frontend_node.resolve(), args.frontend_node_modules.resolve()))
        environment = sanitized_environment(args.output)
        python = args.bundle / "runtimes/service/python.exe"
        def invoke(bundle, mode, label, installed=None):
            return run_worker(args, bundle, mode, label, environment, installed)
        independent("candidate-startup", lambda: invoke(args.bundle, "startup", "candidate-startup"))
        independent("candidate-regressions", lambda: invoke(args.bundle, "tests", "candidate-regressions"))
        if independent("persistent-write", lambda: invoke(args.bundle, "persist-write", "persistent-write")):
            independent("persistent-read", lambda: invoke(args.bundle, "persist-read", "persistent-read"))
        if independent("forced-crash-write", lambda: invoke(args.bundle, "crash-write", "forced-crash-write")):
            independent("forced-crash-read", lambda: invoke(args.bundle, "crash-read", "forced-crash-read"))
        if independent("forced-job-crash-write", lambda: invoke(args.bundle, "crash-job-write", "forced-job-crash-write")):
            independent("forced-job-crash-read", lambda: invoke(args.bundle, "crash-job-read", "forced-job-crash-read"))
        independent("exact-schema12-fault", lambda: invoke(args.bundle, "migration", "exact-schema12-fault"))
        independent("experimental-entry-http", lambda: check_live_entry(args, environment, "experimental-entry-http"))
        installed = independent("offline-install", lambda: install_offline(args, environment, "offline-install"))
        if installed:
            independent("offline-installed-startup", lambda: invoke(args.bundle, "startup", "offline-installed-startup", installed))
        relocation.mkdir(parents=True)
        copied = []
        for name in ("runtimes/service", "app", "config", "web", "tools"):
            for source in (args.bundle / name).rglob("*"):
                if source.is_file():
                    relative = source.relative_to(args.bundle).as_posix()
                    target = relocation / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    if sha(target) != indexed[relative]["sha256"]:
                        raise ValueError("Relocation copy mismatch: " + relative)
                    copied.append(relative)
        save(args.output / "relocation-copy.json", {"status": "pass", "destination": str(relocation), "files": copied,
             "files_count": len(copied), "bytes": sum(indexed[x]["bytes"] for x in copied), "full_bundle": False, "same_machine": True})
        independent("chinese-relocation-startup", lambda: invoke(relocation, "startup", "chinese-relocation-startup"))
        independent("chinese-relocation-entry", lambda: check_live_entry(args, environment, "chinese-relocation-entry", relocation))
        after = {p.relative_to(args.bundle).as_posix(): [p.stat().st_size, p.stat().st_mtime_ns] for p in args.bundle.rglob("*") if p.is_file() and p.relative_to(args.bundle).as_posix() != "manifest.json"}
        if before != after or sha(args.bundle / "manifest.json") != manifest["manifest_sha256"]:
            raise ValueError("Candidate changed during audit")
        save(args.output / "candidate-unchanged.json", {"status": "pass", "files": len(before), "method": "full pre-audit content hashes; complete post-audit names/sizes/mtime plus manifest SHA256", "manifest_sha256": manifest["manifest_sha256"]})
        status.update(status="fail" if failures else "pass_with_qualification_limits", stage_failures=failures, manifest=manifest, source_and_dependencies=identity,
                      elapsed_seconds=round(time.monotonic() - started, 3), candidate_modified=False)
        save(args.output / "target-environment-results.json", {"status": "inspect_per_stage_receipts" if failures else "available_environment_checks_passed", "clean_windows": "not_tested", "same_host": True,
             "evidence": ["candidate-startup-result.json", "offline-install-process.json", "offline-installed-startup-result.json", "chinese-relocation-startup-result.json", "experimental-entry-http-http.json", "chinese-relocation-entry-http.json"], "limitations": status["limitations"]})
        save(args.output / "upgrade-rollback-results.json", {"status": "inspect_per_stage_receipts" if failures else "synthetic_package_regressions_passed", "source": "tests frozen in candidate source archive, imported application from candidate app",
             "evidence": ["candidate-regressions-tests.json", "persistent-write-result.json", "persistent-read-result.json", "forced-crash-read-result.json", "forced-job-crash-read-result.json", "exact-schema12-fault-result.json"],
             "old_release_executable_rollback": "not_tested", "forced_os_crash": "inspect forced-crash-read-result.json and forced-job-crash-read-result.json; two persisted-interrupt windows"})
    except BaseException as error:
        status.update(status="fail", error_type=type(error).__name__, error=str(error), elapsed_seconds=round(time.monotonic() - started, 3))
        save(args.output / "bundle-validation.json", status)
        raise
    save(args.output / "bundle-validation.json", status)
    print(json.dumps(status, ensure_ascii=False), flush=True)
    if status["status"] == "fail":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
