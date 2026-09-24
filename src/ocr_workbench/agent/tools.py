"""Register available application tools; remaining capabilities stay explicit."""
import asyncio

from .context import AgentContext
from .contracts import ToolResult


def register_read_tools(registry, services, capabilities):
    views = AgentContext(registry.policy.agent, services)

    async def workspace(args, context):
        return await asyncio.to_thread(views.workspace, context["run"], args, capabilities())

    async def search(args, context):
        return await asyncio.to_thread(views.search, context["run"], args)

    async def read(args, context):
        return await asyncio.to_thread(views.read_page, context["run"], args)

    async def navigate(args, context):
        return await asyncio.to_thread(views.navigate, context["run"], args)

    async def ask(args, context):
        run = context["run"]
        payload = {"question": args.question, "options": [{"id": f"option-{i}", "label": value} for i, value in enumerate(args.options)], "evidence_ref_ids": args.evidence_ref_ids}
        decision = registry.policy.agent.create_decision(run["project_id"], run["id"], context["generation"], payload)
        return ToolResult(status="needs_user", summary=args.question, data={"decision_id": decision["id"]}).model_dump(mode="json")

    for name, handler in (("get_workspace_context", workspace), ("search_document", search), ("read_page_result", read), ("navigate_to_evidence", navigate), ("ask_user", ask)):
        registry.register(name, handler)
    return views


def register_processing_tools(registry, services, jobs):
    from .operations import Operations
    from .policy import PolicyDenied
    operations = Operations(registry.policy.agent, registry.policy)
    store = services.store

    def process(args, context):
        parameters = services.documents.processing_parameters(args.mode, args.engine or "ppocr")
        def effect(db, operation_id):
            rows = db.execute("SELECT id,page_number FROM pages WHERE document_id=? ORDER BY page_number", (args.document_id,)).fetchall()
            selected = [r["id"] for r in rows if r["page_number"] in args.page_numbers]
            prior = {r[0] for page in selected for r in db.execute("SELECT id FROM document_stages WHERE page_id=?", (page,))}
            ids = store._enqueue_document_stages(db, selected, "process", parameters, force=args.force)
            links = [{"kind": "pdf_stage", "job_id": key, "ownership": "reused" if key in prior else "created",
                      "state": db.execute("SELECT status FROM document_stages WHERE id=?", (key,)).fetchone()[0]} for key in ids]
            return {"stage_ids": ids}, links
        def fingerprint(db):
            return {"document_sha256": db.execute("SELECT sha256 FROM documents WHERE id=?", (args.document_id,)).fetchone()[0], "parameters": parameters}
        result = operations.submit(context, "process_pages", args.model_dump(exclude_none=True), effect, fingerprint=fingerprint,
                                   cost={"pages": len(args.page_numbers), "jobs": len(args.page_numbers)})
        services.documents.wake.set()
        return jobs.operation_result(context["run"]["project_id"], context["run"]["id"], result["operation_id"])

    def ocr(args, context):
        services.require_recognition()
        packages = {name: spec["package_id"] for name, spec in services.registry.engines().items()}
        fusion = None
        if args.fusion_config_id is not None:
            from ocr_workbench.fusion import load_policy
            if args.fusion_config_id not in {"table:conservative", "table:aggressive", "print:conservative", "print:aggressive", "handwriting:conservative", "handwriting:aggressive"}:
                raise PolicyDenied("unsupported_capability", "未知融合配置；请使用工作台列出的配置")
            kind, mode = args.fusion_config_id.split(":")
            fusion = load_policy(services.bundle, kind, mode)
        def effect(db, operation_id):
            ids = store._enqueue(db, context["run"]["project_id"], args.version_ids, args.engines,
                                 engine_packages=packages, fusion_policy=fusion, request_id=operation_id)
            links = [{"kind": "fusion" if db.execute("SELECT kind FROM tasks WHERE id=?", (key,)).fetchone()[0] == "fusion" else "ocr",
                      "job_id": key, "ownership": "created", "state": "queued"} for key in ids]
            return {"task_ids": ids}, links
        def fingerprint(db):
            return {"versions": [dict(db.execute("SELECT id,sha256 FROM versions WHERE id=?", (key,)).fetchone()) for key in sorted(args.version_ids)], "packages": packages, "fusion": fusion}
        result = operations.submit(context, "run_ocr", args.model_dump(exclude_none=True), effect, fingerprint=fingerprint,
                                   cost={"pages": len(args.version_ids), "jobs": len(args.version_ids) * (len(args.engines) + bool(fusion))})
        services.queue.wake.set()
        services.fusion_queue.wake.set()
        return jobs.operation_result(context["run"]["project_id"], context["run"]["id"], result["operation_id"])

    async def process_async(args, context):
        return await asyncio.to_thread(process, args, context)

    async def ocr_async(args, context):
        return await asyncio.to_thread(ocr, args, context)

    async def status(args, context):
        run = context["run"]
        operations_seen = set()
        summaries = []
        links = []
        for job in args.jobs:
            rows = store.rows("""SELECT o.id,o.run_id FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
                JOIN agent_runs r ON r.id=o.run_id WHERE j.job_kind=? AND j.job_id=? AND r.session_id=? AND o.project_id=?""",
                (job.kind, job.job_id, run["session_id"], run["project_id"]))
            for row in rows:
                if row["id"] in operations_seen:
                    continue
                operations_seen.add(row["id"])
                result = jobs.operation_result(run["project_id"], row["run_id"], row["id"])
                summaries.append({"operation_id": row["id"], "summary": result["summary"]})
                links += [link for link in result["job_refs"] if any(j.job_id == link["job_id"] and j.kind == link["kind"] for j in args.jobs)]
        # This is a query; returning non-terminal job_refs would trigger graph waiting.
        return ToolResult(status="success", summary="已查询关联任务的实际状态", data={"operations": summaries[:20], "jobs": links[:20]}, truncated=len(summaries) > 20 or len(links) > 20).model_dump(mode="json")

    registry.register("process_pages", process_async)
    registry.register("run_ocr", ocr_async)
    registry.register("get_job_status", status)
    return operations
