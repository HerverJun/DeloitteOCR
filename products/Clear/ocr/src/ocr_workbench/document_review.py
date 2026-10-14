"""Document-level review queue over the actual adopted page revisions."""
import json

from ocr_workbench.geometry_contract import fingerprint


PRIORITIES = {"detection": 0, "structure": 1, "amount": 2, "date": 2,
              "identifier": 2, "number": 2, "document_conflict": 2, "text": 3, "geometry": 4}


def document_review_queue(store, document_id, *, offset=0, limit=50, state="open"):
    if offset < 0 or not 1 <= limit <= 100 or state not in ("open", "deferred", "all"):
        raise ValueError("复核队列分页或状态无效")
    with store.transaction() as db:
        db.execute("BEGIN")
        document = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if document is None:
            raise KeyError("文档不存在")
        pages = db.execute("""SELECT p.*,i.active_version,r.id result_id,r.revision,r.edited,r.original,
                COALESCE(json_extract(r.original,'$.project_image_version'),t.version_id) result_version
            FROM pages p LEFT JOIN images i ON i.id=p.image_id LEFT JOIN selections s ON s.image_id=p.image_id
            LEFT JOIN results r ON r.id=s.result_id LEFT JOIN tasks t ON t.id=r.task_id
            WHERE p.document_id=? ORDER BY p.page_number""", (document_id,)).fetchall()
        tasks, summary = [], {"pages": len(pages), "unprocessed_pages": 0, "checked_pages": 0, "candidate_pages": 0}
        for page in pages:
            base = {"page_id": page["id"], "page_number": page["page_number"], "image_id": page["image_id"],
                    "result_id": page["result_id"], "revision": page["revision"], "version_id": page["active_version"]}
            def add(kind, target, **details):
                category = details.pop("category", "structure")
                # Individual suggestions/jobs keep their identity even when a
                # text target is rebased; structure alternatives remain grouped.
                identity = next(({key: details[key]} for key in ('proposal_id', 'task_id', 'issue_id') if key in details),
                                {'target': target})
                tasks.append({**base, "id": fingerprint({"page": page["id"], "result": page["result_id"], "kind": kind, **identity}),
                    "kind": kind, "target": target, "category": category, "priority": PRIORITIES.get(category, 3),
                    "state": "pending", **details})
            if not page["result_id"] or page["result_version"] != page["active_version"]:
                summary["unprocessed_pages"] += 1
                stage = db.execute("SELECT status,phase,error FROM document_stages WHERE page_id=? ORDER BY created DESC LIMIT 1", (page["id"],)).fetchone()
                add("page_processing", {"kind": "page"}, category="detection", reason="页面尚无当前版本的采用结果",
                    stage=dict(stage) if stage else None)
                continue
            edit, raw = json.loads(page["edited"]), json.loads(page["original"])
            from ocr_workbench.structure_store import structure_snapshot
            structure = structure_snapshot(db, {'id': page['result_id'], 'original': raw,
                'version_id': page['active_version'], 'revision': page['revision'], 'selected': page['result_id']})
            tool = structure['table_tool']
            if state != 'deferred' and tool['state'] not in ('ready', 'empty', 'not_applicable'):
                add('table_tool', {'kind': 'page'}, reason=tool['message'])
            group = {}
            for row in structure['proposals']:
                if row["state"] not in ("pending", "deferred") and state != "all":
                    continue
                if state == "deferred" and row["state"] != "deferred":
                    continue
                scope = tuple(row["table_indices"])
                group.setdefault(scope, []).append({"id": row["id"], "state": row["state"], "kind": row["kind"],
                    "provider": row["provider"], "reason": row["reason"], "can_apply": row["can_apply"],
                    "conflicts": len(row.get("conflicts", [])), "priority": row["priority"]})
            covered_tables = set()
            for scope, alternatives in group.items():
                if any(p['state'] in ('pending', 'deferred') for p in alternatives):
                    covered_tables.update(scope)
                add("structure", {"kind": "tables", "tables": list(scope)}, category="detection" if any(p["priority"] == 0 for p in alternatives) else "structure",
                    reason="表身份待核对" if any(p["priority"] == 0 for p in alternatives) else "结构差异待核对",
                    proposal_ids=[p["id"] for p in alternatives], alternatives=alternatives,
                    state=("pending" if any(p["state"] == "pending" for p in alternatives) else
                           "deferred" if any(p["state"] == "deferred" for p in alternatives) else "resolved"))
            candidates = structure['candidate_rows']
            if candidates:
                summary["candidate_pages"] += 1
            if structure['checked']:
                summary["checked_pages"] += 1
            elif candidates and state != "deferred":
                add("structure_check", {"kind": "page"}, reason="候选已就绪，检查当前修订")
            # Fold cell-level fusion alerts into their primary structural task.
            table_ids = {t.get("fusion_id"): i for i,t in enumerate(edit["tables"])}
            for issue in db.execute("SELECT id,category,state,target,definition FROM fusion_issues WHERE result_id=? ORDER BY ordinal", (page["result_id"],)):
                if issue["state"] == "resolved" and state != "all" or state == "deferred" and issue["state"] != "question":
                    continue
                target = json.loads(issue["target"])
                if table_ids.get(target.get("table_id")) in covered_tables:
                    continue
                add("fusion", target, category=issue["category"], issue_id=issue["id"], state=issue["state"],
                    reason=json.loads(issue["definition"]).get("reason", "来源内容有分歧"))
            if state != "deferred":
                decisions = {d["conflict_id"]:d["edited_sha256"] for d in db.execute("SELECT * FROM document_conflict_decisions WHERE result_id=?", (page["result_id"],))}
                for conflict in raw.get("document", {}).get("conflicts", []):
                    if decisions.get(conflict["id"]) != fingerprint(edit):
                        add("document_conflict", {"kind": "document_conflict", "id": conflict["id"]}, category="document_conflict",
                            reason="区域未识别出文字，需核对原图" if conflict.get('reason')=='region_no_text' else "原生文字与识别文字重叠")
            for proposal in db.execute("SELECT * FROM multimodal_proposals WHERE result_id=? ORDER BY created,id", (page["result_id"],)):
                if state == "deferred" and proposal["state"] != "question":
                    continue
                if state != "all" and (proposal["state"] not in ("pending", "question") or proposal["revision"] != page["revision"]):
                    continue
                add("multimodal", json.loads(proposal["current_target"]), category="text", proposal_id=proposal["id"],
                    state=proposal["state"], reason="视觉审校：" + proposal["reason"],
                    location=json.loads(proposal["evidence"]))
            if state != "deferred":
                for pending in db.execute("""SELECT t.id,t.status,t.phase,t.error FROM tasks t
                    JOIN multimodal_requests mr ON mr.task_id=t.id
                    WHERE mr.result_id=? AND mr.obsolete=0 AND t.status IN ('queued','running','failed','interrupted','paused')
                    ORDER BY t.created,t.id""", (page["result_id"],)):
                    add("multimodal_task", {"kind": "page"}, category="text", task_id=pending["id"],
                        reason=pending["error"] or pending["phase"], state=pending["status"])
        tasks.sort(key=lambda t: (t["priority"], t["state"] in ("deferred", "question"), t["page_number"], t["id"]))
        counts = {}
        for task in tasks:
            counts[task["category"]] = counts.get(task["category"], 0)+1
        timings = db.execute("""SELECT COUNT(*) n,COALESCE(SUM(active_ms),0) active_ms FROM review_timings rt
            JOIN results r ON r.id=rt.result_id JOIN tasks t ON t.id=r.task_id JOIN pages p ON p.image_id=t.image_id
            WHERE p.document_id=?""", (document_id,)).fetchone()
        return {"document_id": document_id, "total": len(tasks), "offset": offset, "limit": limit,
                "tasks": tasks[offset:offset+limit], "counts": counts, "summary": summary,
                "timing": {"events": timings["n"], "active_ms": timings["active_ms"], "human_efficiency_claim": False},
                "empty_means_correct": False}


def refresh_document_review(store, document_id):
    from ocr_workbench.structure_store import refresh_proposals
    store.one("documents", document_id)
    rows = store.rows("""SELECT r.id,r.revision FROM pages p JOIN selections s ON s.image_id=p.image_id
        JOIN results r ON r.id=s.result_id WHERE p.document_id=? ORDER BY p.page_number""", (document_id,))
    receipts = []
    for row in rows:
        try:
            view = refresh_proposals(store, row["id"], row["revision"])
            receipts.append({"result_id": row["id"], "status": "checked", "proposals": len(view["proposals"])})
        except (ValueError, KeyError) as error:
            receipts.append({"result_id": row["id"], "status": "failed", "error": str(error)})
    return {"receipts": receipts, "queue": document_review_queue(store, document_id)}
