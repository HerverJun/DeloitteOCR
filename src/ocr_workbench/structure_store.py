"""Immutable candidate snapshots, revision-bound proposals and durable decisions."""
from copy import deepcopy
import json

from ocr_workbench.geometry_contract import fingerprint
from ocr_workbench.structure_diagnostics import (
    VERSION, differences, identify_tables, local_variants, prepare_candidates,
    preserve_values, table_value,
)


def migrate_v10(db):
    for sql in (
        """CREATE TABLE structure_candidates(id TEXT PRIMARY KEY,
            result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
            version_id TEXT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
            image_sha256 TEXT NOT NULL,provider TEXT NOT NULL,payload TEXT NOT NULL,
            prediction_sha256 TEXT NOT NULL,artifact TEXT,created TEXT NOT NULL)""",
        "CREATE INDEX structure_candidates_result ON structure_candidates(result_id,version_id,created)",
        """CREATE TABLE structure_proposals(id TEXT PRIMARY KEY,
            result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
            version_id TEXT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,scope TEXT NOT NULL,basis TEXT NOT NULL,
            payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',created TEXT NOT NULL,updated TEXT NOT NULL)""",
        "CREATE INDEX structure_proposals_result ON structure_proposals(result_id,state,revision)",
        """CREATE TABLE structure_checks(result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,version_id TEXT NOT NULL,candidates_sha256 TEXT NOT NULL,created TEXT NOT NULL)""",
        """CREATE TABLE structure_decisions(result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL,proposal_id TEXT NOT NULL REFERENCES structure_proposals(id) ON DELETE CASCADE,
            payload_hash TEXT NOT NULL,action TEXT NOT NULL,response BLOB NOT NULL,created TEXT NOT NULL,
            PRIMARY KEY(result_id,request_id))""",
        """CREATE TRIGGER structure_version_changed AFTER UPDATE OF active_version ON images
            WHEN OLD.active_version IS NOT NEW.active_version BEGIN
            UPDATE structure_proposals SET state='stale' WHERE result_id IN
            (SELECT r.id FROM results r JOIN tasks t ON t.id=r.task_id WHERE t.image_id=NEW.id); END""",
        """CREATE TRIGGER structure_selection_changed AFTER UPDATE OF result_id ON selections
            WHEN OLD.result_id IS NOT NEW.result_id BEGIN
            UPDATE structure_proposals SET state='stale' WHERE result_id=OLD.result_id; END""",
    ):
        db.execute(sql)


def record_candidates(db, result_id, version, prediction, blocks, *, source_result=None, artifact=None):
    from ocr_workbench.store import encoded, now
    payload = prepare_candidates(prediction, blocks, source_result=source_result or result_id,
        image_version=version["id"], width=version["width"], height=version["height"])
    prediction_hash = fingerprint(prediction)
    key = fingerprint({"result": result_id, "version": version["id"], "prediction": prediction_hash,
                       "tokens": payload["token_pool_sha256"], "algorithm": VERSION})
    payload["prediction"] = prediction
    payload["candidate_set_id"] = key
    inserted = db.execute("INSERT OR IGNORE INTO structure_candidates VALUES(?,?,?,?,?,?,?,?,?)",
        (key, result_id, version["id"], version["sha256"], prediction.get('candidate_provider_key') or ("paddle" if prediction.get("component", "paddle") == "paddle-table-v2" else prediction.get("component", "paddle")),
         encoded(payload), prediction_hash, artifact, now())).rowcount
    if inserted:
        db.execute("DELETE FROM structure_checks WHERE result_id=?", (result_id,))
        db.execute("UPDATE structure_proposals SET state='stale',updated=? WHERE result_id=? AND state IN ('pending','deferred')", (now(),result_id))
    return key


def import_saved_candidates(store, db, result, version):
    """Reuse only image-bound legacy artifacts whose saved byte hash verifies."""
    import hashlib
    rows = db.execute("SELECT details FROM geometry_evidence WHERE result_id=? AND version_id=? AND image_sha256=? ORDER BY created",
        (result['id'],version['id'],version['sha256'])).fetchall()
    seen = set()
    for row in rows:
        details = json.loads(row['details'])
        relative, digest = details.get('artifact'), details.get('artifact_sha256')
        if not relative or not digest or (relative,digest) in seen:
            continue
        seen.add((relative,digest))
        if db.execute('SELECT 1 FROM structure_candidates WHERE result_id=? AND artifact=?', (result['id'],relative)).fetchone():
            continue
        path = (store.root / relative).resolve()
        if not path.is_relative_to(store.root.resolve()) or not path.is_file():
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            continue
        prediction = json.loads(data)
        if prediction.get('image_version') != version['id'] or prediction.get('image_sha256') not in (None,version['sha256']):
            continue
        blocks = prediction.get('ocr_blocks')
        if not isinstance(blocks,list):
            continue
        record_candidates(db,result['id'],version,prediction,blocks,
            source_result=prediction.get('ocr_source') or result['id'],artifact=relative)


def context(db, result_id):
    from ocr_workbench.store import Conflict
    row = db.execute("""SELECT r.*,t.image_id,t.version_id task_version,i.active_version,s.result_id selected
        FROM results r JOIN tasks t ON t.id=r.task_id JOIN images i ON i.id=t.image_id
        LEFT JOIN selections s ON s.image_id=i.id WHERE r.id=?""", (result_id,)).fetchone()
    if row is None:
        raise KeyError("识别结果不存在")
    result = dict(row)
    result["original"], result["edited"] = json.loads(row["original"]), json.loads(row["edited"])
    result["version_id"] = result["original"].get("project_image_version") or row["task_version"]
    if result["version_id"] != row["active_version"]:
        raise Conflict("请切回结果对应的图像版本后检查结构")
    return result


def scope_value(edit, indices):
    if not indices:
        return edit
    return [table_value(edit["tables"][i]) if 0 <= i < len(edit["tables"]) else None for i in indices]


def reconcile_structure(db, result_id, before, after):
    """Decisions survive unrelated edits, proposals always expire on new revisions."""
    from ocr_workbench.store import now
    for row in db.execute("SELECT * FROM structure_proposals WHERE result_id=? AND state!='stale'", (result_id,)).fetchall():
        indices = json.loads(row["scope"])
        if row["state"] in ("pending", "deferred") or scope_value(before, indices) != scope_value(after, indices):
            db.execute("UPDATE structure_proposals SET state='stale',updated=? WHERE id=?", (now(), row["id"]))


def _current_tables(db, result):
    from ocr_workbench.fusion_alignment import source_tables, table_content
    tables = deepcopy(result["edited"]["tables"])
    originals = source_tables({"original": result["original"]})
    aligned = []
    for i, table in enumerate(tables):
        found = [o for o in originals if (table.get("fusion_id") and table.get("fusion_id") == o.get("fusion_id")) or
                 (table.get("source") and table["source"] == o.get("source")) or table_content(table) == table_content(o)]
        original = found[0] if len(found) == 1 else None
        aligned.append(original)
        if original:
            table["region_polygon"] = original.get("region_polygon")
        # Use only already verified table evidence for this exact image/structure.
        from ocr_workbench.document_store import structure_fingerprint
        rows = db.execute("""SELECT polygon,region_id FROM geometry_evidence WHERE result_id=? AND version_id=?
            AND status='valid' AND structure_sha256=? AND json_extract(target,'$.kind')='table'
            AND json_extract(details,'$.reason') IN ('accepted','table_region')
            AND json_extract(target,'$.table')=? AND polygon IS NOT NULL ORDER BY created DESC LIMIT 1""",
            (result["id"], result["version_id"], structure_fingerprint(result["edited"]), i)).fetchall()
        if rows:
            table["region_polygon"], table["region_id"] = json.loads(rows[0]["polygon"]), rows[0]["region_id"]
    return tables, aligned


def current_candidates(db, result, tool):
    """Latest provider sets for this result's current image and tool run."""
    from ocr_workbench.table_tool import current_candidate
    rows = db.execute("""SELECT c.* FROM structure_candidates c JOIN versions v ON v.id=c.version_id
        WHERE c.result_id=? AND c.version_id=? AND c.image_sha256=v.sha256 ORDER BY c.created DESC,c.id""",
        (result['id'], result['version_id'])).fetchall()
    seen, current = set(), []
    for row in rows:
        if row['provider'] not in seen and current_candidate(row, tool):
            seen.add(row['provider'])
            current.append(row)
    return current


def structure_snapshot(db, result):
    """Share actionable proposals and check identity within one read snapshot."""
    from ocr_workbench.table_tool import view as tool_view
    tool = tool_view(db, result)
    candidates = current_candidates(db, result, tool)
    live_sets = {row['id'] for row in candidates}
    proposals = []
    for row in db.execute("""SELECT * FROM structure_proposals WHERE result_id=? AND version_id=?
            AND state!='stale' ORDER BY created DESC,id""", (result['id'], result['version_id'])):
        item = {**json.loads(row['payload']), 'id': row['id'], 'basis': row['basis'],
                'revision': row['revision'], 'state': row['state'], 'version_id': row['version_id']}
        pending = row['state'] in ('pending', 'deferred')
        if pending and (row['revision'] != result['revision'] or item['candidate_set_id'] not in live_sets):
            continue
        # Historical decisions remain visible after unrelated edits or tool
        # upgrades, but are never offered as fresh, actionable suggestions.
        item['can_apply'] = bool(item['can_apply'] and pending and result['selected'] == result['id'])
        proposals.append(item)
    proposals.sort(key=lambda p: (p['priority'], p['table_indices'], p['kind'] != 'replace_table', p['id']))
    check = db.execute("""SELECT candidates_sha256 FROM structure_checks
        WHERE result_id=? AND revision=? AND version_id=?""",
        (result['id'], result['revision'], result['version_id'])).fetchone()
    checked = bool(check and check['candidates_sha256'] == fingerprint([row['id'] for row in candidates])
                   and tool['state'] in ('ready', 'empty', 'not_applicable'))
    return {'table_tool': tool, 'candidate_rows': candidates, 'proposals': proposals, 'checked': checked}


def refresh_proposals(store, result_id, revision):
    from ocr_workbench.store import Conflict, encoded, now
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        result = context(db, result_id)
        if result["revision"] != revision:
            raise Conflict("修订已变化，请保存后重新检查结构")
        version = db.execute("SELECT * FROM versions WHERE id=?", (result["version_id"],)).fetchone()
        import_saved_candidates(store,db,result,version)
        from ocr_workbench.table_tool import view as tool_view
        tool = tool_view(db, result)
        # Latest set from each provider, with an explicitly shared text pool.
        sets = [(row, json.loads(row['payload'])) for row in current_candidates(db, result, tool)]
        current, originals = _current_tables(db, result)
        for row, payload in sets:
            from ocr_workbench.structure_groups import group_suggestions
            manual_tables = {json.loads(b['target']).get('table') for b in db.execute("SELECT target FROM geometry_evidence WHERE result_id=? AND source='manual' AND status='valid'",(result_id,))}
            for group in group_suggestions(result['edited'],current,originals,payload['tables'],manual_tables):
                targets = group['candidate_tables']
                proposal = {'kind':group['kind'],'table_indices':group['table_indices'],
                    'current_tables':[result['edited']['tables'][i] for i in group['table_indices']],
                    'proposed_tables':[c['skeleton'] for c in targets],'proposed_edit':group['proposed_edit'],
                    'differences':[{'kind':'table_identity','current':len(group['table_indices']),'candidate':len(targets)}],
                    'conflicts':[],'candidate_set_id':row['id'],'candidate_table_ids':[c['id'] for c in targets],
                    'provider':row['provider'],'polygon':targets[0]['polygon'] if len(targets)==1 else current[group['table_indices'][0]]['region_polygon'],
                    'token_pool_sha256':payload['token_pool_sha256'],'image_sha256':version['sha256'],'can_apply':not payload['rejected_tokens'],
                    'unverified_empty_cells':[{'table':ti,'row':c['row'],'column':c['column']} for ti,t in enumerate(targets)
                        for c in t['skeleton']['cells'] if c['structure_source']['text_state']=='unverified_empty'],
                    'priority':0,'reason':'independent_regions_and_exact_source_partition','version':VERSION}
                _persist_proposal(db,result,proposal)
            if not current and result['edited']['text'] == result['original']['text'] and result['original'].get('origin') == 'document':
                from ocr_workbench.native_tables import native_table_preview
                preview = native_table_preview(result['original'],payload['prediction'],version['width'],version['height'])
                if preview:
                    native_tables = deepcopy(preview['tables'])
                    native_candidates = identify_tables(native_tables,payload['tables'])
                    if all(native_candidates):
                        for table_index,(table, match) in enumerate(zip(native_tables,native_candidates)):
                            candidate = payload['tables'][match[1]]
                            source_cells = {(c['row'],c['column']):c for c in candidate['skeleton']['cells']}
                            for cell in table['cells']:
                                source = source_cells.get((cell['row'],cell['column']))
                                if source:
                                    cell['structure_source'] = deepcopy(source['structure_source'])
                                    units = preview['document']['table_native_cells'][f"{table_index}:{cell['row']}:{cell['column']}"]['units']
                                    by_id = {t['id']:t for t in payload['tokens']}
                                    ordered = []
                                    for unit in units:
                                        matches = [tid for tid in source['structure_source']['token_ids'] if tid not in ordered
                                            and by_id[tid]['raw_text'] == unit['text'] and by_id[tid]['polygon'] == unit['polygon']]
                                        if len(matches)==1:
                                            ordered.append(matches[0])
                                    if len(ordered)==len(units):
                                        cell['structure_source']['token_ids'] = ordered
                        proposal = {'kind':'native_table','table_indices':[], 'current_tables':[], 'proposed_tables':native_tables,
                            'proposed_text':preview['text'], 'differences':[{'kind':'table_identity'}], 'conflicts':[],
                            'candidate_set_id':row['id'], 'candidate_table_ids':[payload['tables'][m[1]]['id'] for m in native_candidates],
                            'provider':row['provider'], 'token_pool_sha256':payload['token_pool_sha256'], 'text_source_result':result_id,
                            'polygon':payload['tables'][native_candidates[0][1]]['polygon'] if len(native_candidates)==1 else None,
                            'image_sha256':version['sha256'], 'can_apply':True, 'priority':0,
                            'unverified_empty_cells':[{'table':ti,'row':c['row'],'column':c['column']} for ti,t in enumerate(native_tables)
                                for c in t['cells'] if c.get('structure_source',{}).get('text_state')=='unverified_empty'],
                            'reason':'native_text_structure','version':VERSION}
                        _persist_proposal(db,result,proposal)
                        continue
            matches = identify_tables(current, payload["tables"])
            matched = set()
            for ti, match in enumerate(matches):
                if not match:
                    continue
                ci, evidence = match[1], match[2]
                matched.add(ci)
                candidate = payload["tables"][ci]
                proposed = candidate["skeleton"]
                delta = differences(current[ti], proposed)
                if not delta and not candidate["unassigned_token_ids"] and not candidate["reason_codes"]:
                    continue
                bindings = [{**dict(b), "target": json.loads(b["target"]), "polygon": json.loads(b["polygon"])} for b in db.execute(
                    "SELECT * FROM geometry_evidence WHERE result_id=? AND version_id=? AND status='valid' AND source='manual' AND json_extract(target,'$.table')=?",
                    (result_id, version["id"], ti)) if b["polygon"]]
                original = originals[ti] if ti < len(originals) else None
                variants = [{"kind": "replace_table", "range": None, "table": proposed}] + local_variants(current[ti], proposed)
                for variant in variants:
                    # Local variants retain untouched cells and their original metadata.
                    table, conflicts, retained = preserve_values(current[ti], variant["table"], original, payload["tokens"], bindings)
                    if candidate["unassigned_token_ids"]:
                        conflicts.append({"kind": "unassigned_tokens", "token_ids": candidate["unassigned_token_ids"]})
                    if payload["rejected_tokens"]:
                        conflicts.append({"kind": "rejected_source_tokens", "tokens": payload["rejected_tokens"]})
                    fatal = [c for c in candidate["reason_codes"] if c not in ("unverified_lineage", "boxes_slots_out_of_sync")]
                    if fatal:
                        conflicts.append({"kind": "invalid_candidate", "reasons": fatal})
                    table["fusion_id"] = current[ti].get("fusion_id", fingerprint({"result": result_id, "table": ti})[:24])
                    table["source"] = current[ti].get("source")
                    proposal = {"kind": variant["kind"], "range": variant["range"], "table_indices": [ti],
                        "current_tables": [result["edited"]["tables"][ti]], "proposed_tables": [table],
                        "differences": differences(current[ti], variant["table"]), "conflicts": conflicts,
                        "retained_values": retained, "candidate_set_id": row["id"], "candidate_table_ids": [candidate["id"]],
                        "provider": row["provider"], "model_versions": sorted({c["model_version"] for c in candidate["original_cells"]}),
                        "text_source_result": payload["tokens"][0]["source_result"] if payload["tokens"] else None,
                        "token_pool_sha256": payload["token_pool_sha256"], "unassigned_tokens": [t for t in payload["tokens"] if t["id"] in candidate["unassigned_token_ids"]],
                        "identity_evidence": evidence, "polygon": candidate["polygon"], "image_sha256": version["sha256"],
                        "can_apply": not conflicts, "priority": 1,
                        "unverified_empty_cells": [{"row": c["row"], "column": c["column"]} for c in table["cells"] if c.get("structure_source", {}).get("text_state") == "unverified_empty"],
                        "reason": "structure_difference", "version": VERSION}
                    _persist_proposal(db, result, proposal)
            # Identity ambiguity is one page task, with all affected regions attached.
            unmatched = [c for i, c in enumerate(payload["tables"]) if i not in matched]
            if unmatched or any(m is None for m in matches):
                proposal = {"kind": "table_identity", "table_indices": [i for i,m in enumerate(matches) if m is None],
                    "current_tables": [result["edited"]["tables"][i] for i,m in enumerate(matches) if m is None],
                    "proposed_tables": [c["skeleton"] for c in unmatched], "differences": [{"kind": "table_identity", "candidate_regions": [c["polygon"] for c in unmatched]}],
                    "conflicts": [{"kind": "table_identity_ambiguous"}], "candidate_set_id": row["id"],
                    "candidate_table_ids": [c["id"] for c in unmatched], "provider": row["provider"],
                    "polygon": unmatched[0]["polygon"] if len(unmatched) == 1 else None,
                    "image_sha256": version["sha256"], "can_apply": False, "priority": 0,
                    "reason": "table_identity_ambiguous", "version": VERSION}
                _persist_proposal(db, result, proposal)
        db.execute("INSERT OR REPLACE INTO structure_checks VALUES(?,?,?,?,?)", (result_id,revision,version["id"],fingerprint([row["id"] for row,_ in sets]),now()))
    return structure_view(store, result_id)


def _persist_proposal(db, result, proposal):
    from ocr_workbench.store import encoded, now
    basis = fingerprint({"edit": result["edited"], "revision": result["revision"], "version": result["version_id"], "proposal": proposal})
    key = fingerprint({"result": result["id"], "basis": basis})
    # A resolved decision remains valid after an unrelated edit. Do not recreate it.
    scope = proposal["table_indices"]
    for old in db.execute("SELECT payload,state FROM structure_proposals WHERE result_id=? AND scope=? AND state IN ('kept','rejected')", (result["id"], encoded(scope))):
        previous = json.loads(old["payload"])
        if previous == proposal:
            return
    # A new provider expires the page check. Rechecking the identical revision
    # must restore unchanged alternatives too; the immutable basis/ID remains.
    db.execute("""INSERT INTO structure_proposals VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET state='pending',updated=excluded.updated
        WHERE structure_proposals.state='stale'""",
        (key, result["id"], result["version_id"], result["revision"], encoded(scope), basis, encoded(proposal), "pending", now(), now()))


def structure_view(store, result_id):
    with store.transaction() as db:
        db.execute('BEGIN')
        result = context(db, result_id)
        snapshot = structure_snapshot(db, result)
        candidates = [{"id": r["id"], "provider": r["provider"], "created": r["created"], "tables": len(json.loads(r["payload"])["tables"]),
                       "token_pool_sha256": json.loads(r["payload"])["token_pool_sha256"]} for r in snapshot['candidate_rows']]
        return {"result_id": result_id, "revision": result["revision"], "version_id": result["version_id"],
                "adopted": result["selected"] == result_id, "experimental": True,
                "automatic_adoption": False, "candidates": candidates, "proposals": snapshot['proposals'],
                "table_tool": snapshot['table_tool']}


def decide_structure(store, result_id, proposal_id, body):
    from ocr_workbench.editing import validate_edit
    from ocr_workbench.fusion_alignment import canonical_edit
    from ocr_workbench.geometry import reconcile_geometry
    from ocr_workbench.review_issues import reconcile
    from ocr_workbench.store import Conflict, encoded, history_encoded, history_decoded, now
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise ValueError("请提供决策请求编号")
    action = body.get("action")
    if action not in ("accept", "keep", "reject", "defer"):
        raise ValueError("未知结构复核操作")
    signature = fingerprint({"proposal_id": proposal_id, "body": body})
    with store.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM structure_decisions WHERE result_id=? AND request_id=?", (result_id, request_id)).fetchone()
        if prior:
            if prior["payload_hash"] != signature:
                raise Conflict("请求编号已用于另一项决策")
            return json.loads(history_decoded(prior["response"]))
        result = context(db, result_id)
        if result["selected"] != result_id:
            raise Conflict("请先采用当前结果后复核结构")
        row = db.execute("SELECT * FROM structure_proposals WHERE id=? AND result_id=?", (proposal_id,result_id)).fetchone()
        if row is None:
            raise KeyError("结构建议不存在")
        if (row["state"] not in ("pending", "deferred") or row["revision"] != result["revision"] or
            body.get("revision") != result["revision"] or body.get("basis") != row["basis"] or
            body.get("version_id") != result["version_id"]):
            raise Conflict("结构建议已过期，请重新检查后复核")
        proposal = json.loads(row["payload"])
        from ocr_workbench.table_tool import view as tool_view
        if proposal['candidate_set_id'] not in {c['id'] for c in current_candidates(db, result, tool_view(db, result))}:
            raise Conflict('结构候选已过期，请重新提取并检查')
        version = db.execute("SELECT sha256 FROM versions WHERE id=?", (result["version_id"],)).fetchone()
        if version[0] != proposal["image_sha256"]:
            raise Conflict("图像内容已变化，请重新生成建议")
        before, after = result["edited"], deepcopy(result["edited"])
        if action == "accept":
            prior_manual = [dict(b) for b in db.execute("SELECT * FROM geometry_evidence WHERE result_id=? AND version_id=? AND source='manual' AND status='valid'",(result_id,result['version_id']))]
            if not proposal["can_apply"]:
                raise ValueError("此建议含未解决的文字或身份冲突，请先手工核对")
            if proposal.get("unverified_empty_cells") and body.get("acknowledge_unverified_empty") is not True:
                raise ValueError("新增空值尚未确定，请核对原图并确认这些格仍需复核")
            if proposal['kind'] == 'native_table':
                after = {'text':proposal['proposed_text'],'tables':deepcopy(proposal['proposed_tables'])}
                changed_indices = list(range(len(after['tables'])))
            elif proposal['kind'] in ('split_tables','merge_tables'):
                after = deepcopy(proposal['proposed_edit'])
                first = proposal['table_indices'][0]
                changed_indices = list(range(first,first+len(proposal['proposed_tables'])))
            else:
                ti = proposal["table_indices"][0]
                after["tables"][ti] = deepcopy(proposal["proposed_tables"][0])
                changed_indices = [ti]
            for ti in changed_indices:
                after["tables"][ti]["structure_review"] = {"proposal_id": proposal_id, "candidate_set_id": proposal["candidate_set_id"],
                "basis_revision": result["revision"], "provider": proposal["provider"], "token_pool_sha256": proposal["token_pool_sha256"],
                "unverified_empty_cells": proposal.get("unverified_empty_cells", [])}
            after = canonical_edit(after)
            validate_edit(after)
            cursor = result["cursor"]+1
            db.execute("DELETE FROM edits WHERE result_id=? AND position>?", (result_id,result["cursor"]))
            db.execute("INSERT INTO edits VALUES(?,?,?,?)", (result_id,cursor,history_encoded(after),now()))
            db.execute("UPDATE results SET edited=?,cursor=?,revision=revision+1,updated=? WHERE id=?", (encoded(after),cursor,now(),result_id))
            reconcile(db,result_id,before,after)
            reconcile_geometry(db,result_id,before,after)
            reconcile_structure(db,result_id,before,after)
            _remap_adopted(db,result,after,proposal)
            # Preserve manual bindings only through explicit one-to-one retention.
            from ocr_workbench.document_store import structure_fingerprint
            from ocr_workbench.store import uid
            for ti, cell in ((ti,cell) for ti in changed_indices for cell in after["tables"][ti]["cells"]):
                binding = cell.get("structure_source", {}).get("manual_binding")
                if binding:
                    target = {"kind": "cell", "table": ti, "row": cell["row"], "column": cell["column"]}
                    db.execute("INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uid(), result_id, result["version_id"], binding["region_id"], version[0], structure_fingerprint(after), "manual", "human",
                         encoded(target), encoded(binding["polygon"]), binding["details"], "valid", now()))
            from ocr_workbench.fusion_alignment import table_content
            for binding in prior_manual:
                target = json.loads(binding['target'])
                old_index = target.get('table')
                if target.get('kind') != 'cell' or old_index in proposal['table_indices'] or old_index is None:
                    continue
                index = old_index
                if proposal['kind'] in ('split_tables','merge_tables') and old_index > proposal['table_indices'][-1]:
                    index += len(proposal['proposed_tables'])-len(proposal['table_indices'])
                if not 0 <= index < len(after['tables']) or table_content(before['tables'][old_index]) != table_content(after['tables'][index]):
                    continue
                target['table'] = index
                db.execute("INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid(),result_id,result['version_id'],binding['region_id'],version[0],structure_fingerprint(after),'manual','human',
                     encoded(target),binding['polygon'],binding['details'],'valid',now()))
        state = {"accept": "accepted", "keep": "kept", "reject": "rejected", "defer": "deferred"}[action]
        db.execute("UPDATE structure_proposals SET state=?,updated=? WHERE id=?", (state,now(),proposal_id))
        if action == "keep":
            db.execute("UPDATE structure_proposals SET state='kept',updated=? WHERE result_id=? AND revision=? AND scope=? AND state IN ('pending','deferred')",
                       (now(),result_id,result["revision"],row["scope"]))
        saved = dict(db.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone())
        saved["original"], saved["edited"] = json.loads(saved["original"]), json.loads(saved["edited"])
        saved["can_undo"], saved["can_redo"] = saved["cursor"] > 0, bool(db.execute("SELECT 1 FROM edits WHERE result_id=? AND position>?", (result_id,saved["cursor"])).fetchone())
        db.execute("INSERT INTO structure_decisions VALUES(?,?,?,?,?,?,?)", (result_id,request_id,proposal_id,signature,action,history_encoded(saved),now()))
    return saved


def _remap_adopted(db, result, edit, proposal):
    """Recompute correspondence from the saved candidate and fixed token pool."""
    from ocr_workbench.table_matching import local_mapping, policy_for_algorithm
    from ocr_workbench.geometry import geometry_source
    from ocr_workbench.document_store import structure_fingerprint
    from ocr_workbench.store import encoded, now, uid
    candidate = db.execute("SELECT * FROM structure_candidates WHERE id=?", (proposal['candidate_set_id'],)).fetchone()
    payload = json.loads(candidate['payload'])
    version = db.execute("SELECT * FROM versions WHERE id=?", (result['version_id'],)).fetchone()
    blocks = [{'id':t['id'],'text':t['raw_text'],'polygon':t['polygon'],'granularity':t['granularity'],
               'source_kind':t['source_kind'],'source':t['engine']} for t in payload['tokens']]
    policy = policy_for_algorithm('local-v3')
    mappings = local_mapping(edit,payload['prediction'],version['width'],version['height'],policy=policy,
        result_id=result['id'],revision=result['revision']+1,image_version=result['version_id'],ocr_blocks=blocks)
    provider = candidate['provider']
    source = geometry_source(provider,'local-v3')
    for mapping in mappings:
        details = {**mapping,'geometry_provider':provider,'algorithm':'local-v3','structure_revision':result['revision']+1,
                   'candidate_set_id':candidate['id'],'artifact':candidate['artifact'],'contributes_to_votes':False}
        db.execute('INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uid(),result['id'],version['id'],None,version['sha256'],structure_fingerprint(edit),source,
             encoded(payload['prediction'].get('model_revisions',{})),encoded(mapping['target']),
             encoded(mapping['polygon']) if mapping['polygon'] else None,encoded(details),'valid',now()))
