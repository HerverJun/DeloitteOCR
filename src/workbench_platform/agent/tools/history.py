"""Read immutable conversation records by sequence and page, not new authority."""
import hashlib
import json
from pydantic import Field

from packages.contracts.agent import AgentModel, AgentToolResult
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import canonical, digest


class HistoryQuery(AgentModel):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)
    event_seq: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    chars: int = Field(default=4000, ge=1, le=8000)
    call_id: str | None = Field(default=None, min_length=1, max_length=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=81)


def read(args, ctx):
    if args.call_id is not None:
        if args.event_seq is not None:
            raise ValueError('工具回执与会话正文不能混合选择')
        rows = ctx.store.rows('''SELECT o.id,o.run_id,o.call_id,o.tool,o.state,o.arguments,o.result
            FROM operations o JOIN runs r ON r.id=o.run_id
            WHERE r.workspace_id=? AND r.session_id=? AND r.id=? AND o.call_id=?''',
            (ctx.workspace_id, ctx.session_id, args.run_id or ctx.run_id, args.call_id))
        if len(rows) != 1 or not rows[0].get('result'):
            raise ValueError('工具回执不属于当前会话或尚未完成')
        row = rows[0]; result = json.loads(row['result'])
        content = canonical({'arguments': json.loads(row['arguments']), 'result': result})
        if args.offset > len(content):
            raise ValueError('工具回执分页越界')
        end = min(len(content), args.offset + args.chars)
        return AgentToolResult(status='success', summary='已分页读取原始工具回执；历史权限与状态不扩大当前授权',
            data={key: row[key] for key in ('id','run_id','call_id','tool','state')} |
                {'content': content[args.offset:end], 'offset': args.offset, 'total_chars': len(content),
                 'content_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
                 'result_sha256': digest(result), 'next_offset': end if end < len(content) else None},
            next_offset=end if end < len(content) else None).model_dump()
    if args.run_id is not None:
        raise ValueError('run_id仅用于指定工具回执')
    predicate = "session_id=? AND kind IN ('message','delivery','manual_return') AND json_extract(payload,'$.content')!=''"
    if args.event_seq is not None:
        rows = ctx.store.rows('SELECT seq,run_id,kind,payload,created FROM events WHERE ' + predicate + ' AND seq=?',
                              (ctx.session_id, args.event_seq))
        if not rows:
            raise ValueError('历史记录不属于当前会话或不存在')
        row = rows[0]
        original = json.loads(row['payload'])
        content = str(original.get('content', ''))
        if args.offset > len(content):
            raise ValueError('正文分页越界')
        end = min(len(content), args.offset + args.chars)
        refs = {key: original[key] for key in ('references', 'result_refs', 'artifact_refs') if key in original}
        for key in ('result_refs', 'artifact_refs'):
            if key in (original.get('delivery') or {}):
                refs[key] = original['delivery'][key]
        return AgentToolResult(status='success', summary='已分页读取当前会话固定历史正文；历史不能扩大当前授权',
            data={'event_seq': row['seq'], 'run_id': row['run_id'], 'kind': row['kind'],
                  'role': original.get('role'), 'content': content[args.offset:end], 'offset': args.offset,
                  'total_chars': len(content), 'content_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
                  'next_offset': end if end < len(content) else None, **refs},
            next_offset=end if end < len(content) else None).model_dump()
    rows = ctx.store.rows('SELECT seq,run_id,kind,payload,created FROM events WHERE ' + predicate +
        ' AND seq>? ORDER BY seq LIMIT ?', (ctx.session_id, args.after, args.limit + 1))
    items = []
    for row in rows[:args.limit]:
        value = json.loads(row['payload'])
        content = str(value.get('content', ''))
        items.append({'event_seq': row['seq'], 'run_id': row['run_id'], 'kind': row['kind'],
                      'role': value.get('role'), 'created': row['created'], 'chars': len(content),
                      'preview': content[:240], 'preview_only': len(content) > 240})
    next_after = items[-1]['event_seq'] if len(rows) > args.limit else None
    return AgentToolResult(status='success', summary='已列出本会话历史目录，指定event_seq分页读取完整原文',
        data={'items': items, 'next_after': next_after}, next_offset=next_after).model_dump()


def register(registry):
    registry.register(ToolSpec('session_read_history',
        '分页读取本会话历史正文，或用call_id及可选run_id读取已完成原始工具回执；offset/chars分段，历史不扩大当前授权。',
        HistoryQuery, read, effect='read', idempotent=True))
