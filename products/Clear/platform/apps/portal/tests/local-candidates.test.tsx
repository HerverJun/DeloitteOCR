import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { LocalCandidates } from '../src/features/ocr/LocalCandidates'
import type { Diagnostic } from '../src/features/ocr/screenDiagnosticWorkflow'

beforeEach(() => {
  sessionStorage.clear()
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal('crypto', { randomUUID })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const issue: Diagnostic = { id: 'issueA', code: 'conflict', severity: 'warning', reason: '独立证据冲突',
  location: { level: 'cell', table: 0, row: 1, column: 2, box: [10, 20, 90, 60], basis: 'observed_full_cell_geometry', range_semantics: 'full_cell' },
  evidence: [], applicable_actions: [] }
const props = { workspaceId: 'workA', batchId: 'batchA', inputVersionId: 'versionA', resultId: 'resultA', revision: 2,
  diagnostic: issue, remainingBudget: 90, primaryPending: false, currentValue: 'OBFCI' }
const submission = { request_id: 'original001', budget_seconds: 60, target: { input_version_id: 'versionA', result_id: 'resultA', result_revision: 2, issue_ids: ['issueA'] } }
const storage = 'clear-local-candidate:workA:batchA:versionA'
const candidate = { task_id: 'taskA', status: 'succeeded', candidate_state: 'available', current_binding: true,
  text: 'OBFC1', text_preview_only: false, binding: { result_id: 'resultA', result_revision: 2 }, artifact: {} }
const receipt = { request_id: submission.request_id, status: 'submitted', execution_status: 'succeeded', items: [candidate],
  target: submission.target,
  owned_task_ids: ['taskA'],
  values_modified: false, selection_applied: false, human_review: 'not_performed' }
const ok = (value: unknown) => ({ ok: true, status: 200, json: async () => value })
function mount(properties = props) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = (value: typeof props) => <ConfigProvider><QueryClientProvider client={client}><LocalCandidates {...value} /></QueryClientProvider></ConfigProvider>
  const mounted = render(view(properties))
  return { ...mounted, rerenderProperties: (value: typeof props) => mounted.rerender(view(value)) }
}

test('unreliable geometry and unfinished primary batch cannot start a local reread', () => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  const mounted = mount({ ...props, diagnostic: { ...issue, location: { ...issue.location, basis: 'index_only' } } })
  expect(screen.getByRole('button', { name: '生成此格文字候选' })).toBeDisabled()
  mounted.unmount()
  mount({ ...props, primaryPending: true })
  expect(screen.getByRole('button', { name: '生成此格文字候选' })).toBeDisabled()
  expect(fetcher).not.toHaveBeenCalled()
})

test('submission binds the actual issue and revision without free geometry or adoption', async () => {
  const writes: Record<string, unknown>[] = []
  let request = submission.request_id
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'POST') { const body = JSON.parse(String(init.body)); writes.push(body); request = body.request_id }
    return ok({ ...receipt, request_id: request })
  }))
  mount()
  fireEvent.click(screen.getByRole('button', { name: '生成此格文字候选' }))
  await screen.findByText('候选文字：OBFC1')
  expect(screen.getByText('当前格值：OBFCI')).toBeInTheDocument()
  expect(writes).toHaveLength(1)
  expect(Object.keys(writes[0]).sort()).toEqual(['budget_seconds', 'request_id', 'target'])
  expect(writes[0].target).toEqual(submission.target)
  expect(screen.queryByRole('button', { name: /采用|确认/ })).not.toBeInTheDocument()
})

test('a lost pause receipt survives remount and reuses the original control operation', async () => {
  sessionStorage.setItem(storage, JSON.stringify(submission))
  const writes: { request_id: string; action: string }[] = []
  const waiting = { ...receipt, execution_status: 'pending', items: [{ ...candidate, status: 'paused', text: null }] }
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST' && url.endsWith('/control')) {
      writes.push(JSON.parse(String(init.body)))
      if (writes.length === 1) return { ok: false, status: 503 }
    }
    return ok(waiting)
  }))
  const mounted = mount()
  await screen.findByRole('button', { name: '暂停候选' })
  fireEvent.click(screen.getByRole('button', { name: '暂停候选' }))
  await screen.findByText('候选回执暂不可读，请沿原请求重试')
  expect(writes).toHaveLength(1)
  mounted.unmount()
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '核实原控制操作' }))
  await waitFor(() => expect(writes).toHaveLength(2))
  expect(writes[1]).toEqual(writes[0])
  expect(writes[1].action).toBe('pause')
  await waitFor(() => expect(sessionStorage.getItem(storage + ':control')).toBeNull())
})

test('a candidate from an older result stays visibly historical after an edit', async () => {
  sessionStorage.setItem(storage, JSON.stringify(submission))
  vi.stubGlobal('fetch', vi.fn(async () => ok(receipt)))
  mount({ ...props, revision: 3, currentValue: '人工修改值' })
  await screen.findByText('历史候选，所依据的结果已变化')
  expect(screen.getByText('当前格值：人工修改值')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /采用|确认/ })).not.toBeInTheDocument()
})

test.each(['running', 'failed'])('a reused %s task remains readable but has no controls owned by this request', async status => {
  sessionStorage.setItem(storage, JSON.stringify(submission))
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => ok({ ...receipt, status: 'reused', owned_task_ids: [],
    execution_status: status === 'running' ? 'pending' : 'failed',
    items: [{ ...candidate, status, candidate_state: status === 'running' ? 'pending' : 'failed' }] }))
  vi.stubGlobal('fetch', fetcher)
  mount()
  await screen.findByText('已复用此前相同位置的识别，任务由原请求控制。')
  expect(screen.queryByRole('button', { name: '暂停候选' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '继续候选' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '取消候选' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '仅重试失败候选' })).not.toBeInTheDocument()
  expect(fetcher.mock.calls.every(call => call[1]?.method !== 'POST')).toBe(true)
})

test('switching to another cell never pairs its value with the previous cell candidate', async () => {
  sessionStorage.setItem(storage, JSON.stringify(submission))
  vi.stubGlobal('fetch', vi.fn(async () => ok(receipt)))
  const mounted = mount()
  await screen.findByText('候选文字：OBFC1')
  expect(screen.getByText('当前格值：OBFCI')).toBeInTheDocument()
  mounted.rerenderProperties({ ...props, currentValue: '9,999.00',
    diagnostic: { ...issue, id: 'issueB', location: { ...issue.location, row: 3 } } })
  await screen.findByText('当前显示的是此前结果或疑点的候选，请切回对应疑点核对。')
  expect(screen.queryByText('当前格值：9,999.00')).not.toBeInTheDocument()
  expect(screen.getByText('候选文字：OBFC1')).toBeInTheDocument()
})

test('a new request for the selected cell restores a correctly bound comparison', async () => {
  sessionStorage.setItem(storage, JSON.stringify(submission))
  let latest = receipt
  const writes: typeof submission[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      const request = JSON.parse(String(init.body)) as typeof submission
      writes.push(request)
      latest = { ...receipt, request_id: request.request_id, target: request.target,
        items: [{ ...candidate, text: '9,998.00' }] }
    }
    return ok(latest)
  }))
  const mounted = mount()
  await screen.findByText('候选文字：OBFC1')
  mounted.rerenderProperties({ ...props, currentValue: '9,999.00',
    diagnostic: { ...issue, id: 'issueB', location: { ...issue.location, row: 3 } } })
  fireEvent.click(screen.getByRole('button', { name: '生成此格文字候选' }))
  await screen.findByText('候选文字：9,998.00')
  expect(screen.getByText('当前格值：9,999.00')).toBeInTheDocument()
  expect(screen.queryByText('候选文字：OBFC1')).not.toBeInTheDocument()
  expect(screen.queryByText('当前显示的是此前结果或疑点的候选，请切回对应疑点核对。')).not.toBeInTheDocument()
  expect(writes).toHaveLength(1)
  expect(writes[0].target.issue_ids).toEqual(['issueB'])
  expect(writes[0].request_id).not.toBe(submission.request_id)
})
