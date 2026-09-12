"""Persistent CPU task snapshots and atomic, idempotent review decisions."""

from copy import deepcopy
import json

from ocr_workbench.fusion import ENGINES, TERMINAL, validate_sources, fuse
from ocr_workbench.fusion_alignment import canonical_edit, fingerprint
from ocr_workbench.review_issues import apply_choice, comparable, current_target, reconcile, target_value


class FusionStoreMixin:
    def enqueue_fusion(self, project_id, result_ids, policy, request_id, expected_engines=None):
        from ocr_workbench.store import encoded, history_encoded, uid, now
        if not isinstance(result_ids, list) or not result_ids or len(result_ids) > 16 or any(not isinstance(k, str) for k in result_ids):
            raise ValueError("请选择 1–16 份原始结果")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 120:
            raise ValueError("缺少有效请求标识")
        self.one("projects", project_id)
        signature = fingerprint({"results": sorted(set(result_ids)), "policy": policy, "engines": expected_engines})
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = self._fusion_submission(db, project_id, request_id, signature)
            if prior is not None:
                return prior
            sources, parent_ids = [], []
            for result_id in dict.fromkeys(result_ids):
                row = db.execute("SELECT r.original,t.* FROM results r JOIN tasks t ON t.id=r.task_id WHERE r.id=?", (result_id,)).fetchone()
                if not row or row["project_id"] != project_id or row["kind"] != "ocr" or row["status"] != "succeeded":
                    raise ValueError("来源不是当前项目中已完成的原始 OCR 结果")
                original = json.loads(row["original"])
                if original.get("project_image_version", row["version_id"]) != row["version_id"]:
                    raise ValueError("来源输出与任务图像版本不匹配")
                sources.append(self._fusion_source(dict(row), result_id, original))
                parent_ids.append(row["id"])
            validate_sources(sources)
            stamp = sources[0]
            peers = db.execute("SELECT * FROM tasks WHERE project_id=? AND image_id=? AND batch=? AND kind='ocr'",
                               (project_id, stamp["image_id"], stamp["batch"])).fetchall()
            requested = set(expected_engines if expected_engines is not None else [p["engine"] for p in peers])
            if not requested <= set(ENGINES) or not {s["engine"] for s in sources} <= requested:
                raise ValueError("预期来源与所选引擎不一致")
            # The batch's failures/cancellations remain expected evidence. A
            # pending selected source cannot be converted to an abstention.
            supplied = {s["engine"] for s in sources}
            for engine in sorted(requested-supplied):
                matches = [p for p in peers if p["engine"] == engine]
                if len(matches) != 1:
                    raise ValueError("预期来源关系不明确，请重新运行完整融合批次")
                parent = matches[0]
                if parent["status"] not in TERMINAL:
                    raise ValueError("所选来源尚未全部进入终态，请等待或取消后再融合")
                if parent["status"] == "succeeded":
                    raise ValueError("缺少已成功引擎的原始 result ID")
                entry = self._fusion_source(dict(parent), None, None)
                entry["version_id"] = stamp["version_id"]
                entry["version_basis"] = "failed batch peer with identical input and preprocessing"
                if parent["input_version_id"] != peers[0]["input_version_id"] or parent["preprocess"] != peers[0]["preprocess"]:
                    raise ValueError("失败来源处理参数不兼容")
                sources.append(entry)
                parent_ids.append(parent["id"])
            sources = validate_sources(sources)
            key = uid()
            config = {"policy": policy, "expected_engines": sorted(requested), "snapshot_ready": True}
            db.execute("INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,kind,input_version_id,fusion_config) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (key, project_id, stamp["image_id"], stamp["version_id"], "fusion", stamp["batch"], "queued", "等待融合", now(), "fusion", stamp["version_id"], encoded(config)))
            db.execute("INSERT INTO fusion_inputs VALUES(?,?,?)", (key, history_encoded(sources), fingerprint(sources)))
            db.executemany("INSERT INTO fusion_dependencies(task_id,parent_task_id) VALUES(?,?)", [(key, parent) for parent in parent_ids])
            db.execute("INSERT INTO fusion_submissions VALUES(?,?,?,?,?)", (project_id, request_id, signature, encoded([key]), now()))
            return [key]

    @staticmethod
    def _fusion_submission(db, project_id, request_id, signature):
        from ocr_workbench.store import Conflict
        row = db.execute("SELECT * FROM fusion_submissions WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        if row:
            if row["payload_hash"] != signature:
                raise Conflict("同一请求标识不能用于不同融合输入")
            return json.loads(row["task_ids"])
        return None

    @staticmethod
    def _fusion_source(task, result_id, original):
        return {"task_id": task["id"], "result_id": result_id, "engine": task["engine"],
                "image_id": task["image_id"], "version_id": task["version_id"], "batch": task["batch"],
                "status": task["status"], "error": task.get("error"),
                "engine_package": task.get("engine_package"), "preprocess": task.get("preprocess"),
                "input_version_id": task.get("input_version_id"), "fingerprint": fingerprint(original),
                **({"original": original} if original is not None else {})}

    def attach_batch_fusion(self, db, project_id, task_ids, policy):
        """Called in the same transaction as OCR batch creation."""
        from ocr_workbench.store import encoded, uid, now
        grouped = {}
        for key in task_ids:
            row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (key,)).fetchone())
            grouped.setdefault(row["image_id"], []).append(row)
        output = []
        for image_id, parents in grouped.items():
            first, key = parents[0], uid()
            config = {"policy": policy, "expected_engines": [p["engine"] for p in parents],
                      "snapshot_ready": False, "input_version_id": first["version_id"],
                      "preprocess": first["preprocess"],
                      "parents": [{k: p[k] for k in ("id", "engine", "engine_package", "version_id", "preprocess", "batch")} for p in parents]}
            db.execute("INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,kind,input_version_id,fusion_config) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (key, project_id, image_id, first["version_id"], "fusion", first["batch"], "queued", "等待来源任务", now(), "fusion", first["version_id"], encoded(config)))
            db.executemany("INSERT INTO fusion_dependencies(task_id,parent_task_id) VALUES(?,?)", [(key, p["id"]) for p in parents])
            output.append(key)
        return output

    def fusion_sources(self, task_id):
        from ocr_workbench.store import encoded, history_encoded, history_decoded
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT * FROM fusion_inputs WHERE task_id=?", (task_id,)).fetchone()
            if saved:
                sources = json.loads(history_decoded(saved["snapshot"]))
                if fingerprint(sources) != saved["fingerprint"]:
                    raise ValueError("融合来源快照损坏")
                return sources
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["kind"] != "fusion":
                raise ValueError("不是融合任务")
            config = json.loads(task["fusion_config"])
            parents = db.execute("SELECT p.* FROM fusion_dependencies d JOIN tasks p ON p.id=d.parent_task_id WHERE d.task_id=? ORDER BY p.engine", (task_id,)).fetchall()
            if not parents or any(p["status"] not in TERMINAL for p in parents):
                raise ValueError("融合依赖尚未全部结束")
            sources = []
            for parent in parents:
                original = None
                if parent["result_id"] and parent["status"] == "succeeded":
                    original = json.loads(db.execute("SELECT original FROM results WHERE id=?", (parent["result_id"],)).fetchone()[0])
                sources.append(self._fusion_source(dict(parent), parent["result_id"], original))
            versions = {s["version_id"] for s in sources if s["status"] == "succeeded"}
            if len(versions) > 1:
                raise ValueError("同批次成功来源的实际处理版本不一致")
            version = next(iter(versions), task["version_id"])
            for source in sources:
                if source["input_version_id"] != config["input_version_id"] or source["preprocess"] != config["preprocess"]:
                    raise ValueError("融合任务依赖的输入参数发生变化")
                if source["status"] != "succeeded":
                    source["version_id"] = version
                    source["version_basis"] = "failed/cancelled dependency; same frozen input and preprocessing"
            validate_sources(sources)
            db.execute("INSERT INTO fusion_inputs VALUES(?,?,?)", (task_id, history_encoded(sources), fingerprint(sources)))
            config["snapshot_ready"] = True
            db.execute("UPDATE tasks SET version_id=?,fusion_config=? WHERE id=?", (version, encoded(config), task_id))
            return sources

    def run_fusion(self, task, cancelled=lambda: False):
        sources = self.fusion_sources(task["id"])
        policy = json.loads(task["fusion_config"])["policy"]
        data = fuse(sources, policy, task["id"], cancelled=cancelled)
        return self.complete(task["id"], data)

    @staticmethod
    def persist_fusion_issues(db, result_id, units, edit):
        from ocr_workbench.store import encoded, now
        for ordinal, unit in enumerate(units):
            if not unit["needs_review"]:
                continue
            db.execute("INSERT INTO fusion_issues(id,result_id,ordinal,category,state,definition,target,basis,current_value,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (unit["id"], result_id, ordinal, unit["category"], "pending", encoded(unit), encoded(unit["target"]), unit["basis"], encoded(target_value(edit, unit["target"])[0]), now()))

    def review_issues(self, result_id, state=None, category=None, offset=0, limit=50, resume=False):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("无效疑点分页")
        if state not in {None, "pending", "resolved", "question", "stale"} or category not in {None, "structure", "amount", "date", "identifier", "number", "empty", "text"}:
            raise ValueError("未知疑点筛选条件")
        with self.transaction() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
            if not row:
                raise KeyError("识别结果不存在")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            target = self._review_target(db, task["image_id"])
            context_current = target["result_id"] == result_id and target["version_id"] == target["active_version"]
            counts = {name: 0 for name in ("pending", "resolved", "question", "stale")}
            for item in db.execute("SELECT state,COUNT(*) n FROM fusion_issues WHERE result_id=? GROUP BY state", (result_id,)):
                counts[item["state"]] = item["n"]
            clauses, params = ["result_id=?"], [result_id]
            if state: clauses.append("state=?"); params.append(state)
            if category: clauses.append("category=?"); params.append(category)
            where = " AND ".join(clauses)
            total = db.execute("SELECT COUNT(*) FROM fusion_issues WHERE " + where, params).fetchone()[0]
            progress = db.execute("SELECT issue_id FROM fusion_progress WHERE result_id=?", (result_id,)).fetchone()
            if resume and progress:
                position = db.execute("SELECT ordinal FROM fusion_issues WHERE " + where + " AND id=?", [*params, progress[0]]).fetchone()
                if position:
                    offset = db.execute("SELECT COUNT(*) FROM fusion_issues WHERE " + where + " AND ordinal<?", [*params, position[0]]).fetchone()[0]
            rows = db.execute("SELECT * FROM fusion_issues WHERE " + where + " ORDER BY ordinal,id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            issues = []
            edited, original = json.loads(row["edited"]), json.loads(row["original"])
            for issue in rows:
                item = json.loads(issue["definition"])
                resolved_target = current_target(json.loads(issue["target"]), item, original)
                value, _ = target_value(edited, resolved_target)
                item.update(target=resolved_target, current_value=value,
                            state=issue["state"], decision_id=issue["decision_id"], context_current=context_current,
                            current_fingerprint=fingerprint(comparable(value)))
                if not context_current:
                    item["location"] = {**item["location"], "level": "image", "polygon": None, "reason": "当前采用结果或图像版本不匹配"}
                issues.append(item)
            return {"result_id": result_id, "revision": row["revision"], "counts": counts, "total": total,
                    "offset": offset, "limit": limit, "issues": issues, "context_current": context_current,
                    "position": progress[0] if progress else None}

    def set_fusion_position(self, result_id, issue_id):
        from ocr_workbench.store import now
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM fusion_issues WHERE id=? AND result_id=?", (issue_id, result_id)).fetchone():
                raise ValueError("疑点不属于当前结果")
            db.execute("INSERT INTO fusion_progress VALUES(?,?,?) ON CONFLICT(result_id) DO UPDATE SET issue_id=excluded.issue_id,updated=excluded.updated",
                       (result_id, issue_id, now()))
        return {"saved": True}

    def decide_issue(self, result_id, issue_id, body):
        from ocr_workbench.store import Conflict, encoded, history_encoded, history_decoded, now
        request_id, action = body.get("request_id"), body.get("action")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 120:
            raise ValueError("缺少决策请求标识")
        if action not in {"candidate", "keep", "manual", "question"}:
            raise ValueError("未知校对操作")
        signature = fingerprint({"issue_id": issue_id, **body})
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM fusion_decisions WHERE result_id=? AND request_id=?", (result_id, request_id)).fetchone()
            if prior:
                if prior["payload_hash"] != signature:
                    raise Conflict("相同请求标识不能重复提交不同决策")
                return json.loads(history_decoded(prior["response"]))
            row = db.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
            issue = db.execute("SELECT * FROM fusion_issues WHERE id=? AND result_id=?", (issue_id, result_id)).fetchone()
            if not row or not issue:
                raise KeyError("疑点或结果不存在")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            selected = self._review_target(db, task["image_id"])
            if row["revision"] != body.get("revision") or selected["result_id"] != result_id or selected["active_version"] != body.get("version_id") or selected["version_id"] != body.get("version_id"):
                raise Conflict("采用结果、图像版本或修订已改变，草稿与当前位置已保留")
            if issue["basis"] != body.get("basis"):
                raise Conflict("候选依据已经变化，请重新读取疑点")
            before, target = json.loads(row["edited"]), json.loads(issue["target"])
            target = current_target(target, json.loads(issue["definition"]), json.loads(row["original"]))
            current_value, valid = target_value(before, target)
            if fingerprint(comparable(current_value)) != body.get("current_fingerprint"):
                raise Conflict("疑点当前内容已变化，请重新读取后确认")
            after = before
            if action in {"candidate", "manual"}:
                if issue["state"] == "stale" or not valid:
                    raise Conflict("疑点位置或结构已过期，请先在普通编辑器复核并保留当前")
                if action == "candidate":
                    definition = json.loads(issue["definition"])
                    matches = [c for c in definition["candidates"] if c["id"] == body.get("candidate_id")]
                    if len(matches) != 1:
                        raise Conflict("候选不属于当前疑点")
                    value = matches[0]["value"]
                else:
                    value = body.get("value")
                after = apply_choice(before, target, value, body.get("placement"))
            cursor = row["cursor"] + 1
            db.execute("UPDATE fusion_issues SET target=? WHERE id=?", (encoded(target), issue_id))
            # Even a keep/question decision is a revision-bound history event;
            # undo/redo invalidates decisions rather than restoring old review.
            db.execute("DELETE FROM edits WHERE result_id=? AND position>?", (result_id, row["cursor"]))
            db.execute("INSERT INTO edits VALUES(?,?,?,?)", (result_id, cursor, history_encoded(after), now()))
            db.execute("UPDATE results SET edited=?,cursor=?,revision=revision+1,updated=? WHERE id=?", (encoded(after), cursor, now(), result_id))
            reconcile(db, result_id, before, after, deciding=issue_id)
            state = "question" if action == "question" else "resolved"
            changed = db.execute("SELECT target FROM fusion_issues WHERE id=?", (issue_id,)).fetchone()
            actual, _ = target_value(after, json.loads(changed[0]))
            db.execute("UPDATE fusion_issues SET state=?,decision_id=?,current_value=?,updated=? WHERE id=?",
                       (state, request_id, encoded(actual), now(), issue_id))
            db.execute("INSERT INTO fusion_progress VALUES(?,?,?) ON CONFLICT(result_id) DO UPDATE SET issue_id=excluded.issue_id,updated=excluded.updated", (result_id, issue_id, now()))
            response = {"id": result_id, "task_id": row["task_id"], "revision": row["revision"]+1,
                        "cursor": cursor, "can_undo": True, "can_redo": False, "edited": after,
                        "original": json.loads(row["original"]), "review_decision": {"issue_id": issue_id, "state": state, "request_id": request_id}}
            db.execute("INSERT INTO fusion_decisions VALUES(?,?,?,?,?,?,?,?)",
                       (result_id, request_id, issue_id, signature, action, history_encoded(response), encoded({"target": target, "value": current_value, "state": issue["state"], "revision": row["revision"]}), now()))
            return response
