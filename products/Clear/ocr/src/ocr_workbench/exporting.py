"""Disk-backed exports: separate image files by default, optional workbook."""

import json
from pathlib import Path
import shutil
import tempfile
import zipfile
import hashlib
from ocr_workbench.editing import validate_edit, export_markdown, export_text
from ocr_workbench.tables import export_xlsx


def build_export(store, keys, format, aggregate=False, confirmed_only=False, expected_results=None):
    if not isinstance(keys, list) or not keys or len(keys) > 1000:
        raise ValueError("请选择需要导出的结果（最多 1000 个）")
    if not all(isinstance(key, str) for key in keys):
        raise ValueError("结果编号无效")
    if format not in {"txt", "md", "json", "xlsx"}:
        raise ValueError("未知导出格式")
    if not isinstance(aggregate, bool):
        raise ValueError("汇总选项必须为布尔值")
    if not isinstance(confirmed_only, bool):
        raise ValueError("仅导出已确认选项必须为布尔值")
    keys = list(dict.fromkeys(keys))
    parent = store.root / "exports"
    parent.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="export-", dir=parent))
    snapshots, identities, pages = {}, {}, {}
    fusion_sources = []
    structure_files = []
    multimodal_files = []

    def capture_snapshot():
        if not hasattr(store, "transaction"):
            if confirmed_only:
                raise ValueError("此数据源不支持一致的复核状态检查")
            return
        # Copy one result at a time to disk under a single SQLite read snapshot.
        # Workbook generation runs after this transaction and releases its lock.
        with store.transaction() as db:
            db.execute("BEGIN")
            for index, key in enumerate(keys):
                row = db.execute("SELECT * FROM results WHERE id=?", (key,)).fetchone()
                if row is None:
                    raise KeyError("识别结果不存在")
                result = dict(row)
                task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (result['task_id'],)).fetchone())
                if expected_results is not None:
                    from ocr_workbench.store import Conflict
                    expected = expected_results.get(key)
                    if expected is None or (expected['revision'], expected['version_id']) != (result['revision'], task['version_id']):
                        raise Conflict("导出快照版本已变化，请重新核对范围")
                photo = dict(db.execute("SELECT * FROM images WHERE id=?", (task['image_id'],)).fetchone())
                if confirmed_only:
                    from ocr_workbench.store import Conflict

                    review = db.execute("SELECT * FROM reviews WHERE image_id=?", (photo['id'],)).fetchone()
                    adopted = store._review_target(db, photo['id'])
                    if (review is None or review['status'] != 'confirmed'
                            or review['result_id'] != key or adopted['result_id'] != key
                            or review['revision'] != result['revision']
                            or review['version_id'] != photo['active_version']
                            or adopted['version_id'] != photo['active_version']):
                        raise Conflict("导出范围含未确认或确认已失效的结果，请重新复核或调整范围")
                result['original'] = json.loads(result['original'])
                result['edited'] = json.loads(result['edited'])
                page = db.execute('SELECT p.document_id,p.page_number FROM pages p JOIN versions v ON v.image_id=p.image_id WHERE v.id=?',
                    (result['original'].get('project_image_version') or task['version_id'],)).fetchone()
                pages[key] = dict(page) if page else None
                from ocr_workbench.structure_export import structure_sources
                structure = structure_sources(db, result)
                if structure:
                    path = folder / f'_structure-{index}.json'
                    path.write_text(json.dumps(structure,ensure_ascii=False),encoding='utf-8')
                    structure_files.append({'result_id':key,'revision':result['revision'],'path':path})
                from ocr_workbench.multimodal_store import review_sources
                multimodal = review_sources(db, result)
                if multimodal:
                    path = folder / f'_multimodal-{index}.json'
                    path.write_text(json.dumps(multimodal, ensure_ascii=False), encoding='utf-8')
                    multimodal_files.append({'result_id': key, 'path': path})
                fusion = result['original'].get('fusion')
                if fusion:
                    from ocr_workbench.store import history_decoded
                    counts = {s: 0 for s in ('pending', 'question', 'stale', 'resolved')}
                    for issue in db.execute('SELECT state,COUNT(*) n FROM fusion_issues WHERE result_id=? GROUP BY state', (key,)):
                        counts[issue['state']] = issue['n']
                    review = db.execute('SELECT * FROM reviews WHERE image_id=?', (photo['id'],)).fetchone()
                    adopted = store._review_target(db, photo['id'])
                    confirmed = bool(review and review['status'] == 'confirmed' and review['result_id'] == key
                                     and review['revision'] == result['revision'] and adopted['result_id'] == key
                                     and review['version_id'] == photo['active_version'] == adopted['version_id'])
                    result['review_summary'] = {**counts, 'human_confirmed': confirmed}
                    evidence = folder / f'_fusion-{index}.json'
                    source_row = db.execute('SELECT snapshot FROM fusion_inputs WHERE task_id=?', (task['id'],)).fetchone()
                    # Stream the per-unit evidence from the pinned DB snapshot;
                    # don't load every result's evidence into memory at once.
                    with evidence.open('w', encoding='utf-8') as stream:
                        stream.write(json.dumps({'schema_version': 1, 'result_id': key, 'revision': result['revision'],
                                                'policy': fusion['policy'], 'policy_sha256': fusion['policy_sha256'],
                                                'review_summary': result['review_summary']}, ensure_ascii=False)[:-1])
                        stream.write(',"sources":')
                        stream.write(history_decoded(source_row[0]) if source_row else '[]')
                        for name, sql in (
                            ('units', 'SELECT definition value FROM fusion_evidence WHERE result_id=? ORDER BY ordinal'),
                            ('issues', "SELECT json_object('id',id,'state',state,'target',json(target),'current_value',json(current_value),'decision_id',decision_id) value FROM fusion_issues WHERE result_id=? ORDER BY ordinal"),
                            ('decisions', "SELECT json_object('request_id',request_id,'issue_id',issue_id,'action',action,'previous',json(previous),'created',created) value FROM fusion_decisions WHERE result_id=? ORDER BY created"),
                        ):
                            stream.write(',"' + name + '":[')
                            first = True
                            for entry in db.execute(sql, (key,)):
                                if not first: stream.write(',')
                                stream.write(entry['value'])
                                first = False
                            stream.write(']')
                        stream.write('}')
                    with evidence.open('rb') as source_stream:
                        evidence_hash = hashlib.file_digest(source_stream, 'sha256').hexdigest()
                    fusion_sources.append({'result_id': key, 'revision': result['revision'],
                                           'image_id': photo['id'], 'image_version': task['version_id'],
                                           'policy_version': fusion['policy']['version'], 'policy_sha256': fusion['policy_sha256'],
                                           'review_summary': result['review_summary'], 'file': f'sources/{key}.json',
                                           'sha256': evidence_hash, '_path': str(evidence)})
                result['can_undo'] = result['cursor'] > 0
                result['can_redo'] = db.execute(
                    "SELECT 1 FROM edits WHERE result_id=? AND position>? LIMIT 1",
                    (key, result['cursor'])).fetchone() is not None
                path = folder / f'_snapshot-{index}.json'
                path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
                snapshots[key] = path
                identities[key] = (task, photo)

    def reject_empty(names):
        raise ValueError(
            f"选中结果中有 {len(names)} 张图片没有结构化表格："
            + "、".join(names)
            + "。请选择其他格式或调整选择。"
        )

    def read(key, missing=None):
        result = json.loads(snapshots[key].read_text('utf-8')) if key in snapshots else store.result(key)
        validate_edit(result["edited"])
        if format == "xlsx" and not result["edited"]["tables"]:
            if key in identities:
                name = identities[key][1]['name']
            else:
                task = store.one("tasks", result["task_id"])
                name = store.one("images", task["image_id"])["name"]
            if missing is None:
                reject_empty([name])
            missing.append(name)
        return result

    def sources(result, workbook, sheet_start=1):
        if result['id'] in identities:
            task, image = identities[result['id']]
        else:
            task = store.one("tasks", result["task_id"])
            image = store.one("images", task["image_id"])
        original = result.get("original", {})
        metadata = {
            "workbook": workbook,
            "image_name": image["name"],
            "image_id": task["image_id"],
            "image_version": original.get("project_image_version", task.get("version_id", "")),
            "input_sha256": original.get("image", {}).get("version", ""),
            "engine": original.get("engine", task.get("engine", "")),
            "engine_name": original.get("engine_info", {}).get("name", ""),
            "engine_package": original.get("engine_package", {}).get("id", task.get("engine_package", "builtin")),
            "model_revisions": json.dumps(original.get("model_revisions", {}), ensure_ascii=False, sort_keys=True),
            "result_id": result["id"],
            "revision": result.get("revision", 0),
            "origin": original.get("origin", "single-engine"),
            "policy_version": original.get("fusion", {}).get("policy", {}).get("version", ""),
            "policy_sha256": original.get("fusion", {}).get("policy_sha256", ""),
            "review_summary": json.dumps(result.get("review_summary", {}), ensure_ascii=False),
            "document_id": image.get("document_id", ""),
        }
        if result['id'] in pages or hasattr(store, 'page_for_version'):
            page = pages[result['id']] if result['id'] in pages else store.page_for_version(metadata['image_version'])
            if page:
                metadata.update(document_id=page['document_id'],page_number=page['page_number'])
        return [{**metadata, "sheet": f"Table {sheet_start + index}", "table_index": index + 1,
                 "structure_review": json.dumps(table.get('structure_review', {}),ensure_ascii=False),
                 "header_cells": json.dumps([[c['row'],c['column'],c['row_span'],c['column_span']] for c in table['cells'] if c.get('is_header')])}
                for index,table in enumerate(result["edited"]["tables"])]

    def write(result, path):
        if format == "xlsx":
            mapping = sources(result, path.name)
            export_xlsx(result["edited"]["tables"], path, source_rows=mapping)
            return mapping
        else:
            content = (
                json.dumps(result, ensure_ascii=False, indent=2)
                if format == "json"
                else (
                    export_markdown(result["edited"])
                    if format == "md"
                    else export_text(result["edited"])
                )
            )
            path.write_text(content, encoding="utf-8")

    try:
        capture_snapshot()
        if format == "xlsx":
            # Validate every image before writing, without accumulating raw results.
            missing = []
            for key in keys:
                read(key, missing)
            if missing:
                reject_empty(missing)
        if format == "xlsx" and aggregate:
            # Only the requested tables are retained; raw model output is not accumulated.
            mapping = []
            def tables():
                for key in keys:
                    result = read(key)
                    mapping.extend(sources(result, "OCR-tables.xlsx", len(mapping) + 1))
                    yield from result["edited"]["tables"]

            target = folder / "OCR-tables.xlsx"
            export_xlsx(tables(), target, source_rows=mapping)
        elif len(keys) == 1:
            target = folder / ("OCR-result." + format)
            write(read(keys[0]), target)
        else:
            target = folder / "OCR-results.zip"
            with zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                mapping = []
                for index, key in enumerate(keys, 1):
                    # Numeric names cannot inherit user filenames or path separators.
                    item = folder / f"{index:04d}.{format}"
                    item_mapping = write(read(key), item)
                    if item_mapping:
                        mapping.extend(item_mapping)
                    archive.write(item, item.name)
                    item.unlink()
                if format == "xlsx":
                    archive.writestr("sources.json", json.dumps(
                        {"schema_version": 1, "tables": mapping}, ensure_ascii=False, indent=2))
        if fusion_sources:
            original_target = target
            if target.suffix != '.zip':
                target = folder / 'OCR-fusion-export.zip'
            with zipfile.ZipFile(target, 'a' if target == original_target else 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                if original_target != target:
                    archive.write(original_target, original_target.name)
                archive.writestr('fusion-sources.json', json.dumps({'schema_version': 1, 'results': [
                    {k: v for k, v in source.items() if k != '_path'} for source in fusion_sources]}, ensure_ascii=False, indent=2))
                for source in fusion_sources:
                    archive.write(source['_path'], source['file'])
            if original_target != target:
                original_target.unlink()
            for source in fusion_sources:
                Path(source['_path']).unlink()
        if structure_files:
            original_target = target
            if target.suffix != '.zip':
                target = folder / 'OCR-structure-export.zip'
            with zipfile.ZipFile(target,'a' if target == original_target else 'w',compression=zipfile.ZIP_DEFLATED) as archive:
                if original_target != target:
                    archive.write(original_target,original_target.name)
                for source in structure_files:
                    archive.write(source['path'],f"sources/structure-{source['result_id']}.json")
                    source['path'].unlink()
            if original_target != target:
                original_target.unlink()
        if multimodal_files:
            original_target = target
            if target.suffix != '.zip':
                target = folder / 'OCR-reviewed-export.zip'
            with zipfile.ZipFile(target, 'a' if target == original_target else 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                if original_target != target:
                    archive.write(original_target, original_target.name)
                for source in multimodal_files:
                    archive.write(source['path'], f"sources/multimodal-{source['result_id']}.json")
                    source['path'].unlink()
            if original_target != target:
                original_target.unlink()
        for path in snapshots.values():
            path.unlink()
        return target
    except BaseException:
        shutil.rmtree(folder)
        raise
