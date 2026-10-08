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
  values_modified: false, selection_applied: false, human_review: 'not_performed' }
const ok = (value: unknown) => ({ ok: true, status: 200, json: async () => value })
function mount(properties = props) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<ConfigProvider><QueryClientProvider client={client}><LocalCandidates {...properties} /></QueryClientProvider></ConfigProvider>)
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
