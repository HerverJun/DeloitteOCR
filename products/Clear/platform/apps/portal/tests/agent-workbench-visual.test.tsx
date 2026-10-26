import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { AgentWorkbench } from '../src/features/agent/AgentWorkbench'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })
const response = (value: unknown) => ({ ok: true, json: async () => value })
class SilentSource { readyState = 1; onopen = null; onmessage = null; onerror = null; close() {} }
function mount(wide: boolean, status = 'running') {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({ matches: query === '(min-width: 1280px)' && wide,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  const writes: { path: string; body: unknown }[] = []
  vi.stubGlobal('EventSource', SilentSource)
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method) writes.push({ path, body: JSON.parse(String(init.body)) })
    return response(path.endsWith('/health') ? { enabled: true, engine: 'deepseek-harness' } : path.endsWith('/connection') ? { available: true, model: 'gateway-alias' } :
      path.endsWith('/task') ? { id: 'conversation', workspace_id: 'workspace', session_id: 'conversation', title: '核对十万行台账', goal: '保留前导零和原始来源',
        status, manual: false, revision: 1, scope_revision: 1, plan: [], selected_assets: [], job_refs: [], artifact_refs: [], result_refs: [], initial_refs: [], runs: [{ id: 'run', status }] } :
      path.endsWith('/agent/sessions/conversation') ? { id: 'conversation', workspace_id: 'workspace', runs: [{ id: 'run', status }] } :
      path.includes('/events?') ? { items: [{ seq: 1, run_id: 'run', kind: 'model_request', created: '2026-10-16T10:00:00Z',
        payload: { context_budget: { input_tokens_estimate: 50000, available_input_tokens: 61440, model_profile: { context_window: null, runtime_context_cap: 65536,
          effective_context_window: 65536, output_reserve: 2048, safety_margin_tokens: 2048, available_input_tokens: 61440, capacity_sources: { context_window: 'unknown' } } } } }], next_cursor: 1 } : { items: [] })
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><RouterProvider router={createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }],
    { initialEntries: ['/agent?workspace=workspace&session=conversation'] })} /></QueryClientProvider>)
  return writes
}

test('wide task panel is unique and respects the saved collapsed preference', async () => {
  const writes = mount(true)
  expect(await screen.findByRole('heading', { name: '核对十万行台账', level: 2 })).toBeVisible()
  expect(screen.queryByLabelText('当前任务')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '任务详情' }))
  expect(screen.getAllByLabelText('当前任务')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '收起任务详情' }))
  expect(localStorage.getItem('clear-agent-task-panel-v2')).toBe('closed')
  expect(screen.queryByLabelText('当前任务')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '任务详情' })).toHaveFocus()
  fireEvent.click(screen.getByRole('button', { name: '任务详情' }))
  expect(screen.getAllByLabelText('当前任务')).toHaveLength(1)
  expect(writes).toEqual([])
})

test('narrow task details use a drawer and context diagnostics preserve unknown capacity', async () => {
  mount(false)
  await screen.findByRole('heading', { name: '核对十万行台账', level: 2 })
  expect(screen.queryByLabelText('当前任务')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '任务详情' }))
  expect(await screen.findAllByLabelText('当前任务')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  await waitFor(() => expect(screen.queryByLabelText('当前任务')).not.toBeInTheDocument())
  expect(document.querySelector('.agent-runtime-details')).not.toBeInTheDocument()
  expect(screen.getByLabelText('当前上下文')).toHaveTextContent('运行详情')
  fireEvent.click(screen.getByLabelText('当前上下文'))
  await waitFor(() => expect(document.querySelector('.agent-runtime-details')).toBeInTheDocument())
  expect(within(document.querySelector('.agent-runtime-details') as HTMLElement).getByLabelText('模型请求上下文预算')).toHaveTextContent('模型容量：未知')
})

test('visible stop requests native-job cancellation and the secondary option preserves jobs', async () => {
  const writes = mount(true)
  await screen.findByRole('button', { name: '暂停任务' })
  expect(screen.getByRole('button', { name: '停止后续步骤' }).closest('details')).not.toHaveAttribute('open')
  expect(screen.getByRole('button', { name: '停止任务', exact: true })).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '停止任务', exact: true }))
  await waitFor(() => expect(writes).toEqual([{ path: '/api/platform/v1/agent/runs/run/cancel', body: { cancel_owned_jobs: true } }]))
  const summary = screen.getByText('更多', { selector: 'summary' })
  fireEvent.click(summary)
  expect(screen.getByRole('button', { name: '停止后续步骤' })).toBeVisible()
  fireEvent.keyDown(summary, { key: 'Escape' })
  expect(summary).toHaveFocus()
  expect(summary.closest('details')).not.toHaveAttribute('open')
})

test('optional extraction stays secondary and preserves the unsent message without submitting work', async () => {
  const writes = mount(false, 'answered')
  const message = await screen.findByRole('textbox', { name: '任务或问题' })
  fireEvent.change(message, { target: { value: '保留这条尚未发送的问题' } })
  expect(screen.queryByRole('button', { name: '文件工具' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '直接提取文字' })).not.toBeVisible()
  expect(screen.getByRole('button', { name: '工具与参考设置' })).not.toBeVisible()
  fireEvent.click(screen.getByText('更多', { selector: 'summary' }))
  fireEvent.click(screen.getByRole('button', { name: '直接提取文字' }))
  const drawer = await screen.findByRole('dialog', { name: '直接提取文字' })
  fireEvent.click(within(drawer).getByRole('tab', { name: 'PDF 指定页' }))
  expect(await within(drawer).findByRole('textbox', { name: 'PDF 页码' })).toBeVisible()
  expect(within(drawer).getByRole('combobox', { name: 'PDF 处理方式' })).not.toBeVisible()
  expect(within(drawer).getByText('自动提取文字，扫描页会自动识别。')).toBeVisible()
  expect(document.querySelector('.agent-input-surface')?.querySelector('[aria-label="PDF 页码"]')).toBeNull()
  expect(within(drawer).getByRole('button', { name: '提取指定页文字' })).toBeDisabled()
  fireEvent.click(within(drawer).getByRole('button', { name: 'Close' }))
  await waitFor(() => expect(screen.getByRole('textbox', { name: '任务或问题' })).toHaveValue('保留这条尚未发送的问题'))
  expect(writes).toEqual([])
})
