"""Immutable model evidence, transactional proposals, and explicit human edits."""

from copy import deepcopy
import hashlib
import json

from ocr_workbench.geometry_contract import fingerprint
from ocr_workbench.multimodal_contract import (CONTRACT_VERSION, apply_literal, build_targets,
    rebase_target, target_value, validate_response)


def migrate_v11(db):
    for statement in (
        """CREATE TABLE multimodal_requests(task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,request_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,snapshot BLOB NOT NULL,obsolete INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',raw_response BLOB,created TEXT NOT NULL,UNIQUE(result_id,request_id))""",
        """CREATE TABLE multimodal_proposals(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES multimodal_requests(task_id) ON DELETE CASCADE,
            result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,target_id TEXT NOT NULL,
            target TEXT NOT NULL,current_target TEXT NOT NULL,before_value TEXT NOT NULL,after_value TEXT NOT NULL,
            current_value TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('keep','replace','uncertain')),
            state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected','question','stale')),
            reason TEXT NOT NULL,evidence TEXT NOT NULL,basis_revision INTEGER NOT NULL,revision INTEGER NOT NULL,
            edited_sha256 TEXT NOT NULL,rebase_history TEXT NOT NULL DEFAULT '[]',created TEXT NOT NULL,updated TEXT NOT NULL,
            UNIQUE(task_id,target_id))""",
        "CREATE INDEX multimodal_proposals_result ON multimodal_proposals(result_id,state,created)",
        """CREATE TABLE multimodal_decisions(result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL,proposal_id TEXT NOT NULL REFERENCES multimodal_proposals(id) ON DELETE CASCADE,
            payload_hash TEXT NOT NULL,action TEXT NOT NULL,response BLOB NOT NULL,created TEXT NOT NULL,
            PRIMARY KEY(result_id,request_id))""",
        """CREATE TRIGGER multimodal_result_changed AFTER UPDATE OF revision ON results WHEN OLD.revision != NEW.revision BEGIN
            UPDATE multimodal_requests SET obsolete=1 WHERE result_id=NEW.id;
            UPDATE multimodal_proposals SET state='stale' WHERE result_id=NEW.id;
            END""",
        """CREATE TRIGGER multimodal_version_changed AFTER UPDATE OF active_version ON images WHEN OLD.active_version IS NOT NEW.active_version BEGIN
            UPDATE multimodal_requests SET obsolete=1 WHERE result_id IN (SELECT r.id FROM results r JOIN tasks t ON t.id=r.task_id WHERE t.image_id=NEW.id);
            UPDATE multimodal_proposals SET state='stale' WHERE result_id IN (SELECT r.id FROM results r JOIN tasks t ON t.id=r.task_id WHERE t.image_id=NEW.id);
            END""",
        """CREATE TRIGGER multimodal_selection_changed AFTER UPDATE OF result_id ON selections WHEN OLD.result_id IS NOT NEW.result_id BEGIN
            UPDATE multimodal_requests SET obsolete=1 WHERE result_id=OLD.result_id OR result_id=NEW.result_id;
            UPDATE multimodal_proposals SET state='stale' WHERE result_id=OLD.result_id OR result_id=NEW.result_id;
            END""",
        """CREATE TRIGGER multimodal_selection_deleted AFTER DELETE ON selections BEGIN
            UPDATE multimodal_requests SET obsolete=1 WHERE result_id=OLD.result_id;
            UPDATE multimodal_proposals SET state='stale' WHERE result_id=OLD.result_id;
            END""",
        """CREATE TRIGGER multimodal_image_content_changed AFTER UPDATE OF sha256,path,width,height ON versions
            WHEN OLD.sha256 IS NOT NEW.sha256 OR OLD.path IS NOT NEW.path OR OLD.width IS NOT NEW.width OR OLD.height IS NOT NEW.height BEGIN
            UPDATE multimodal_requests SET obsolete=1 WHERE task_id IN (SELECT id FROM tasks WHERE kind='multimodal' AND version_id=NEW.id);
            UPDATE multimodal_proposals SET state='stale' WHERE task_id IN (SELECT id FROM tasks WHERE kind='multimodal' AND version_id=NEW.id);
            END""",
    ):
        db.execute(statement)


def _context(db, result_id):
    row = db.execute("""SELECT r.*,t.project_id,t.image_id,t.version_id task_version,i.active_version,s.result_id selected
        FROM results r JOIN tasks t ON t.id=r.task_id JOIN images i ON i.id=t.image_id
        LEFT JOIN selections s ON s.image_id=i.id WHERE r.id=?""", (result_id,)).fetchone()
    if row is None:
        raise KeyError("识别结果不存在")
    value = dict(row)
    value["original"], value["edited"] = json.loads(row["original"]), json.loads(row["edited"])
    value["version_id"] = value["original"].get("project_image_version") or row["task_version"]
    return value


def _request_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("请提供 1–128 字符的幂等请求编号")
    return value


def lookup_existing_review(store, result_id, body):
    """Read-only request recovery remains available without a working model."""
    from ocr_workbench.store import Conflict
    if not isinstance(body, dict):
        raise ValueError("审校请求格式无效")
    request_id = _request_id(body.get("request_id"))
    signature = fingerprint(body)
    with store.transaction() as db:
        row = db.execute("SELECT task_id,payload_hash FROM multimodal_requests WHERE result_id=? AND request_id=?",
                         (result_id, request_id)).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != signature:
            raise Conflict("请求编号已用于另一项审校")
        return row["task_id"]


def _file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(value):
    from ocr_workbench.store import history_decoded
    return json.loads(history_decoded(value))


def _assert_current(result, revision, version_id):
    from ocr_workbench.store import Conflict
    if (type(revision) is not int or result["revision"] != revision or result["selected"] != result["id"]
            or result["active_version"] != version_id or result["version_id"] != version_id):
        raise Conflict("采用结果、图像版本或校对内容已变化，请重新加载后审校")


def _verify_image(store, db, snapshot):
    from ocr_workbench.store import Conflict
    version = db.execute("SELECT * FROM versions WHERE id=?", (snapshot["version_id"],)).fetchone()
    if (not version or version["sha256"] != snapshot["image_sha256"] or version["path"] != snapshot["image_relative_path"]
            or version["width"] != snapshot["width"] or version["height"] != snapshot["height"]):
        raise Conflict("审校图像版本或内容已变化，请重新提交")
    path = store.file(version["path"])
    if not path.is_file() or _file_sha(path) != snapshot["image_sha256"]:
        raise Conflict("审校原图文件已变化或缺失，请重新导入")
    return path


def enqueue_review(store, result_id, body, config):
    from ocr_workbench.geometry import geometry_view
    from ocr_workbench.store import Conflict, encoded, history_encoded, now, uid
    if not isinstance(body, dict) or not isinstance(config, dict):
        raise ValueError("审校请求或模型配置无效")
    request_id = _request_id(body.get("request_id"))
    model = body.get("model_id")
    if not isinstance(model, str) or not 1 <= len(model) <= 200:
        raise ValueError("请选择已配置的视觉审校模型")
    signature = fingerprint(body)
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT task_id,payload_hash FROM multimodal_requests WHERE result_id=? AND request_id=?", (result_id, request_id)).fetchone()
        if prior:
            if prior["payload_hash"] != signature:
                raise Conflict("请求编号已用于另一项审校")
            return prior["task_id"]
        result = _context(db, result_id)
        _assert_current(result, body.get("revision"), body.get("version_id"))
        version = dict(db.execute("SELECT * FROM versions WHERE id=?", (result["version_id"],)).fetchone())
        geometry = geometry_view(store, result_id, db=db)["evidence"]
        targets = build_targets(result["edited"], result["original"], version, body.get("scope"), body.get("target"), geometry)
        task_id = uid()
        snapshot = {"contract_version": CONTRACT_VERSION, "task_id": task_id, "result_id": result_id,
            "image_id": result["image_id"], "revision": result["revision"], "selected_result_id": result["selected"],
            "edited_sha256": fingerprint(result["edited"]), "version_id": version["id"], "image_version": version["id"],
            "image_relative_path": version["path"], "image_path": str(store.file(version["path"])),
            "width": version["width"], "height": version["height"], "image_sha256": version["sha256"],
            "model_id": model, "config": deepcopy(config), "scope": body["scope"], "targets": targets}
        _verify_image(store, db, snapshot)
        db.execute("""INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,kind,input_version_id,engine_package)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (task_id,result["project_id"],result["image_id"],version["id"],"reviewer",
            now()+"-"+task_id,"queued","等待视觉审校",now(),"multimodal",version["id"],"builtin"))
        db.execute("INSERT INTO multimodal_requests(task_id,result_id,request_id,payload_hash,snapshot,created) VALUES(?,?,?,?,?,?)",
            (task_id,result_id,request_id,signature,history_encoded(snapshot),now()))
    return task_id


def _current_snapshot(store, db, row):
    from ocr_workbench.store import Conflict
    snapshot = _decode(row["snapshot"])
    if row["obsolete"]:
        raise Conflict("审校快照已过期，请重新提交")
    result = _context(db, row["result_id"])
    _assert_current(result, snapshot["revision"], snapshot["version_id"])
    if fingerprint(result["edited"]) != snapshot["edited_sha256"]:
        raise Conflict("校对内容已变化，请重新提交审校")
    _verify_image(store, db, snapshot)
    return snapshot


def prepare_review(store, task_id, *, require_running=True):
    from ocr_workbench.store import Conflict
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT q.*,t.status task_status FROM multimodal_requests q JOIN tasks t ON t.id=q.task_id WHERE q.task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("视觉审校任务不存在")
        if require_running and row["task_status"] != "running":
            raise Conflict("视觉审校任务已暂停或取消")
        return _current_snapshot(store, db, row)


def complete_review(store, task_id, response):
    from ocr_workbench.store import Conflict, encoded, history_encoded, now, uid
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT q.*,t.status task_status FROM multimodal_requests q JOIN tasks t ON t.id=q.task_id WHERE q.task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("视觉审校任务不存在")
        if row["task_status"] != "running":
            return False
        try:
            snapshot = _current_snapshot(store, db, row)
        except Conflict as error:
            db.execute("UPDATE tasks SET status='cancelled',phase='审校快照已过期',error=?,finished=? WHERE id=?", (str(error),now(),task_id))
            return False
        checked = validate_response(response, snapshot["targets"])
        serialized = json.dumps(response, ensure_ascii=False, allow_nan=False)
        if len(serialized.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("模型审校响应超过 4 MiB 保存上限")
        targets = {t["id"]: t for t in snapshot["targets"]}
        for item in checked["items"]:
            target = targets[item["target_id"]]
            db.execute("""INSERT INTO multimodal_proposals(id,task_id,result_id,target_id,target,current_target,before_value,after_value,current_value,
                status,state,reason,evidence,basis_revision,revision,edited_sha256,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid(),task_id,row["result_id"],item["target_id"],encoded(target["target"]),encoded(target["target"]),target["before"],item["after"],
                 target["before"],item["decision"],"pending",item["reason"],encoded(target["evidence"]),snapshot["revision"],snapshot["revision"],
                 snapshot["edited_sha256"],now(),now()))
        db.execute("UPDATE multimodal_requests SET summary=?,raw_response=? WHERE task_id=?", (checked["summary"],history_encoded(response),task_id))
        db.execute("UPDATE tasks SET status='succeeded',phase='审校建议已生成',finished=?,error=NULL WHERE id=? AND status='running'", (now(),task_id))
    return True


def reconcile_review(db, result_id):
    from ocr_workbench.store import now
    result = _context(db, result_id)
    digest = fingerprint(result["edited"])
    db.execute("""UPDATE multimodal_proposals SET state='stale',updated=? WHERE result_id=? AND state!='stale'
        AND (revision!=? OR edited_sha256!=? OR ? IS NOT ? OR ? IS NOT ?)""",
        (now(),result_id,result["revision"],digest,result["selected"],result_id,result["active_version"],result["version_id"]))


def _proposal(row):
    value = dict(row)
    value["original_target"], value["target"] = json.loads(value.pop("target")), json.loads(value.pop("current_target"))
    value["before"], value["after"] = value.pop("before_value"), value.pop("after_value")
    value["decision"] = value["status"]
    value["evidence"], value["rebase_history"] = json.loads(value["evidence"]), json.loads(value["rebase_history"])
    value["can_accept"] = value["state"] in ("pending", "question") and value["status"] != "uncertain"
    return value


def _request_view(row):
    value, snapshot = dict(row), _decode(row["snapshot"])
    value.pop("snapshot")
    value.pop("raw_response", None)
    value.update({k: snapshot[k] for k in ("model_id", "revision", "version_id", "scope", "image_sha256")})
    value["target_count"] = len(snapshot["targets"])
    value["snapshot_current"] = not bool(value["obsolete"])
    return value


def view_review(store, result_id):
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        result = _context(db, result_id)
        reconcile_review(db, result_id)
        proposals = [_proposal(r) for r in db.execute("SELECT * FROM multimodal_proposals WHERE result_id=? ORDER BY created,id", (result_id,))]
        requests = [_request_view(r) for r in db.execute("""SELECT q.*,t.status,t.phase,t.error,t.started,t.finished
            FROM multimodal_requests q JOIN tasks t ON t.id=q.task_id WHERE q.result_id=? ORDER BY q.created DESC,q.task_id""", (result_id,))]
        counts = {state: sum(p["state"] == state for p in proposals) for state in ("pending","accepted","rejected","question","stale")}
        return {"result_id": result_id, "revision": result["revision"], "version_id": result["version_id"], "requests": requests,
                "proposals": proposals, "counts": counts, "automatic_adoption": False, "contributes_to_votes": False}


def _saved(db, result_id):
    value = dict(db.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone())
    value["original"], value["edited"] = json.loads(value["original"]), json.loads(value["edited"])
    value["can_undo"] = value["cursor"] > 0
    value["can_redo"] = bool(db.execute("SELECT 1 FROM edits WHERE result_id=? AND position>?", (result_id,value["cursor"])).fetchone())
    return value


def decide_review(store, result_id, proposal_id, body):
    from ocr_workbench.store import Conflict, encoded, history_encoded, now
    if not isinstance(body, dict):
        raise ValueError("审校决定格式无效")
    request_id = _request_id(body.get("request_id"))
    action = body.get("action")
    if action not in ("accept", "reject", "question"):
        raise ValueError("未知审校决定")
    signature = fingerprint({"proposal_id": proposal_id, "body": body})
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT payload_hash,response FROM multimodal_decisions WHERE result_id=? AND request_id=?", (result_id,request_id)).fetchone()
        if prior:
            if prior["payload_hash"] != signature:
                raise Conflict("请求编号已用于另一项决定")
            return _decode(prior["response"])
        result = _context(db, result_id)
        _assert_current(result, body.get("revision"), body.get("version_id"))
        row = db.execute("SELECT * FROM multimodal_proposals WHERE id=? AND result_id=?", (proposal_id,result_id)).fetchone()
        if row is None:
            raise KeyError("视觉审校建议不存在")
        if (row["state"] not in ("pending", "question") or row["revision"] != result["revision"]
                or row["edited_sha256"] != fingerprint(result["edited"])):
            raise Conflict("视觉审校建议已过期，请重新审校")
        snapshot = _decode(db.execute("SELECT snapshot FROM multimodal_requests WHERE task_id=?", (row["task_id"],)).fetchone()[0])
        _verify_image(store, db, snapshot)
        target = json.loads(row["current_target"])
        if target_value(result["edited"], target) != row["current_value"]:
            raise Conflict("目标内容已变化，请重新审校")
        if action == "accept" and row["status"] == "uncertain":
            raise ValueError("存疑项没有可采用修改，请人工核对或保留存疑")
        siblings = [dict(r) for r in db.execute("SELECT * FROM multimodal_proposals WHERE task_id=? AND state!='stale'", (row["task_id"],))]
        changed = target if action == "accept" and row["status"] == "replace" else None
        after = apply_literal(result["edited"], target, row["after_value"]) if changed else deepcopy(result["edited"])
        if result["original"].get("origin") == "fusion":
            from ocr_workbench.fusion_alignment import canonical_edit
            after = canonical_edit(after)
        cursor, revision = result["cursor"]+1, result["revision"]+1
        db.execute("DELETE FROM edits WHERE result_id=? AND position>?", (result_id,result["cursor"]))
        db.execute("INSERT INTO edits VALUES(?,?,?,?)", (result_id,cursor,history_encoded(after),now()))
        db.execute("UPDATE results SET edited=?,cursor=?,revision=?,updated=? WHERE id=?", (encoded(after),cursor,revision,now(),result_id))
        from ocr_workbench.geometry import reconcile_geometry
        from ocr_workbench.structure_store import reconcile_structure
        from ocr_workbench.review_issues import reconcile
        reconcile(db,result_id,result["edited"],after)
        reconcile_geometry(db,result_id,result["edited"],after)
        reconcile_structure(db,result_id,result["edited"],after)
        digest = fingerprint(after)
        for sibling in siblings:
            old_target = json.loads(sibling["current_target"])
            is_deciding = sibling["id"] == proposal_id
            next_target = deepcopy(target) if is_deciding else rebase_target(old_target, changed, row["after_value"])
            expected = row["after_value"] if is_deciding and changed else sibling["current_value"]
            if is_deciding and changed and target["kind"] == "text":
                next_target["end"] = target["start"] + len(row["after_value"])
            # An accepted deletion has an empty provenance span; never make it actionable again.
            empty_deletion = (next_target is not None and next_target["kind"] == "text" and expected == ""
                and next_target["start"] == next_target["end"] and 0 <= next_target["start"] <= len(after["text"])
                and (is_deciding and changed or sibling["state"] == "accepted"))
            try:
                valid = next_target is not None and (empty_deletion or target_value(after, next_target) == expected)
            except ValueError:
                valid = False
            if not valid:
                continue
            state = {"accept":"accepted","reject":"rejected","question":"question"}[action] if is_deciding else sibling["state"]
            history = json.loads(sibling["rebase_history"])
            history.append({"revision":revision,"decision_id":request_id,"from":old_target,"to":next_target})
            db.execute("UPDATE multimodal_proposals SET current_target=?,current_value=?,state=?,revision=?,edited_sha256=?,rebase_history=?,updated=? WHERE id=?",
                (encoded(next_target),expected,state,revision,digest,encoded(history),now(),sibling["id"]))
        saved = _saved(db,result_id)
        db.execute("INSERT INTO multimodal_decisions VALUES(?,?,?,?,?,?,?)", (result_id,request_id,proposal_id,signature,action,history_encoded(saved),now()))
    return saved


def review_sources(db, result):
    rows = db.execute("""SELECT q.*,t.status,t.phase,t.error,t.started,t.finished FROM multimodal_requests q
        JOIN tasks t ON t.id=q.task_id WHERE q.result_id=? ORDER BY q.created,q.task_id""", (result["id"],)).fetchall()
    if not rows:
        return None
    requests = []
    for row in rows:
        snapshot = _decode(row["snapshot"])
        snapshot.pop("image_path", None)
        requests.append({**_request_view(row), "snapshot": snapshot,
                         "response": _decode(row["raw_response"]) if row["raw_response"] else None})
    return {"schema_version":CONTRACT_VERSION,"result_id":result["id"],"revision":result["revision"],
        "automatic_adoption":False,"contributes_to_votes":False,"requests":requests,
        "proposals":[_proposal(r) for r in db.execute("SELECT * FROM multimodal_proposals WHERE result_id=? ORDER BY created,id", (result["id"],))],
        "decisions":[dict(r) for r in db.execute("SELECT request_id,proposal_id,action,created FROM multimodal_decisions WHERE result_id=? ORDER BY created", (result["id"],))]}
