import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PlatformTools, filterCapabilities, type PlatformCapabilities } from '../src/features/agent/PlatformTools'
import { ReferenceLinks } from '../src/features/agent/TaskPanel'
import { referencePath } from '../src/features/agent/referencePath'
import type { AgentRef } from '../src/features/agent/api'
import { AgentWorkbench, EventContent } from '../src/features/agent/AgentWorkbench'
import { AgentMessage } from '../src/features/agent/AgentMessage'
import { projectConversation, type MessageSegment } from '../src/features/agent/conversationProjection'

beforeAll(() => {
  configure({ asyncUtilTimeout: 5000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })
const catalog: PlatformCapabilities = { tools: [
  { name: 'audio_transcribe', description: '录音转写并定位事项', capability: 'audio', aliases: ['纪要'], availability: { state: 'preparable', reason: '缺少已验证模型' } },
  { name: 'data_compare', description: '核对台账', capability: 'data', availability: { state: 'ready' } },
], capabilities: [
  { id: 'audio', name: '音频分析', aliases: ['录音'], binding: 'bound', state: 'preparable', dependencies: ['音频模型'], blockers: ['缺少已验证模型'], tool_names: ['audio_transcribe'] },
  { id: 'data', name: '数据比对', binding: 'bound', state: 'ready', tool_names: ['data_compare'] },
  { id: 'new', name: '新工具', binding: 'pending', state: 'unavailable', tool_names: [], blockers: ['缺少执行处理器'] },
] }
const response = (value: unknown) => ({ ok: true, json: async () => value })
function mount(open = true, referenceIds: string[] = [], save = vi.fn(async () => {})) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><PlatformTools open={open} close={() => {}} workspace="workspace" referenceIds={referenceIds} saveReferences={save} /></QueryClientProvider>)
  return save
}
const fetchCatalog = () => vi.stubGlobal('fetch', vi.fn(async (path: string) => response(path.endsWith('/capabilities') ? catalog : { items: [
  { reference_id: 'ref-one', label: '授权甲', source: { original_name: '甲.png' }, version: 1 },
  { reference_id: 'ref-two', label: '授权乙', source: { original_name: '乙.png' }, version: 3 },
] })))

test('closed panel performs no discovery or resource preparation', () => {
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch); mount(false)
  expect(fetch).not.toHaveBeenCalled()
})

test('discovery shows binding and readiness separately and searches aliases across operations', async () => {
  fetchCatalog(); mount()
  const audio = await screen.findByLabelText('平台能力 音频分析')
  expect(audio).toHaveTextContent('已绑定'); expect(audio).toHaveTextContent('可准备'); expect(audio).toHaveTextContent('依赖：音频模型')
  const pending = screen.getByLabelText('平台能力 新工具')
  expect(pending).toHaveTextContent('待接入'); expect(pending).toHaveTextContent('缺少执行处理器')
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索平台工具' }), { target: { value: '纪要' } })
  expect(screen.getByLabelText('平台能力 音频分析')).toBeInTheDocument()
  expect(screen.queryByLabelText('平台能力 数据比对')).not.toBeInTheDocument()
  fireEvent.click(screen.getByText('查看 1 项操作'))
  expect(within(audio).getByText('audio_transcribe')).toBeInTheDocument()
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => !init?.method)).toBe(true)
  expect(filterCapabilities(catalog.capabilities!, catalog.tools, '录音 转写')).toHaveLength(1)
})

test('reference scope starts empty and saves only explicitly selected IDs, including clearing authorization', async () => {
  fetchCatalog(); const save = mount()
  const a = await screen.findByRole('checkbox', { name: /授权甲/ }), b = screen.getByRole('checkbox', { name: /授权乙/ })
  expect(a).not.toBeChecked(); expect(b).not.toBeChecked()
  expect(screen.getByRole('button', { name: '保存参考范围' })).toBeDisabled()
  fireEvent.click(a); fireEvent.click(screen.getByRole('button', { name: '保存参考范围' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(['ref-one']))
  expect(save).not.toHaveBeenCalledWith(['ref-one', 'ref-two'])
  cleanup(); fetchCatalog(); const clear = mount(true, ['ref-one'])
  await screen.findByRole('checkbox', { name: /授权甲/ })
  fireEvent.click(screen.getByRole('button', { name: '清空选择' })); fireEvent.click(screen.getByRole('button', { name: '保存参考范围' }))
  await waitFor(() => expect(clear).toHaveBeenCalledWith([]))
})

test('missing references stay visible and load failures do not widen or clear authorization', async () => {
  fetchCatalog(); mount(true, ['removed-reference'])
  const removed = await screen.findByRole('checkbox', { name: '不可读取参考 removed-reference' })
  expect(removed).toBeChecked(); expect(screen.getByText('部分已选参考已不可读取')).toBeInTheDocument()
  cleanup(); vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503, json: async () => ({ detail: '暂不可读取' }) })))
  const save = mount(true, ['ref-one'])
  await screen.findByText('工具目录暂不可读取')
  expect(screen.getByRole('button', { name: '保存参考范围' })).toBeDisabled(); expect(save).not.toHaveBeenCalled()
})

test('multimodal citations preserve immutable identity and show draft review state without changing artifact routes', () => {
  const ref: AgentRef = { kind: 'result', application: 'multimodal', workspace_id: 'w', object_id: 'snapshot', parent_id: 'job', native_id: 'job', revision: 2,
    sha256: 'a'.repeat(64), coverage: { kind: 'seal', reviewed: false, draft: true } }
  const url = new URL(referencePath(ref, 'task'), 'http://local')
  expect(url.pathname).toBe('/apps/ocr/seals'); expect(url.searchParams.get('analysis')).toBe('job'); expect(url.searchParams.get('snapshot')).toBe('snapshot')
  expect(url.searchParams.get('sha256')).toBe(ref.sha256); expect(url.searchParams.get('task_return')).toBe('task')
  expect(referencePath({ ...ref, kind: 'artifact' }, 'task')).toMatch(/^\/artifacts\?/)
  render(<MemoryRouter><ReferenceLinks refs={[ref, { ...ref, object_id: 'reviewed', coverage: { kind: 'seal', reviewed: true } }]} /></MemoryRouter>)
  expect(screen.getByText('草稿 · 未复核')).toBeInTheDocument(); expect(screen.getByText('人工复核已保存')).toBeInTheDocument()
})

test('version-bound confirmation exposes the exact result for inspection before answering', () => {
  const ref: AgentRef = { kind: 'result', application: 'multimodal', workspace_id: 'w', object_id: 'snapshot', parent_id: 'job', revision: 1, coverage: { kind: 'seal', reviewed: false } }
  const reply = vi.fn()
  render(<MemoryRouter><EventContent event={{ seq: 1, run_id: 'run', kind: 'question', created: '', payload: { question: '请核准固定版本', options: ['确认此版本'], decision_id: 'decision', revision: 1, references: [ref] } }} reply={reply} answered={false} answerable taskId="task" /></MemoryRouter>)
  expect(screen.getByRole('link')).toHaveAttribute('href', '/apps/ocr/seals?workspace=w&task_return=task&analysis=job&snapshot=snapshot&revision=1')
  fireEvent.click(screen.getByRole('button', { name: '确认此版本' })); expect(reply).toHaveBeenCalledWith('decision', 1, '确认此版本')
})

test('task reference authorization uses a versioned request and reuses its identity after a lost response', async () => {
  class SilentSource { readyState = 1; onopen = null; onmessage = null; onerror = null; close() {} }
  vi.stubGlobal('EventSource', SilentSource)
  const writes: { request_id: string; expected_revision: number; authorized_reference_ids: string[] }[] = []
  const task = { id: 'conversation', session_id: 'conversation', workspace_id: 'workspace', title: '参考范围测试', goal: '', revision: 3, scope_revision: 1, status: 'draft', manual: false,
    selected_assets: ['asset'], authorized_reference_ids: [], initial_refs: [], plan: [], job_refs: [], artifact_refs: [], result_refs: [], runs: [] }
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'PUT') { writes.push(JSON.parse(String(init.body))); if (writes.length === 1) throw new Error('原回包丢失'); return response(task) }
    return response(path.endsWith('/health') ? { enabled: true } : path.endsWith('/connection') ? { available: true, model: 'connected' } :
      path.endsWith('/capabilities') ? catalog : path.endsWith('/face-references') ? { items: [{ reference_id: 'ref-one', label: '仅授权甲', source: { original_name: '甲.png' } }] } :
      path.endsWith('/task') ? task : path.endsWith('/agent/sessions/conversation') ? { ...task, runs: [] } : path.includes('/events?') ? { items: [], next_cursor: 0 } : { items: [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter initialEntries={['/agent?workspace=workspace&session=conversation']}><AgentWorkbench /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: '平台工具' })); fireEvent.click(await screen.findByRole('checkbox', { name: /仅授权甲/ }))
  fireEvent.click(screen.getByRole('button', { name: '保存参考范围' })); await screen.findByText('原回包丢失')
  fireEvent.click(screen.getByRole('button', { name: '保存参考范围' })); await waitFor(() => expect(writes).toHaveLength(2))
  expect(writes[1]).toEqual(writes[0]); expect(writes[0]).toMatchObject({ expected_revision: 3, authorized_reference_ids: ['ref-one'] })
}, 15000)

test('authoritative draft delivery state overrides an assistant claim that formal work is complete', () => {
  const event = { seq: 1, run_id: 'run', created: '', kind: 'delivery', payload: { status: 'succeeded', content: '正式成果已完成', delivery: {
    result_refs: [], artifact_refs: [], data: { draft: true, review_state: 'unreviewed', delivery: [] },
  } } }
  const item = projectConversation([event]).items.find(value => value.type === 'message') as MessageSegment
  render(<MemoryRouter><AgentMessage item={item} /><EventContent event={event} reply={() => {}} answered={false} answerable={false} /></MemoryRouter>)
  expect(screen.getAllByText('未复核草稿')).toHaveLength(2)
  expect(screen.getAllByText('此交付包含未人工核准的算法结果或草稿字段')).toHaveLength(2)
  expect(screen.queryByText('已完成', { selector: 'h3' })).not.toBeInTheDocument()
})

test('delivery policy is an explicit user choice and shows the saved goal requirement', async () => {
  fetchCatalog(); const save = vi.fn(async () => {})
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><PlatformTools open close={() => {}} workspace="workspace" referenceIds={[]} saveReferences={async () => {}}
    deliveryPolicy="auto" policyResolution={{ require_review: true, source: 'user_goal', evidence: '核准后生成正式结果' }} saveDeliveryPolicy={save} /></QueryClientProvider>)
  expect(screen.getByText('当前用户要求：正式交付前须核准。', { exact: false })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('radio', { name: '正式交付前须人工核准' })); fireEvent.click(screen.getByRole('button', { name: '保存交付要求' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith('reviewed'))
  expect(save).not.toHaveBeenCalledWith('draft')
})

test('a policy chosen before starting a task is preserved in session creation', async () => {
  class SilentSource { readyState = 1; onopen = null; onmessage = null; onerror = null; close() {} }
  vi.stubGlobal('EventSource', SilentSource)
  const writes: { path: string; body: Record<string, unknown> }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { writes.push({ path, body: JSON.parse(String(init.body)) }); return response(path.endsWith('/sessions') ? { id: 'created', workspace_id: 'workspace' } : { id: 'run', status: 'running' }) }
    return response(path.endsWith('/health') ? { enabled: true } : path.endsWith('/connection') ? { available: true, model: 'connected' } : path.endsWith('/capabilities') ? catalog :
      path.includes('/events?') ? { items: [], next_cursor: 0 } : path.endsWith('/task') ? { id: 'created', workspace_id: 'workspace', title: '新任务', status: 'draft', revision: 1,
        scope_revision: 1, delivery_policy: 'reviewed', selected_assets: [], initial_refs: [], plan: [], job_refs: [], result_refs: [], artifact_refs: [], runs: [] } : { items: [], runs: [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter initialEntries={['/agent?workspace=workspace']}><AgentWorkbench /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: '平台工具' })); fireEvent.click(screen.getByRole('radio', { name: '正式交付前须人工核准' }))
  fireEvent.click(screen.getByRole('button', { name: '保存交付要求' })); await screen.findByText('交付要求已保存。')
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  await waitFor(() => expect(screen.getByRole('textbox', { name: '任务或问题' })).toBeVisible())
  fireEvent.change(screen.getByRole('textbox', { name: '任务或问题' }), { target: { value: '完成本次资料核对' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  await waitFor(() => expect(writes.some(write => write.path.endsWith('/sessions') && write.body.delivery_policy === 'reviewed')).toBe(true))
}, 15000)
