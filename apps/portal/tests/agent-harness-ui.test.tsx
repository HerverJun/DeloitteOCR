import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { ControllerSettings } from '../src/features/agent/ControllerSettings'
import { compactionNotice, ContextBudgetContent, ContextCompactionContent, DeliveryCheckContent, ModelUsageContent } from '../src/features/agent/ContextBudget'
import { AgentWorkbench } from '../src/features/agent/AgentWorkbench'
import { projectConversation, terminalActivity } from '../src/features/agent/conversationProjection'
import type { AgentEvent, ModelProfile } from '../src/features/agent/api'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })
function mount(element: React.ReactNode, url?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const content = url ? <RouterProvider router={createMemoryRouter([{ path: '*', element }], { initialEntries: [url] })} /> : element
  return render(<QueryClientProvider client={client}>{content}</QueryClientProvider>)
}
const profile: ModelProfile = { version: 'clear-model-profile-v1', model: 'gateway-alias', protocol: 'openai_chat_completions', context_window: null,
  model_max_output_tokens: null, capacity_sources: { context_window: 'unknown', max_output_tokens: 'unknown' }, capacity_verified: false,
  runtime_context_cap: 65536, effective_context_window: 65536, output_reserve: 2048, safety_margin_tokens: 2048, available_input_tokens: 61440, connection_revision: 2 }
const connection = { configured: true, available: true, protocol: 'openai_chat_completions', base_url: 'http://127.0.0.1:9988/v1', model: 'gateway-alias',
  context_cap: 65536, runtime_context_cap: 65536, max_output_tokens: 2048, total_seconds: 180, streaming: true, model_profile: profile }
const response = (value: unknown) => ({ ok: true, json: async () => value })

test('idle and active manual compaction receipts report completed versus queued work accurately', () => {
  expect(compactionNotice({ queued: true })).toBe('已安排在下一次模型请求前整理上下文。')
  expect(compactionNotice({ queued: false, compacted: true })).toBe('上下文已整理，完整历史与来源保留。')
  expect(compactionNotice({ queued: false, compacted: false })).toBe('当前没有需要整理的较早历史。')
})

test('unknown model capacity is distinct from the local ceiling and missing usage remains unknown', () => {
  mount(<><ContextBudgetContent budget={{ model_profile: profile, context_cap: 65536, input_tokens_estimate: 50000, output_reserve: 2048, safety_margin_tokens: 2048, available_input_tokens: 61440 }} /><ModelUsageContent payload={{}} /></>)
  const budget = screen.getByLabelText('模型请求上下文预算')
  expect(budget).toHaveTextContent('模型容量：未知 · 尚无容量声明')
  expect(budget).toHaveTextContent('本地上限 65,536 · 有效窗口 65,536 tokens')
  expect(budget).toHaveTextContent('合计 54,096 tokens（含安全余量 2,048）')
  expect(screen.getByLabelText('输入预算占用')).toHaveTextContent('已达到自动整理阈值')
  expect(screen.getByLabelText('服务返回实际用量')).toHaveTextContent('输入 未提供 tokens · 输出 未提供 tokens')
})

test('model metadata is adopted without converting it into a manual capacity declaration and budgets default unlimited', async () => {
  let submitted: Record<string, unknown> | undefined
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (input.endsWith('/models')) return response({ models: [{ id: 'gateway-alias', context_window: 1048576, max_output_tokens: 65536, source: 'endpoint_metadata' }] })
    if (init?.method === 'PUT') submitted = JSON.parse(String(init.body))
    return response(connection)
  }))
  mount(<ControllerSettings open close={() => {}} />)
  await screen.findByDisplayValue('gateway-alias')
  expect(screen.getByLabelText('模型能力来源')).toHaveTextContent('上下文容量：未知')
  fireEvent.click(screen.getByRole('button', { name: '读取服务模型列表' }))
  await waitFor(() => expect(screen.getByLabelText('模型能力来源')).toHaveTextContent('上下文容量：1,048,576 tokens · 服务元数据'))
  fireEvent.click(screen.getByText('高级连接参数'))
  const local = screen.getByRole('spinbutton', { name: '本地上下文预算' }), output = screen.getByRole('spinbutton', { name: '单次输出上限' })
  expect(local).not.toHaveAttribute('max'); expect(output).not.toHaveAttribute('max')
  fireEvent.change(local, { target: { value: '524288' } })
  fireEvent.change(output, { target: { value: '32768' } })
  expect(screen.getByRole('spinbutton', { name: '模型轮数上限' })).toHaveDisplayValue('')
  expect(screen.getByRole('spinbutton', { name: '工具调用次数上限' })).toHaveDisplayValue('')
  expect(screen.getByRole('spinbutton', { name: '运行时长上限' })).toHaveDisplayValue('')
  fireEvent.click(screen.getByRole('button', { name: '测试并保存' }))
  await waitFor(() => expect(submitted).toMatchObject({ context_cap: 524288, runtime_context_cap: 524288, max_output_tokens: 32768,
    model_context_window: null, model_max_output_tokens: null, round_limit: null, tool_limit: null, active_seconds_limit: null }))
}, 15000)

test('manual declarations take precedence and changing a gateway alias clears the previous declaration', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ ...connection, model_context_window: 131072, model_max_output_tokens: 16384,
    model_profile: { ...profile, context_window: 131072, model_max_output_tokens: 16384, capacity_sources: { context_window: 'explicit_declaration', max_output_tokens: 'explicit_declaration' } } })))
  mount(<ControllerSettings open close={() => {}} />)
  const model = await screen.findByDisplayValue('gateway-alias')
  expect(screen.getByLabelText('模型能力来源')).toHaveTextContent('上下文容量：131,072 tokens · 人工声明')
  fireEvent.change(model, { target: { value: 'another-alias' } })
  await waitFor(() => expect(screen.getByLabelText('模型能力来源')).toHaveTextContent('上下文容量：未知'))
  fireEvent.click(screen.getByText('高级连接参数'))
  expect(screen.getByRole('spinbutton', { name: '声明模型上下文容量' })).toHaveDisplayValue('')
  expect(screen.getByRole('spinbutton', { name: '声明模型输出容量' })).toHaveDisplayValue('')
})

test('a fresh ID-only model list clears stale capacity metadata and changing the endpoint hides its list', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => response(input.endsWith('/models') ? { models: [{ id: 'gateway-alias' }] } : {
    ...connection, endpoint_model_metadata: { id: 'gateway-alias', context_window: 131072 },
    model_profile: { ...profile, context_window: 131072, capacity_sources: { context_window: 'endpoint_metadata', max_output_tokens: 'unknown' } },
  })))
  mount(<ControllerSettings open close={() => {}} />)
  await screen.findByDisplayValue('gateway-alias')
  expect(screen.getByLabelText('模型能力来源')).toHaveTextContent('上下文容量：131,072 tokens · 服务元数据')
  fireEvent.click(screen.getByRole('button', { name: '读取服务模型列表' }))
  await waitFor(() => expect(screen.getByLabelText('模型能力来源')).toHaveTextContent('上下文容量：未知'))
  expect(screen.getByRole('combobox', { name: '服务模型列表' })).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'http://127.0.0.1:9989/v1' } })
  await waitFor(() => expect(screen.queryByRole('combobox', { name: '服务模型列表' })).not.toBeInTheDocument())
  expect(screen.getByLabelText('模型能力来源')).toHaveTextContent('上下文容量：未知')
}, 15000)

test('compaction and artifact checks show honest lifecycle states and retained history', () => {
  mount(<><ContextCompactionContent payload={{ phase: 'end', before_tokens: 52000, after_tokens: 12000, message: '较早历史已摘要' }} />
    <DeliveryCheckContent payload={{ phase: 'failed', message: '继续修复缺失文件', checks: [{ name: '来源引用', status: 'passed' }, { name: '成果可读取', status: 'failed', message: '文件不存在' }] }} /></>)
  expect(screen.getByText('上下文已整理')).toBeInTheDocument()
  expect(screen.getByText('输入估计 52,000 → 12,000 tokens')).toBeInTheDocument()
  expect(screen.getByText('完整历史与来源保留，可按需回查。')).toBeInTheDocument()
  expect(screen.getByLabelText('交付检查')).toHaveTextContent('交付检查未通过')
  expect(screen.getByLabelText('交付检查')).toHaveTextContent('成果可读取：文件不存在')
})

test('a staged delivery check does not claim final delivery passed', () => {
  mount(<DeliveryCheckContent payload={{ phase: 'staged', finalized: false, message: '已保存原要求，继续处理下一份成果' }} />)
  expect(screen.getByLabelText('交付检查')).toHaveTextContent('本批成果已核验，等待最终交付')
  expect(screen.queryByText('交付检查通过')).not.toBeInTheDocument()
})

test('projection replaces progress for each compaction and delivery check without marking pauses terminal', () => {
  const event = (seq: number, kind: string, payload: Record<string, unknown>): AgentEvent => ({ seq, kind, payload, run_id: 'run', created: '' })
  const projected = projectConversation([event(1, 'context_compaction', { phase: 'start', compaction_id: 'c1' }), event(2, 'context_compaction', { phase: 'end', compaction_id: 'c1' }), event(3, 'delivery_check', { phase: 'checking' }), event(4, 'delivery_check', { phase: 'passed' })])
  expect(projected.items).toHaveLength(2)
  expect(projected.items[0]).toMatchObject({ event: { payload: { phase: 'end' } } })
  expect(projected.items[1]).toMatchObject({ event: { payload: { phase: 'passed' } } })
  expect(terminalActivity('paused')).toBe(false)
})

class SilentSource { onopen: (() => void) | null = null; onerror: (() => void) | null = null; onmessage: ((event: MessageEvent) => void) | null = null; readyState = 1; close() {} }
test.each(['failed', 'partial'])('historical recoverable %s run exposes continue original task and supplements the same conversation', async (initialStatus) => {
  let status = initialStatus
  const writes: string[] = []
  vi.stubGlobal('EventSource', SilentSource)
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      writes.push(input)
      if (input.endsWith('/resume')) status = 'running'
      return response({ id: 'old006run', status })
    }
    if (input.endsWith('/agent/health')) return response({ enabled: true })
    if (input.endsWith('/agent/connection')) return response(connection)
    if (input.includes('/events?')) return response({ items: [{ seq: 1, run_id: 'old006run', kind: 'agent_activity', created: '', payload: { phase: 'failed' } }], next_cursor: 1 })
    if (input.endsWith('/agent/sessions/conversation')) return response({ id: 'conversation', workspace_id: 'workspace', runs: [{ id: 'old006run', status, error: '连接曾暂时失败', can_resume: true }] })
    if (input.endsWith('/task')) return response({ id: 'conversation', title: '旧目标', workspace_id: 'workspace', session_id: 'conversation', goal: '保留原编号完成清理', status, manual: false,
      selected_assets: [], revision: 1, scope_revision: 1, plan: [], job_refs: [], result_refs: [], artifact_refs: [], initial_refs: [] })
    return response(input.includes('/audit-tasks?') ? { items: [] } : {})
  }))
  mount(<AgentWorkbench />, '/agent?workspace=workspace&session=conversation')
  const button = await screen.findByRole('button', { name: '继续原任务' })
  if (initialStatus === 'partial') expect(screen.getAllByRole('status').some(node => node.textContent === '部分成果')).toBe(true)
  expect(screen.getByRole('button', { name: '追加要求' })).toBeInTheDocument()
  fireEvent.click(button)
  await waitFor(() => expect(writes).toEqual(['/api/platform/v1/agent/runs/old006run/resume']))
  expect(writes.some(path => path.includes('/messages') || path.includes('/sessions/'))).toBe(false)
}, 15000)

test('pause resume stop and manual compaction address the existing run and conversation', async () => {
  let status = 'running'
  const writes: { path: string; body: unknown }[] = []
  vi.stubGlobal('EventSource', SilentSource)
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      writes.push({ path: input, body: JSON.parse(String(init.body)) })
      if (input.endsWith('/pause')) status = 'paused'
      if (input.endsWith('/resume')) status = 'running'
      if (input.endsWith('/cancel')) status = 'cancelled'
      return response({ id: 'run', status })
    }
    if (input.endsWith('/agent/health')) return response({ enabled: true, engine: 'deepseek-harness', engine_version: '0.2.1-alpha.4' })
    if (input.endsWith('/agent/connection')) return response(connection)
    if (input.includes('/events?')) return response({ items: [], next_cursor: 0 })
    if (input.endsWith('/agent/sessions/conversation')) return response({ id: 'conversation', workspace_id: 'workspace', runs: [{ id: 'run', status, error: null }] })
    if (input.endsWith('/task')) return response({ id: 'conversation', title: '真实任务', workspace_id: 'workspace', session_id: 'conversation', goal: '完成清洗', status, manual: false,
      selected_assets: [], revision: 1, scope_revision: 1, plan: [], job_refs: [], result_refs: [], artifact_refs: [], initial_refs: [] })
    return response(input.includes('/audit-tasks?') ? { items: [] } : {})
  }))
  mount(<AgentWorkbench />, '/agent?workspace=workspace&session=conversation')
  fireEvent.click(await screen.findByRole('button', { name: '暂停任务' }))
  fireEvent.click(await screen.findByRole('button', { name: '继续任务' }))
  await screen.findByRole('button', { name: '暂停任务' })
  fireEvent.click(screen.getByText('更多', { selector: 'summary' }))
  fireEvent.click(screen.getByRole('button', { name: '整理上下文' }))
  expect(await screen.findByText('已安排在下一次模型请求前整理上下文。')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '停止后续步骤', exact: true }))
  await waitFor(() => expect(writes).toEqual([
    { path: '/api/platform/v1/agent/runs/run/pause', body: {} }, { path: '/api/platform/v1/agent/runs/run/resume', body: {} },
    { path: '/api/platform/v1/agent/sessions/conversation/compact', body: {} }, { path: '/api/platform/v1/agent/runs/run/cancel', body: { cancel_owned_jobs: false } },
  ]))
  expect(within(screen.getByLabelText('Agent 会话')).getByRole('textbox', { name: '任务或问题' })).toBeInTheDocument()
}, 15000)
