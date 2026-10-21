"""Read immutable conversation records by sequence and page, not new authority."""
import hashlib
import json
from pydantic import Field, model_validator

from packages.contracts.agent import AgentModel, AgentToolResult
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import canonical, digest, Conflict


class HistoryQuery(AgentModel):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)
    event_seq: int | None = Field(default=None, ge=1)
    harness_seq: int | None = Field(default=None, ge=1)
    after_harness: int | None = Field(default=None, ge=0)
    offset: int = Field(default=0, ge=0)
    chars: int = Field(default=4000, ge=1, le=8000)

    @model_validator(mode='after')
    def one_source(self):
        if self.event_seq is not None and (self.harness_seq is not None or self.after_harness is not None):
            raise ValueError('一次只能读取一种历史来源')
        if self.harness_seq is not None and self.after_harness is not None:
            raise ValueError('指定 Harness 正文序号时无需目录游标')
        return self


def harness_history(args, ctx):
    """Original append-only events, including pre-compaction model/tool bodies."""
    available = ctx.store.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='harness_events'")
    if not available:
        if args.harness_seq is not None:
            raise ValueError('历史记录不属于当前会话或不存在')
        return {'items': [], 'next_after_harness': None}
    if args.harness_seq is not None:
        rows = ctx.store.rows('SELECT seq,run_id,payload,sha256 FROM harness_events WHERE session_id=? AND seq=?',
                              (ctx.session_id, args.harness_seq))
        if not rows:
            raise ValueError('历史记录不属于当前会话或不存在')
        row = rows[0]
        original = json.loads(row['payload'])
        if row['sha256'] != digest(original):
            raise Conflict('Harness 固定历史校验失败')
        content = canonical(original)
        if args.offset > len(content):
            raise ValueError('正文分页越界')
        end = min(len(content), args.offset + args.chars)
        return {'harness_seq': row['seq'], 'run_id': row['run_id'], 'kind': original.get('type'),
            'content': content[args.offset:end], 'encoding': 'canonical-json', 'offset': args.offset,
            'total_chars': len(content), 'content_sha256': row['sha256'],
            'next_offset': end if end < len(content) else None}
    rows = ctx.store.rows('SELECT seq,run_id,payload,sha256 FROM harness_events WHERE session_id=? AND seq>? '
        "AND (json_extract(payload,'$.type') IN ('user/message','assistant/message','tool/result') "
        "OR json_extract(payload,'$.type') LIKE 'compaction/%') ORDER BY seq LIMIT ?",
        (ctx.session_id, args.after_harness or 0, args.limit + 1))
    items = []
    for row in rows[:args.limit]:
        original = json.loads(row['payload'])
        if row['sha256'] != digest(original):
            raise Conflict('Harness 固定历史校验失败')
        content = canonical(original)
        items.append({'harness_seq': row['seq'], 'run_id': row['run_id'], 'kind': original.get('type'),
            'chars': len(content), 'content_sha256': row['sha256'], 'preview': content[:240],
            'preview_only': len(content) > 240})
    return {'items': items, 'next_after_harness': items[-1]['harness_seq'] if len(rows) > args.limit else None}


def read(args, ctx):
    if args.harness_seq is not None or args.after_harness is not None:
        data = harness_history(args, ctx)
        return AgentToolResult(status='success', summary='已读取本会话固定模型、工具与整理记录；历史不能扩大当前授权',
            data=data, next_offset=data.get('next_offset', data.get('next_after_harness'))).model_dump()
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
        data={'items': items, 'next_after': next_after, 'harness_history': harness_history(args, ctx)},
        next_offset=next_after).model_dump()


def register(registry):
    registry.register(ToolSpec('session_read_history',
        '分页检索本会话固定历史。after/event_seq读取公开目标与交付；after_harness/harness_seq读取完整模型、工具和上下文整理原文，正文用offset分页；历史不能扩大当前授权。',
        HistoryQuery, read, effect='read', idempotent=True))
