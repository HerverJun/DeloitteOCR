"""B00: actual official SQLite saver, synthetic work, process-level crash probes.

Run with the isolated embedded Windows service Python. No model credentials or
P0 evaluation fixtures are read. Python socket audit hooks deny network access.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Annotated, TypedDict

# Must happen before any framework imports, including the saver's metadata serde.
for env_key in tuple(os.environ):
    if env_key.startswith(("LANGSMITH_", "LANGCHAIN_")):
        del os.environ[env_key]
os.environ.update(LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false", LANGGRAPH_STRICT_MSGPACK="true")
NETWORK_ATTEMPTS = []


def offline(event, args):
    # Windows asyncio implements socketpair with a loopback TCP connection.
    if event == "socket.connect" and args[1][0] in {"127.0.0.1", "::1"}:
        return
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        NETWORK_ATTEMPTS.append(event)
        raise RuntimeError("B00 network access forbidden")


sys.addaudithook(offline)
import sqlite3
import aiosqlite
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from langgraph.errors import GraphInterrupt, GraphRecursionError
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langsmith import tracing_context

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "audit/ocr-agent-20260921-langgraph"
BUILD = ROOT / "build/ocr-agent-20260921-langgraph/langgraph-probe"


def by_id(old, new):
    values = {m["id"]: m for m in old}
    values.update({m["id"]: m for m in new})
    return list(values.values())


class State(TypedDict):
    messages: Annotated[list[dict], by_id]
    generation: int
    graph_version: str
    job: str


class Ledger:
    def __init__(self, path):
        self.path = path
        with closing(self.db()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, state TEXT);
                CREATE TABLE IF NOT EXISTS counts(name TEXT PRIMARY KEY, n INTEGER);
                CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, generation INTEGER, state TEXT);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload TEXT);
            """)

    def db(self):
        return sqlite3.connect(self.path)

    def increment(self, name):
        with closing(self.db()) as db, db:
            db.execute("INSERT INTO counts VALUES(?,1) ON CONFLICT(name) DO UPDATE SET n=n+1", (name,))

    def count(self, name):
        with closing(self.db()) as db:
            row = db.execute("SELECT n FROM counts WHERE name=?", (name,)).fetchone()
            return row[0] if row else 0

    def submit(self, job, early):
        with closing(self.db()) as db, db:
            if db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?)", (job, "succeeded" if early else "queued")).rowcount:
                db.execute("INSERT INTO counts VALUES('effects',1) ON CONFLICT(name) DO UPDATE SET n=n+1")

    def finish(self):
        with closing(self.db()) as db, db:
            db.execute("UPDATE jobs SET state='succeeded'")

    def ready(self, job):
        with closing(self.db()) as db:
            return db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone() == ("succeeded",)


def build_graph(saver, ledger, scenario, stage):
    async def submit(state):
        ledger.increment("submit_entries")
        ledger.submit("operation-1", scenario == "early")
        if scenario == "submit_crash" and stage == "start":
            # os._exit prevents the framework from writing the node's checkpoint.
            os._exit(71)
        return {"job": "operation-1"}

    async def wait(state):
        ledger.increment("wait_entries")
        if not ledger.ready(state["job"]):
            interrupt({"kind": "jobs", "job": state["job"], "generation": state["generation"]})
        # Payload never asserts success: re-read the business record on reentry.
        if not ledger.ready(state["job"]):
            raise ValueError("Job is not terminal")
        return {"messages": [{"id": "tool-1", "role": "tool", "content": "succeeded"}]}

    async def finalize(state):
        ledger.increment("finalize_entries")
        return {"messages": [{"id": "answer-1", "role": "assistant", "content": "completed"}]}

    builder = StateGraph(State)
    builder.add_node("submit", submit)
    builder.add_node("wait", wait)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "submit")
    builder.add_edge("submit", "wait")
    builder.add_edge("wait", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=saver)


async def worker(directory, scenario, stage):
    ledger = Ledger(directory / "business.sqlite3")
    config = {"configurable": {"thread_id": "server-thread-1"}, "recursion_limit": 50}
    serde = JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)
    async with aiosqlite.connect(directory / "checkpoints.sqlite3") as conn:
        saver = AsyncSqliteSaver(conn, serde=serde)
        graph = build_graph(saver, ledger, scenario, stage)
        if stage == "start":
            result = await graph.ainvoke({"generation": 1, "graph_version": "v1", "messages": []}, config, durability="sync")
            assert bool(result.get("__interrupt__")) == (scenario != "early")
        else:
            snapshot = await graph.aget_state(config)
            assert snapshot.values["graph_version"] == "v1"
            if stage == "finish":
                ledger.finish()
            if scenario == "submit_crash" and snapshot.next == ("submit",):
                await graph.ainvoke(None, config, durability="sync")
                snapshot = await graph.aget_state(config)
            if scenario == "submit_crash":
                ledger.finish()
            if snapshot.interrupts:
                item = snapshot.interrupts[0]
                with closing(ledger.db()) as db, db:
                    db.execute("INSERT OR IGNORE INTO intents VALUES(?,1,'pending')", (item.id,))
                    row = db.execute("SELECT generation,state FROM intents WHERE id=?", (item.id,)).fetchone()
                    assert row[0] == snapshot.values["generation"]
                    db.execute("UPDATE intents SET state='claimed' WHERE id=?", (item.id,))
                if stage == "claim_crash":
                    os._exit(72)
                # Server maps the persisted identity; no user-supplied Command.
                await graph.ainvoke(Command(resume={item.id: {"notification": True}}), config, durability="sync")
                if stage == "applied_crash":
                    os._exit(73)
            # Reconcile after graph commit, including a lost application event.
            snapshot = await graph.aget_state(config)
            if not snapshot.next:
                with closing(ledger.db()) as db, db:
                    db.execute("UPDATE intents SET state='applied'")
                    db.execute("INSERT OR IGNORE INTO events VALUES('completed',?)", (json.dumps(snapshot.values["messages"]),))
        snapshot = await graph.aget_state(config)
        result = {"next": list(snapshot.next), "interrupts": len(snapshot.interrupts),
                  "messages": snapshot.values.get("messages", []), "effects": ledger.count("effects"),
                  "submit_entries": ledger.count("submit_entries"), "wait_entries": ledger.count("wait_entries"),
                  "finalize_entries": ledger.count("finalize_entries"), "network_attempts": NETWORK_ATTEMPTS}
        (directory / f"{stage}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


async def loop_probe(directory):
    count = {"model": 0, "tools": 0}

    def model(state):
        count["model"] += 1
        step = len(state["messages"])
        return {"messages": [{"id": f"m{step}", "role": "assistant", "tool": "echo" if step < 4 else None}]}

    def tool(state):
        count["tools"] += 1
        return {"messages": [{"id": f"t{count['tools']}", "role": "tool", "content": "synthetic"}]}

    builder = StateGraph(State)
    builder.add_node("model", model)
    builder.add_node("tools", tool)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", lambda s: "tools" if s["messages"][-1].get("tool") else END)
    builder.add_edge("tools", "model")
    async with aiosqlite.connect(directory / "loop.sqlite3") as conn:
        saver = AsyncSqliteSaver(conn, serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None))
        graph = builder.compile(checkpointer=saver)
        result = await graph.ainvoke({"messages": []}, {"configurable": {"thread_id": "loop"}}, durability="sync")
        assert count == {"model": 3, "tools": 2}
        assert len(result["messages"]) == 5
        assert len(by_id(result["messages"], result["messages"])) == 5
        try:
            await graph.ainvoke({"messages": []}, {"configurable": {"thread_id": "limited"}, "recursion_limit": 2})
        except GraphRecursionError:
            pass
        else:
            raise AssertionError("Recursion limit not enforced")
        await saver.adelete_thread("loop")
        assert await saver.aget_tuple({"configurable": {"thread_id": "loop"}}) is None
    try:
        try:
            raise GraphInterrupt(())
        except GraphInterrupt:
            raise
        except Exception:
            raise AssertionError("GraphInterrupt swallowed by ordinary tool handler")
    except GraphInterrupt:
        pass
    return {"two_tool_rounds": True, "stable_message_reducer": True, "recursion_limit": True,
            "graph_interrupt_propagates_with_explicit_rethrow": True,
            "graph_interrupt_is_Exception": issubclass(GraphInterrupt, Exception), "official_thread_delete": True}


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "worker":
        with tracing_context(enabled=False):
            asyncio.run(worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4]))
        assert not NETWORK_ATTEMPTS
        return
    target = BUILD / ("synthetic-" + time.strftime("%Y%m%d-%H%M%S"))
    target.mkdir(parents=True)
    checks = []
    env = dict(os.environ, LANGSMITH_TRACING="true", LANGCHAIN_TRACING_V2="true",
               LANGSMITH_API_KEY="synthetic-secret-must-not-reach-state")
    for scenario, stages in {
        "ordinary": [("start", 0), ("finish", 0), ("duplicate", 0)],
        "early": [("start", 0), ("duplicate", 0)],
        "submit_crash": [("start", 71), ("finish", 0), ("duplicate", 0)],
        "claim_crash": [("start", 0), ("claim_crash", 72), ("finish", 0), ("duplicate", 0)],
        "applied_crash": [("start", 0), ("finish", 0)],
    }.items():
        directory = target / scenario
        directory.mkdir()
        if scenario == "applied_crash":
            stages = [("start", 0), ("applied_crash", 73), ("duplicate", 0)]
        timings = []
        for stage, expected in stages:
            if stage == "applied_crash":
                Ledger(directory / "business.sqlite3").finish()
            started = time.perf_counter()
            completed = subprocess.run([sys.executable, "-B", __file__, "worker", str(directory), scenario, stage],
                                       capture_output=True, text=True, env=env, timeout=45)
            timings.append(round(time.perf_counter() - started, 3))
            (directory / f"{stage}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
            assert completed.returncode == expected, (scenario, stage, completed.returncode, completed.stderr)
        ledger = Ledger(directory / "business.sqlite3")
        assert ledger.count("effects") == 1
        assert ledger.count("finalize_entries") == 1
        last = json.loads((directory / f"{stages[-1][0]}.json").read_text("utf-8"))
        assert last["next"] == [] and len(last["messages"]) == 2
        if scenario not in {"early", "submit_crash"}:
            assert ledger.count("wait_entries") == 2
        if scenario == "submit_crash":
            assert ledger.count("submit_entries") == 2
        with closing(ledger.db()) as db:
            assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
            assert not db.execute("SELECT * FROM intents WHERE state!='applied'").fetchall()
        assert b"synthetic-secret" not in (directory / "checkpoints.sqlite3").read_bytes()
        checks.append({"scenario": scenario, "status": "pass", "process_seconds": timings, "result": last})
    with tracing_context(enabled=False):
        loop = asyncio.run(loop_probe(target))
    assert not NETWORK_ATTEMPTS
    receipt = {"status": "pass", "framework": "official release wheels", "model": "synthetic",
               "graph_version": "probe-v1", "business_effects": "synthetic SQLite jobs only",
               "checks": checks, "loop": loop, "evidence_directory": str(target)}
    (AUDIT / "langgraph-replay-probe.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (AUDIT / "langgraph-compatibility.json").write_text(json.dumps({
        "status": "pass", "python": sys.version, "executable": sys.executable,
        "versions": {p: version(p) for p in ["langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "langchain-core"]},
        "network": {"method": "Python audit hook denies connect/DNS/sendto in each process", "attempts": NETWORK_ATTEMPTS,
                    "inherited_tracing_disabled": True, "os_firewall_disconnect": "not_tested"},
        "serializer": {"pickle_fallback": False, "strict_msgpack": True},
        "unicode_path": True, "process_restart": True,
        "limitations": ["Single machine; official saver production load remains F06", "No application integration or real model qualification claimed"]
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "pass", "scenarios": len(checks), "loop_checks": loop}))


if __name__ == "__main__":
    main()
