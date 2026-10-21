"""Bounded literal project-document search with verified file/page sources."""
from pathlib import Path

from workbench_platform.application_services.references import verify
from workbench_platform.audit.direct import source_locator


def search(args, ctx):
    registry = ctx.services.asset_library[0]
    asset_offset = getattr(args, 'asset_offset', 0)
    if ctx.selected_assets:
        identifiers = list(dict.fromkeys(ctx.selected_assets))
        total = len(identifiers)
        assets = [registry.get(ctx.workspace_id, identifier) for identifier in identifiers[asset_offset:asset_offset+200]]
    else:
        rows, total = registry.list(ctx.workspace_id, offset=asset_offset, limit=200)
        assets = [registry.get(ctx.workspace_id, item['asset_id']) for item in rows]
    matches, skipped, inspected = [], [], 0
    needle = args.query.casefold()
    next_asset_offset, next_asset_match_offset = -1, 0
    for asset_index, asset in enumerate(assets):
        suffix = Path(asset.original_name).suffix.lower()
        document_service = getattr(ctx.services, 'document_service', None)
        saved = None
        if document_service:
            from contextlib import closing
            import json
            with closing(document_service.db()) as db:
                rows = db.execute('SELECT body FROM documents WHERE workspace_id=? ORDER BY rowid DESC', (ctx.workspace_id,)).fetchall()
            saved = next((value for row in rows if (value := json.loads(row[0]))['asset_id'] == asset.asset_id
                and value['asset_revision'] == asset.revision and value['blob_hash'] == asset.blob_hash
                and value['status'] in {'succeeded', 'partial'}), None)
        if not saved and suffix not in {'.txt', '.md', '.csv', '.json', '.pdf'}:
            skipped.append({'asset_id': asset.asset_id, 'name': asset.original_name, 'reason': '需要OCR或结构化导入'}); continue
        if not saved and asset.size_bytes > 8 * 1024 * 1024:
            skipped.append({'asset_id': asset.asset_id, 'name': asset.original_name, 'reason': '超过当前原文读取范围'}); continue
        with registry.stage_verified(ctx.workspace_id, asset.asset_id, revision=asset.revision, blob_hash=asset.blob_hash) as path:
            if saved:
                document = document_service.parsed(ctx.workspace_id, saved['document_id'])
                chunks = [(page['page_number'], '\n'.join(block['text'] for block in page['blocks'])) for page in document['pages']]
                if any(page['needs_ocr'] for page in document['pages']):
                    skipped.append({'asset_id': asset.asset_id, 'name': asset.original_name, 'reason': '已解析文档部分页仍需OCR，已保留可读页检索'})
            elif suffix == '.pdf':
                try:
                    from pypdf import PdfReader
                except ImportError:
                    skipped.append({'asset_id': asset.asset_id, 'reason': '当前运行时没有PDF原文解析；可先OCR再检索'}); continue
                reader = PdfReader(path)
                if reader.is_encrypted or len(reader.pages) > 200:
                    skipped.append({'asset_id': asset.asset_id, 'reason': '加密或超过200页；可指定页OCR'}); continue
                chunks = [(number + 1, (page.extract_text() or '')[:100000]) for number, page in enumerate(reader.pages)]
            else:
                payload = path.read_bytes()
                text = None
                for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
                    try:
                        text = payload.decode(encoding); break
                    except UnicodeError:
                        continue
                if text is None:
                    skipped.append({'asset_id': asset.asset_id, 'reason': '无法读取文本编码'}); continue
                chunks = [(None, text)]
        inspected += 1
        matched_lines = 0
        skip_matches = getattr(args, 'asset_match_offset', 0) if asset_index == 0 else 0
        for page_number, text in chunks:
            for line_number, line in enumerate(text.splitlines(), 1):
                if needle in line.casefold():
                    matched_lines += 1
                    if matched_lines <= skip_matches:
                        continue
                    source = source_locator(asset, page=page_number)
                    matches.append({'text': line[:args.context_chars], 'line_number': line_number, 'name': asset.original_name,
                        'source': source, 'reference': {'kind': 'asset', 'workspace_id': ctx.workspace_id, 'object_id': asset.asset_id,
                                                      'revision': asset.revision, 'sha256': asset.blob_hash, 'application': 'platform'}})
                    if len(matches) >= args.limit:
                        next_asset_offset = asset_offset + asset_index
                        next_asset_match_offset = matched_lines
                        break
            if len(matches) >= args.limit:
                break
        if len(matches) >= args.limit:
            break
    else:
        if total > asset_offset + len(assets):
            next_asset_offset = asset_offset + len(assets)
    # Existing OCR result references remain authoritative; never re-run OCR as
    # a side effect of searching. Failed/stale outputs are explicitly omitted.
    task = ctx.services.agent.tasks.get(ctx.session_id)
    references = [*reversed((task.get('last_manual_change') or {}).get('references', [])),
                  *reversed(task.get('initial_refs', [])), *reversed(task['result_refs'])]
    from .manual import result_references
    from itertools import chain, islice
    unavailable = []
    def unique_ocr_references():
        discovered = (ref.model_dump(mode='json') for ref, _ in result_references(ctx, task, unavailable, only_ocr=True))
        seen = set()
        for ref in chain(references, discovered):
            if ref.get('application') != 'ocr' or ref.get('kind') != 'result':
                continue
            identity = (ref.get('parent_id'), ref['object_id'])
            if identity not in seen:
                seen.add(identity)
                yield ref
    ocr_offset = getattr(args, 'ocr_offset', 0)
    discovered = list(islice(unique_ocr_references(), ocr_offset, ocr_offset + 201))
    consumed_ocr = 0
    for ref in discovered[:200]:
        if len(matches) >= args.limit:
            break
        consumed_ocr += 1
        try:
            value = verify(ref, ctx)
            edited = value['snapshot']['edited']
            chunks = [edited.get('text', '')]
            for table in edited.get('tables', []):
                chunks.append(table.get('caption', ''))
                chunks.extend(cell.get('text', '') for cell in table.get('cells', []))
            text = '\n'.join(chunk for chunk in chunks if isinstance(chunk, str))
            position = text.casefold().find(needle)
            if position >= 0:
                member = value['source_asset']; asset = registry.get(ctx.workspace_id, member['asset_id'])
                matches.append({'text': text[max(0, position - 100):position + args.context_chars], 'name': asset.original_name,
                    'source': source_locator(asset, page=value['locator'].get('page_number')), 'reference': ref, 'kind': 'ocr'})
        except ValueError:
            skipped.append({'reference': ref, 'reason': 'OCR结果已修订或不可用'})
    next_ocr_offset = ocr_offset + consumed_ocr if consumed_ocr < len(discovered) else -1
    profile = ctx.services.agent.tasks.profile(ctx.workspace_id)
    if needle in profile['rules'].casefold() and len(matches) < args.limit:
        matches.append({'text': profile['rules'][:args.context_chars], 'kind': 'user_rule', 'profile_revision': profile['revision'],
                        'name': '用户保存的项目口径', 'source': None})
    return {'items': matches, 'coverage': {'inspected_files': inspected, 'candidate_files': len(assets), 'asset_page_total': total,
                'candidate_scope': 'selected_assets' if ctx.selected_assets else 'project_page',
                'file_limit': 200, 'candidate_files_truncated': next_asset_offset >= 0,
                'next_asset_offset': next_asset_offset, 'next_asset_match_offset': next_asset_match_offset,
                'next_ocr_offset': next_ocr_offset,
                'indexed_ocr_results': consumed_ocr, 'ocr_discovery_unavailable': len(unavailable),
                'limit': args.limit, 'limit_reached': len(matches) >= args.limit, 'complete': False}, 'skipped': skipped[:30],
            'scope': '原文逐行和已存在OCR结果的文字匹配；不包含未识别图片或语义向量检索'}
