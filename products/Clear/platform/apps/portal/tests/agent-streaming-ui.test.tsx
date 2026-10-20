import '@testing-library/jest-dom/vitest'
import { useMemo } from 'react'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AgentWorkbench } from '../src/features/agent/AgentWorkbench'
import { AgentMarkdown, AgentMessage } from '../src/features/agent/AgentMessage'
import { ToolActivity } from '../src/features/agent/ToolActivity'
import { RunStatus } from '../src/features/agent/RunStatus'
import { useAgentEvents } from '../src/features/agent/useAgentEvents'
import { useConversationScroll } from '../src/features/agent/useConversationScroll'
import { applyConversationEvents, createProjection, projectConversation, type MessageSegment } from '../src/features/agent/conversationProjection'
import type { AgentEvent, AgentRef } from '../src/features/agent/api'

vi.mock('../src/App', () => ({ useWorkspaces: () => ({ data: [{ workspace_id: 'workspace', name: '项目' }] }) }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); FakeSource.instances = [] })
const ref: AgentRef = { kind: 'job', application: 'data', workspace_id: 'workspace', object_id: 'job', parent_id: 'data-session', revision: 1, sources: [], coverage: {} }
const ev = (seq: number, kind: string, payload: Record<string, unknown>, run_id = 'run'): AgentEvent => ({ seq, kind, payload, run_id, created: '2026-10-05T00:00:00Z' })
const preview = (seq: number, revision: number, content: string, extra: Record<string, unknown> = {}) => ev(seq, 'response_preview', { version: 1, generation: 1, request_id: 'request', display_id: 'answer', revision, mode: 'snapshot', offset: 0, content, source: 'answer', provisional: true, ...extra })
const state = (seq: number, status: string, extra: Record<string, unknown> = {}) => ev(seq, 'response_state', { version: 1, generation: 1, request_id: 'request', display_id: 'answer', revision: 3, status, ...extra })
const messages = (events: AgentEvent[]) => projectConversation(events).items.filter((item): item is MessageSegment => item.type === 'message')

test('same goal resumes activity after failure while earlier unfinished prose stays interrupted', () => {
  const result = projectConversation([preview(1, 1, '尚未核实的比较'),
    ev(2, 'agent_activity', { version: 1, generation: 1, phase: 'failed' }),
    ev(3, 'run_resumed', { version: 1, generation: 1 }),
    ev(4, 'agent_activity', { version: 1, generation: 1, phase: 'model_wait' })])
  expect(result.activities.run.phase).toBe('model_wait')
  expect(result.items.find(item => item.type === 'message')).toMatchObject({ status: 'interrupted' })
})

test('original text is usable without a final model summary and does not claim goal completion', () => {
  const available = messages([ev(1, 'result_available', { version: 1, generation: 1, goal_complete: false,
    result: { result_refs: [], artifact_refs: [], data: { finalized: false,
      readable_text: [{ text: '编号 00001 金额 10.00', text_truncated: false }],
      delivery: [{ bundle_id: 'original', title: 'OCR 原文', files: [{ name: 'ocr-text.txt', download_url: '/download/original' }] }] } } })])[0]
  render(<MemoryRouter><AgentMessage item={available} /></MemoryRouter>)
  expect(screen.getByRole('heading', { name: '原文已完成' })).toBeVisible()
  expect(screen.getByText('编号 00001 金额 10.00')).toBeVisible()
  expect(screen.getByRole('link', { name: 'ocr-text.txt' })).toHaveAttribute('href', '/download/original')
  expect(screen.getByText(/后续分析待继续/)).toBeVisible()
})

test('a streamed answer is one stable segment through generated, confirmation and official references', () => {
  const initial = applyConversationEvents(createProjection(), [preview(1, 1, '开头'), preview(2, 2, '开头与结论', { references: [ref] }), state(3, 'generated')])
  const draft = initial.items[0] as MessageSegment
  expect(draft.content).toBe('开头与结论'); expect(draft.status).toBe('generated'); expect(draft.references).toEqual([])
  const final = applyConversationEvents(initial, [state(4, 'confirmed', { content: '正式结论', references: [ref] }), ev(5, 'message', { generation: 1, display_id: 'answer', role: 'assistant', content: '正式结论', references: [ref] }), preview(6, 4, '迟到正文')])
  expect(final.items).toHaveLength(1)
  expect(final.items[0]).toMatchObject({ id: draft.id, content: '正式结论', status: 'confirmed', references: [ref] })
})

test('Unicode append offsets, revision gaps and snapshot repair preserve exact content', () => {
  let projection = applyConversationEvents(createProjection(), [preview(1, 1, '😀中'), preview(2, 2, '文', { mode: 'append', offset: 2 })])
  expect((projection.items[0] as MessageSegment).content).toBe('😀中文')
  projection = applyConversationEvents(projection, [preview(3, 4, '错误', { mode: 'append', offset: 3 })])
  expect(projection.protocolIssue).toBeDefined(); expect((projection.items[0] as MessageSegment).content).toBe('😀中文')
  projection = applyConversationEvents(projection, [preview(4, 5, '😀中文完整', { truncated: true }), state(5, 'confirmed', { revision: 6, content: '完整正式回答' })])
  expect(projection.protocolIssue).toBeUndefined(); expect(projection.items[0]).toMatchObject({ content: '完整正式回答', truncated: false })
})

test('process text and delivery keep distinct identities while final attachments enrich the delivery segment', () => {
  const items = messages([preview(1, 1, '正在整理资料', { display_id: 'process', source: 'text' }), preview(2, 1, '候选交付', { display_id: 'delivery', source: 'delivery' }),
    ev(3, 'response_state', { version: 1, generation: 1, request_id: 'request', display_id: 'delivery', revision: 2, status: 'confirmed', content: '已形成成果' }),
    ev(4, 'delivery', { generation: 1, display_id: 'delivery', status: 'succeeded', content: '已形成成果', delivery: { artifact_refs: [], result_refs: [], data: { delivery: [{ bundle_id: 'bundle', title: '结果文件', files: [{ name: 'result.csv', download_url: '/api/download' }] }] } } })])
  expect(items).toHaveLength(2); expect(items[0].content).toBe('正在整理资料'); expect(items[1].delivery?.kind).toBe('delivery')
  render(<MemoryRouter><AgentMessage item={items[1]} /></MemoryRouter>)
  expect(screen.getAllByText('已形成成果')).toHaveLength(1); expect(screen.getByRole('heading', { name: '已完成', level: 3 })).toBeVisible(); expect(screen.getByRole('link', { name: 'result.csv' })).toHaveAttribute('href', '/api/download')
})

test('terminal generation fences block late previews and late old final messages', () => {
  const events = [preview(1, 1, '未确认'), ev(2, 'agent_activity', { version: 1, generation: 2, phase: 'stopped' }), preview(3, 2, '迟到'),
    ev(4, 'message', { generation: 1, display_id: 'answer', role: 'assistant', content: '迟到正式回答' }), ev(5, 'delivery', { generation: 1, display_id: 'answer', content: '旧交付' })]
  const projected = projectConversation([...events, events[0]])
  expect(projected.items).toHaveLength(1); expect(projected.items[0]).toMatchObject({ content: '未确认', status: 'interrupted', references: [] })
  expect(projected.activities.run.phase).toBe('stopped')
})

test('cancel fences update the original preview when interrupted state carries the new generation', () => {
  const projected = projectConversation([preview(1, 1, '取消前已收到的文字'), state(2, 'interrupted', { generation: 2, reason: '用户停止' }),
    ev(3, 'agent_activity', { version: 1, generation: 2, phase: 'stopped' }), preview(4, 5, '旧代际迟到片段')])
  expect(projected.items).toHaveLength(1)
  expect(projected.items[0]).toMatchObject({ content: '取消前已收到的文字', generation: 2, status: 'interrupted', reason: '用户停止' })
})

test('legacy deltas replace snapshots and unidentified historical messages are retained without text matching', () => {
  const items = messages([ev(1, 'model_delta', { message_id: 'old', content: '你好' }), ev(2, 'model_delta', { message_id: 'old', content: '你好，世界' }), ev(3, 'message', { message_id: 'old', role: 'assistant', content: '你好，世界' }),
    ev(4, 'message', { role: 'assistant', content: '相同旧正文' }), ev(5, 'message', { role: 'assistant', content: '相同旧正文' })])
  expect(items).toHaveLength(3); expect(items[0].content).toBe('你好，世界'); expect(items.filter(item => item.content === '相同旧正文')).toHaveLength(2)
})

test('one tool activity aggregates operation phases and real progress does not invent a percentage', () => {
  const projected = projectConversation([ev(1, 'tool_activity', { version: 1, generation: 1, call_id: 'call', operation_id: 'op', tool: 'data_compare', phase: 'started', status: 'running', summary: '核对台账' }),
    ev(2, 'tool_activity', { version: 1, generation: 1, call_id: 'call', operation_id: 'op', tool: 'data_compare', phase: 'pending', status: 'pending', job_refs: [ref] }),
    ev(3, 'job_progress', { generation: 1, job_ref: ref, status: 'running', stage: '核对数据', progress_done: 12, progress_total: null, counts: null }),
    ev(4, 'tool_activity', { version: 1, generation: 1, call_id: 'call', operation_id: 'op', tool: 'data_compare', phase: 'finished', status: 'success', summary: '核对完成' }),
    ev(5, 'tool_activity', { version: 1, generation: 1, call_id: 'call', operation_id: 'op', tool: 'data_compare', phase: 'pending', status: 'pending' })])
  const item = projected.items[0]
  expect(item.type).toBe('activity'); if (item.type !== 'activity') throw new Error('activity missing')
  expect(item.tools).toHaveLength(1); expect(item.tools[0]).toMatchObject({ phase: 'finished', status: 'success', operationId: 'op' })
  render(<MemoryRouter><ToolActivity item={item} /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: /已完成 1 项/ }))
  expect(screen.getByText('核对完成')).toBeVisible(); expect(screen.getByText(/已处理 12/)).toBeInTheDocument(); expect(screen.queryByText(/%/)).not.toBeInTheDocument()
})

test('unavailable native progress keeps its observation and does not show previous completion or counters as current', () => {
  const projected = projectConversation([ev(1, 'job_progress', { generation: 1, job_ref: ref, status: 'succeeded', stage: '完成', progress_done: 99, progress_total: 99, counts: { pages: 99 }, observation: 'native' }),
    ev(2, 'job_progress', { generation: 1, job_ref: ref, status: 'unknown', stage: '', progress_done: 99, counts: { pages: 99 }, observation: 'unavailable', availability: 'unreachable', has_error: false, sampled_at: '2026-10-05T00:00:00Z' })])
  const item = projected.items[0]; if (item.type !== 'activity') throw new Error('activity missing')
  expect(item.jobs[0]).toMatchObject({ observation: 'unavailable', availability: 'unreachable', hasError: false })
  render(<MemoryRouter><ToolActivity item={item} /></MemoryRouter>)
  expect(screen.getByText('状态暂不可核实')).toBeInTheDocument(); expect(screen.getByText(/尝试读取于/)).toBeInTheDocument()
  expect(screen.queryByText('完成')).not.toBeInTheDocument(); expect(screen.queryByText(/已处理 99|pages 99/)).not.toBeInTheDocument()
})

test('native manual pause displays the actual resume guidance and a later running observation clears it', () => {
  const stopped = projectConversation([ev(1, 'job_progress', { generation: 1, job_ref: ref, status: 'paused', needs_resume: true, resume_message: '原生作业已暂停，请在手动工具中恢复；Agent保持等待' })]).items[0]
  if (stopped.type !== 'activity') throw new Error('activity missing')
  const view = render(<MemoryRouter><ToolActivity item={stopped} /></MemoryRouter>)
  expect(screen.getByText('原生作业已暂停，请在手动工具中恢复；Agent保持等待')).toBeInTheDocument()
  const running = projectConversation([ev(1, 'job_progress', { generation: 1, job_ref: ref, status: 'paused', needs_resume: true }), ev(2, 'job_progress', { generation: 1, job_ref: ref, status: 'running' })]).items[0]
  if (running.type !== 'activity') throw new Error('activity missing')
  view.rerender(<MemoryRouter><ToolActivity item={running} /></MemoryRouter>)
  expect(screen.queryByText(/原生作业已暂停/)).not.toBeInTheDocument()
})

test('permission-blocked tools remain visibly unexecuted', () => {
  const item = projectConversation([ev(1, 'tool_activity', { version: 1, generation: 1, call_id: 'blocked', tool: 'data_export', phase: 'blocked', status: 'error', summary: 'Skill不允许此调用' })]).items[0]
  if (item.type !== 'activity') throw new Error('activity missing')
  render(<MemoryRouter><ToolActivity item={item} /></MemoryRouter>)
  expect(screen.getByText('未执行')).toBeInTheDocument(); expect(screen.queryByText('已完成')).not.toBeInTheDocument()
  expect(screen.getByText('1 项工具操作未完成，可展开查看原因。')).toBeInTheDocument()
})

test('explicit receipt reconciliation restores the same unknown tool step as completed', () => {
  const projected = projectConversation([ev(1, 'tool_activity', { version: 1, generation: 1, call_id: 'recover', operation_id: 'original', tool: 'data_export', phase: 'finished', status: 'needs_verification', summary: '原回执待核实' }),
    ev(2, 'tool_activity', { version: 1, generation: 1, call_id: 'recover', operation_id: 'original', tool: 'data_export', phase: 'restored', status: 'success', summary: '已核实原导出结果' }),
    ev(3, 'tool_activity', { version: 1, generation: 1, call_id: 'recover', operation_id: 'original', tool: 'data_export', phase: 'pending', status: 'pending' })])
  const item = projected.items[0]; if (item.type !== 'activity') throw new Error('activity missing')
  expect(item.tools).toHaveLength(1); expect(item.tools[0]).toMatchObject({ operationId: 'original', phase: 'restored', status: 'success' })
  render(<MemoryRouter><ToolActivity item={item} /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: /已完成 1 项/ }))
  expect(screen.getByText('已恢复 · 已完成')).toBeVisible(); expect(screen.getByText('工具过程 · 1 项，1 项完成')).toBeInTheDocument()
  expect(screen.queryByText(/项执行或等待/)).not.toBeInTheDocument()
})

test('Markdown GFM supports tables and unfinished code without running HTML or executable links', async () => {
  const writeText = vi.fn(async () => {})
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  render(<AgentMarkdown content={'# 核对结果\n\n| 列 | 金额 |\n|---|---:|\n| 付款 | 12 |\n\n<script>window.evil=1</script>\n\n[危险](javascript:alert(1))\n\n```python\nprint("中文")'} />)
  expect(screen.getByRole('heading', { name: '核对结果' })).toBeInTheDocument(); expect(screen.getByRole('table')).toBeInTheDocument()
  expect(document.querySelector('script')).toBeNull(); expect(document.querySelector('a[href^="javascript:"]')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '复制代码' })); await waitFor(() => expect(writeText).toHaveBeenCalledWith('print("中文")\n'))
})

test('run status animates only active facts and respects reduced motion', () => {
  const activity = { runId: 'run', generation: 1, phase: 'model_wait', created: new Date().toISOString(), seq: 1, payload: {} }
  const props = { activeStatus: 'running', submitting: false, subscription: { status: 'connected' as const, error: '' }, reconnect: () => {} }
  const view = render(<RunStatus {...props} activity={activity} />)
  expect(screen.getByRole('status')).toHaveTextContent('正在等待模型'); expect(document.querySelector('[data-animated="true"]')).toBeInTheDocument()
  view.rerender(<RunStatus {...props} activeStatus={undefined} taskStatus="cancelled" activity={{ ...activity, phase: 'stopped' }} />)
  expect(document.querySelector('[data-animated="true"]')).not.toBeInTheDocument()
  view.rerender(<RunStatus {...props} activeStatus={undefined} taskStatus="running" activity={{ ...activity, phase: 'completed' }} />)
  expect(screen.getByRole('status')).toHaveTextContent('本轮已结束'); expect(document.querySelector('[data-animated="true"]')).not.toBeInTheDocument()
  view.unmount()
  const prior = window.matchMedia
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  render(<RunStatus {...props} activity={activity} />)
  expect(screen.getByRole('status')).toHaveTextContent('正在等待模型'); expect(document.querySelector('[data-reduced-motion="true"]')).toBeInTheDocument(); expect(document.querySelector('[data-animated="true"]')).not.toBeInTheDocument()
  window.matchMedia = prior
})

class FakeSource {
  static instances: FakeSource[] = []
  readyState = 0; onmessage: ((event: { data: string }) => void) | null = null; onopen: (() => void) | null = null; onerror: (() => void) | null = null
  closed = false
  constructor(public url: string) { FakeSource.instances.push(this) }
  close() { this.closed = true; this.readyState = 2 }
  emit(event: AgentEvent) { this.onmessage?.({ data: JSON.stringify(event) }) }
}
function Subscription({ session = 'session' }: { session?: string }) {
  const { projection, subscription, reconnect } = useAgentEvents('workspace', session, true)
  return <div><span data-testid="connection">{subscription.status}</span><button onClick={reconnect}>重连</button><span data-testid="events">{projection.events.map(event => event.seq).join(',')}</span>{projection.items.filter((item): item is MessageSegment => item.type === 'message').map(item => <p key={item.id}>{item.content}</p>)}</div>
}
test('SSE reconnect catches up read-only, deduplicates and receives final usage after a terminal event', async () => {
  vi.stubGlobal('EventSource', FakeSource)
  const requests: string[] = [], final = ev(4, 'usage', { input_tokens: 30, output_tokens: 5 })
  let resumed = false
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => { requests.push(input); expect(init?.method).toBeUndefined(); return { ok: true, json: async () => ({ items: resumed ? [state(2, 'confirmed', { content: '确认答复' }), ev(3, 'agent_activity', { version: 1, generation: 1, phase: 'completed' }), final] : [preview(1, 1, '草稿')], next_cursor: resumed ? 4 : 1 }) } }))
  render(<Subscription />)
  await waitFor(() => expect(FakeSource.instances).toHaveLength(1)); const source = FakeSource.instances[0]
  act(() => { source.onopen?.(); source.emit(preview(1, 1, '草稿')); source.onerror?.() })
  expect(screen.getByTestId('connection')).toHaveTextContent('reconnecting')
  resumed = true; act(() => source.onopen?.())
  expect(await screen.findByText('确认答复')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByTestId('events')).toHaveTextContent('1,2,3,4'))
  expect(requests.some(url => url.includes('after=1'))).toBe(true); expect(source.closed).toBe(false)
})

test('switching sessions closes the old stream and ignores its late frames while retaining each history', async () => {
  vi.stubGlobal('EventSource', FakeSource)
  vi.stubGlobal('fetch', vi.fn(async (input: string) => ({ ok: true, json: async () => ({ items: [ev(input.includes('/second/') ? 20 : 10, 'message', { role: 'user', content: input.includes('/second/') ? '第二会话' : '第一会话' })], next_cursor: input.includes('/second/') ? 20 : 10 }) })))
  const view = render(<Subscription session="first" />)
  expect(await screen.findByText('第一会话')).toBeInTheDocument(); await waitFor(() => expect(FakeSource.instances).toHaveLength(1))
  const old = FakeSource.instances[0], late = old.onmessage
  view.rerender(<Subscription session="second" />)
  expect(await screen.findByText('第二会话')).toBeInTheDocument(); expect(old.closed).toBe(true)
  act(() => late?.({ data: JSON.stringify(ev(11, 'message', { role: 'user', content: '错误会话内容' })) }))
  expect(screen.queryByText('错误会话内容')).not.toBeInTheDocument(); expect(screen.queryByText('第一会话')).not.toBeInTheDocument()
  view.rerender(<Subscription session="first" />)
  expect(await screen.findByText('第一会话')).toBeInTheDocument(); expect(screen.queryByText('第二会话')).not.toBeInTheDocument()
})

function ScrollReading({ scope, items, stamp }: { scope: string; items: string[]; stamp: number }) {
  const ids = useMemo(() => items, [items]), scroll = useConversationScroll(scope, ids, stamp)
  return <><div data-testid="scroll" ref={scroll.scroller} onScroll={scroll.onScroll}><div ref={scroll.content}>{items.map(item => <div data-timeline-id={item} key={item}>{item}</div>)}</div></div><span data-testid="following">{String(scroll.following)}</span><span data-testid="unread">{scroll.unread}</span><button onClick={scroll.jumpToLatest}>回到最新</button></>
}
test('scroll follow pauses on reading, counts messages once and restores per-session position', () => {
  const view = render(<ScrollReading scope="first" items={['one']} stamp={1} />), scroller = screen.getByTestId('scroll')
  Object.defineProperties(scroller, { scrollHeight: { configurable: true, value: 1000 }, clientHeight: { configurable: true, value: 200 } })
  scroller.scrollTop = 800; fireEvent.scroll(scroller); expect(screen.getByTestId('following')).toHaveTextContent('true')
  scroller.scrollTop = 180; fireEvent.scroll(scroller)
  view.rerender(<ScrollReading scope="first" items={['one', 'two']} stamp={2} />)
  expect(scroller.scrollTop).toBe(180); expect(screen.getByTestId('unread')).toHaveTextContent('1')
  view.rerender(<ScrollReading scope="first" items={['one', 'two']} stamp={3} />); expect(screen.getByTestId('unread')).toHaveTextContent('1')
  view.rerender(<ScrollReading scope="second" items={['other']} stamp={4} />)
  view.rerender(<ScrollReading scope="first" items={['one', 'two']} stamp={5} />)
  expect(scroller.scrollTop).toBe(180); expect(screen.getByTestId('following')).toHaveTextContent('false')
  fireEvent.click(screen.getByRole('button', { name: '回到最新' })); expect(screen.getByTestId('following')).toHaveTextContent('true')
})

test('manual takeover unknown replies remain recoverable with the original identity after server state changes', async () => {
  vi.stubGlobal('EventSource', FakeSource)
  let manual = false, failed = false; const controls: Record<string, unknown>[] = []
  const task = () => ({ id: 'session', session_id: 'session', workspace_id: 'workspace', title: '审计', goal: '核对', revision: manual ? 2 : 1, scope_revision: 1, status: manual ? 'manual' : 'running', manual, pending_scope: null, selected_assets: [], initial_refs: [], plan: [], runs: [], result_refs: [], artifact_refs: [], job_refs: [] })
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (input.endsWith('/task/control')) { controls.push(JSON.parse(String(init?.body))); manual = true; if (!failed) { failed = true; throw new Error('控制回包未知') } return { ok: true, json: async () => task() } }
    return { ok: true, json: async () => input.endsWith('/health') ? { enabled: true } : input.endsWith('/connection') ? { available: true, model: 'test' } : input.endsWith('/task') ? task() : input.includes('/events?') ? { items: [], next_cursor: 0 } : input.endsWith('/agent/sessions/session') ? { id: 'session', workspace_id: 'workspace', runs: manual ? [] : [{ id: 'run', status: 'running' }] } : input.includes('/audit-tasks?') ? { items: [] } : input.includes('/manual-results') ? { items: [], next_offset: null, unavailable: [] } : input.includes('/native-results') ? { items: [], links: [], next_offset: null } : {} }
  }))
  const router = createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }], { initialEntries: ['/?workspace=workspace&session=session'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  const composer = document.querySelector('.agent-composer') as HTMLElement
  fireEvent.click(within(composer).getByText('更多'))
  fireEvent.click(await screen.findByRole('button', { name: '手动接管' }))
  expect(await screen.findByText('控制回包未知')).toBeInTheDocument()
  const retry = await within(composer).findByRole('button', { name: '核实原接管操作' })
  await waitFor(() => expect(retry).toBeEnabled()); fireEvent.click(retry)
  await waitFor(() => expect(controls).toHaveLength(2)); expect(controls[0]).toEqual(controls[1]); expect(controls[0]).toMatchObject({ expected_revision: 1, action: 'take_over' })
  await waitFor(() => expect(sessionStorage.getItem('clear-agent-control:session')).toBeNull())
})

test('tool elapsed time uses recorded start and completion, leaving pending and unobserved duration unknown', () => {
  const started = { ...ev(1, 'tool_activity', { version: 1, generation: 1, call_id: 'timed', tool: 'data_compare', phase: 'started', status: 'running', summary: '核对台账' }), created: '2026-10-05T00:00:00Z' }
  const finished = { ...ev(2, 'tool_activity', { version: 1, generation: 1, call_id: 'timed', tool: 'data_compare', phase: 'finished', status: 'success', summary: '核对完成' }), created: '2026-10-05T00:00:04Z' }
  const pending = projectConversation([started]).items[0]
  const completed = projectConversation([started, finished]).items[0]
  const unobserved = projectConversation([finished]).items[0]
  if (pending.type !== 'activity' || completed.type !== 'activity' || unobserved.type !== 'activity') throw new Error('activity missing')
  const view = render(<MemoryRouter><ToolActivity item={pending} /></MemoryRouter>)
  expect(screen.getByText('正在执行')).toBeVisible(); expect(screen.queryByText(/用时/)).not.toBeInTheDocument()
  view.rerender(<MemoryRouter><ToolActivity item={completed} /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: /已完成 1 项/ }))
  expect(screen.getByText('用时 4 秒')).toBeVisible(); expect(screen.getByText('核对完成')).toBeVisible()
  view.rerender(<MemoryRouter><ToolActivity item={unobserved} /></MemoryRouter>)
  expect(screen.getByText('核对完成')).toBeVisible(); expect(screen.queryByText(/用时/)).not.toBeInTheDocument()
})
