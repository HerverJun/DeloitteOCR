import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'
import type { ItemEvidence, TodoItem, TodoPage } from '../../../packages/contracts/types.generated'
import type { Batch, BatchDisposition, Issue } from '../src/features/batches/api'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  localStorage.clear()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = (value: unknown) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(value)) })
const failure = (status: number, detail: string) => ({ ok: false, status, json: async () => ({ detail }) })
function todo(id: number, changes: Partial<TodoItem> = {}): TodoItem {
  return { todo_id: `todo${id}`, kind: 'issue', issue_type: 'result', context: { workspace_id: 'wMonthly', batch_id: 'bMonthly', item_id: `item${id}` },
    scenario: 'data_compare', batch_revision: 9, project_revision: null, title: `事项 ${id}`, state: 'pending', priority: 'normal',
    priority_basis: 'default_review_order', updated_at: '2026-10-03T03:00:00Z', source_summary: `原账第 ${id} 行`,
    actions: ['open_evidence', 'confirm', 'defer'], ...changes }
}
function page(items: TodoItem[], next_cursor: string | null = null, snapshot_token = 'snapshotA'): TodoPage {
  return { schema_version: '1.0', workspace_id: 'wMonthly', total: 86, query_hash: 'a'.repeat(64), sort: 'priority_then_updated', items, next_cursor, snapshot_token }
}
function evidence(item: TodoItem, value = `真实证据 ${item.context.item_id}`): ItemEvidence {
  return { schema_version: '1.0', workspace_id: 'wMonthly', batch_id: 'bMonthly', item_id: item.context.item_id!, batch_revision: 9,
    scenario: item.scenario!, issue_type: item.issue_type === 'technical' ? 'technical' : 'result', snapshot_hash: 'b'.repeat(64),
    template_id: 'tMonthly', template_version: 1, config_hash: 'c'.repeat(64), result_id: 'resultA', native_version_id: null,
    native_revision: null, adoption_operation_id: null, rule_summary: '原账固定规则', rule_hash: 'd'.repeat(64),
    fields: [{ name: '实际字段', values: [{ role: 'original', kind: 'text', value, raw: value, source: null }], rule_kind: 'exact', outcome: 'different' }],
    sources: [], source_count: 0, native_context_available: false }
}
function issue(item: TodoItem): Issue { return { item_id: item.context.item_id!, title: item.title, category: 'different', detail: '业务金额需要解释',
  source: { source_row: 45 }, raw_output: { marker: '固定原始技术数据' }, disposition: item.state as Issue['disposition'], note: '原有业务说明' } }
function batch(scenario: TodoItem['scenario'] = 'data_compare', revision = 9): Batch {
  return { batch_id: 'bMonthly', workspace_id: 'wMonthly', revision, template_id: 'tMonthly', template_version: 1,
    template: { template_id: 'tMonthly', version: 1, name: '月度业务模板', scenario: scenario!, config: { link_id: 'linkActual' }, config_hash: 'c'.repeat(64) },
    coverage: { index_complete: true, result_rows: 86, required_items: 86, indexed_items: 86, pending: 86, confirmed: 0, deferred: 0 },
    delivery: { revision: null, bundle_id: null, status: 'none', kind: null }, status: 'succeeded', stage: 'done',
    summary: { total: 86, processed: 86, succeeded: 86, failed: 0, pending: 86, confirmed: 0, deferred: 0, coverage: {}, execution_complete: true, review_complete: false, pending_operations: 0 },
    issues: [], receipts: { native_import: { handoff_id: 'handoffActual' }, native_inputs: { asset_set_id: 'setActual' } },
    artifact_state: 'collected', artifacts: [], capabilities: [], created_at: '2026-10-03T03:00:00Z', updated_at: '2026-10-03T03:00:00Z' }
}
function shared(path: string) {
  if (path.includes('/workspaces?')) return response({ items: [{ workspace_id: 'wMonthly', name: '业务空间' }], next_cursor: null, snapshot_revision: 1 })
  if (path.includes('/audit-projects?')) return response({ schema_version: '1.0', items: [{ project_id: 'projectA', project: { title: '审计项目 A' } }], snapshot_token: 'projectsA', next_cursor: null, total: 1 })
  if (path.includes('/procedures?')) return response({ schema_version: '1.0', items: [{ procedure_id: 'procedureA', procedure: { title: '审计程序 A' } }], snapshot_token: 'proceduresA', next_cursor: null, total: 1 })
  return null
}
function mount(url: string, client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })) {
  const router = createMemoryRouter(routes, { initialEntries: [url] })
  const view = render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, view, client }
}

test('a deep todo reads its actual evidence and next identity without replacing the visible page or losing native context', async () => {
  const selected = todo(45, { scenario: 'classification', actions: ['open_evidence', 'open_native', 'confirm'] })
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    calls.push(path)
    const common = shared(path); if (common) return common
    const url = new URL(path, 'http://localhost')
    if (url.pathname.endsWith('/todos/todo45')) return response({ schema_version: '1.0', item: selected, matches_filters: true, page_cursor: 'page45', snapshot_token: 'snapshotA' })
    if (url.pathname.endsWith('/todos')) return response(url.searchParams.get('cursor') === 'page45' ? page([selected, todo(46)]) : page(Array.from({ length: 20 }, (_, index) => todo(index + 1)), 'page21'))
    if (path.includes('/items/item45/evidence')) return response(evidence(selected, '第 45 项的实际模型原文'))
    if (url.pathname.endsWith('/batches/bMonthly')) return response(batch('classification'))
    if (url.pathname.endsWith('/classification/projects')) return response({ items: [{ project_id: 'nativeActual', link_id: 'linkActual' }, { project_id: 'nativeWrong', link_id: 'linkWrong' }] })
    throw new Error(path)
  }))
  const origin = '/todos?workspace=wMonthly&scenario=classification&disposition=pending&sort=updated&todo=todo45'
  const { router, view, client } = mount(origin)
  expect(await screen.findByText('第 45 项的实际模型原文')).toBeInTheDocument()
  expect(await screen.findByRole('button', { name: '下一待办事项' })).toBeInTheDocument()
  expect(router.state.location.search).not.toContain('cursor=')
  expect(screen.getByRole('button', { name: '事项 1' })).toBeInTheDocument()
  expect(screen.queryByText('真实证据 item1')).not.toBeInTheDocument()
  const batchLink = new URL(screen.getByRole('link', { name: '打开并处理此批次事项' }).getAttribute('href')!, 'http://localhost')
  expect(batchLink.searchParams.get('item')).toBe('item45')
  expect(batchLink.searchParams.get('next_todo')).toBe('todo46')
  expect(batchLink.searchParams.get('return_to')).toBe(origin)
  const nativeLink = new URL((await screen.findByRole('link', { name: '到原生项目核对' })).getAttribute('href')!, 'http://localhost')
  expect(nativeLink.searchParams.get('project')).toBe('nativeActual')
  expect(nativeLink.searchParams.get('handoff')).toBe('handoffActual')
  expect(nativeLink.searchParams.get('set')).toBe('setActual')
  expect(nativeLink.searchParams.get('return_to')).toBe(origin)
  expect(calls.filter(path => path.includes('/items/') && path.includes('/evidence')).every(path => path.includes('/items/item45/'))).toBe(true)

  view.unmount()
  const readsBeforeReturn = calls.filter(path => path.includes('/items/item45/evidence')).length
  mount(nativeLink.searchParams.get('return_to')!, client)
  await screen.findByText('第 45 项的实际模型原文')
  await waitFor(() => expect(calls.filter(path => path.includes('/items/item45/evidence')).length).toBeGreaterThan(readsBeforeReturn))
}, 20000)

test('server filters and sort survive a stale snapshot refresh and retain the selected identity outside filters', async () => {
  const selected = todo(45, { state: 'confirmed' })
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    calls.push(path)
    const common = shared(path); if (common) return common
    const url = new URL(path, 'http://localhost')
    if (url.pathname.includes('/todos')) {
      if (url.searchParams.get('snapshot_token') === 'snapshotOld') return failure(409, '待办快照已过期')
      if (url.pathname.endsWith('/todo45')) return response({ schema_version: '1.0', item: selected, matches_filters: false, page_cursor: null, snapshot_token: 'snapshotNew' })
      return response(page([todo(1)], null, 'snapshotNew'))
    }
    if (path.includes('/items/item45/evidence')) return response(evidence(selected))
    if (url.pathname.endsWith('/batches/bMonthly')) return response(batch())
    throw new Error(path)
  }))
  const expected = { kind: 'issue', issue_type: 'result', scenario: 'data_compare', disposition: 'pending', execution_state: 'succeeded', priority: 'normal', sort: 'updated', project_id: 'projectA', procedure_id: 'procedureA' }
  const query = new URLSearchParams({ workspace: 'wMonthly', ...expected, todo: 'todo45', cursor: 'pageOld', snapshot_token: 'snapshotOld' })
  const { router } = mount(`/todos?${query}`)
  await screen.findAllByText('待办快照已过期')
  fireEvent.click(screen.getByRole('button', { name: '刷新全部并保留筛选' }))
  expect(await screen.findByText('此事项已不符合当前筛选')).toBeInTheDocument()
  expect(await screen.findByText('真实证据 item45')).toBeInTheDocument()
  const current = new URLSearchParams(router.state.location.search)
  for (const [name, value] of Object.entries(expected)) expect(current.get(name)).toBe(value)
  expect(current.get('todo')).toBe('todo45')
  expect(current.has('cursor')).toBe(false)
  expect(current.has('snapshot_token')).toBe(false)
  const fresh = calls.filter(path => path.includes('/todos?') && !path.includes('snapshotOld')).at(-1)!
  const serverQuery = new URL(fresh, 'http://localhost').searchParams
  for (const [name, value] of Object.entries(expected)) expect(serverQuery.get(name)).toBe(value)
  expect(serverQuery.get('limit')).toBe('20')
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '待办事项类型' }))
  fireEvent.click((await screen.findAllByText('技术故障', { selector: '.ant-select-item-option-content' })).at(-1)!)
  await waitFor(() => expect(calls.some(path => path.includes('/todos?') && path.includes('issue_type=technical'))).toBe(true))
  expect(new URLSearchParams(router.state.location.search).get('todo')).toBe('todo45')
}, 20000)

test('confirming a deep batch item uses its actual revision and returns to the next global page under a new snapshot', async () => {
  const selected = todo(45), next = todo(46)
  const saved: BatchDisposition[] = [], calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    calls.push(path)
    const common = shared(path); if (common) return common
    const url = new URL(path, 'http://localhost')
    if (url.pathname.endsWith('/dispositions')) { saved.push(JSON.parse(init!.body as string)); selected.state = 'confirmed'; return response(batch('data_compare', 10)) }
    if (url.pathname.endsWith('/issues')) { const value = batch(); return response({ items: [issue(todo(1))], total: 86, revision: 5, coverage: value.coverage, summary: value.summary }) }
    if (url.pathname.endsWith('/items/item45')) return response({ schema_version: '1.0', workspace_id: 'wMonthly', batch_id: 'bMonthly', revision: saved.length ? 10 : 9, item: issue(selected) })
    if (url.pathname.endsWith('/items/item45/evidence')) return response(evidence(selected))
    if (url.pathname.endsWith('/items/item46/evidence')) return response(evidence(next, '下一全局待办的证据'))
    if (url.pathname.endsWith('/batches/bMonthly')) return response(batch('data_compare', saved.length ? 10 : 9))
    if (url.pathname.endsWith('/history')) return response({ schema_version: '1.0', batch_id: 'bMonthly', revision: 9, items: [], total: 0, offset: 0 })
    if (url.pathname.endsWith('/todos/todo46')) return response({ schema_version: '1.0', item: next, matches_filters: true, page_cursor: 'pageFresh46', snapshot_token: 'snapshotFresh' })
    if (url.pathname.endsWith('/todos')) return response(page([next, todo(47)], null, 'snapshotFresh'))
    throw new Error(path)
  }))
  const origin = '/todos?workspace=wMonthly&kind=issue&scenario=data_compare&disposition=pending&sort=updated&cursor=pageOld45&snapshot_token=snapshotOld&todo=todo45'
  const batchQuery = new URLSearchParams({ workspace: 'wMonthly', batch: 'bMonthly', item: 'item45', disposition: 'all', issue_page: '1', todo: 'todo45', next_todo: 'todo46', return_to: origin })
  const { router } = mount(`/batches?${batchQuery}`)
  expect(await screen.findByRole('heading', { name: '事项 45' })).toBeInTheDocument()
  expect(await screen.findByText('真实证据 item45')).toBeInTheDocument()
  const details = screen.getByText('批次技术详情 · 原始输出与来源记录').closest('details')!
  expect(details).not.toHaveAttribute('open')
  fireEvent.change(screen.getByRole('textbox', { name: '本机复核人' }), { target: { value: '业务复核人' } })
  fireEvent.change(screen.getByRole('textbox', { name: '复核说明' }), { target: { value: '原凭证已核对' } })
  fireEvent.click(screen.getByRole('button', { name: '确认并下一项' }))
  expect(await screen.findByRole('heading', { name: '事项 46' })).toBeInTheDocument()
  expect(await screen.findByText('下一全局待办的证据')).toBeInTheDocument()
  expect(saved).toHaveLength(1)
  expect(saved[0]).toMatchObject({ item_ids: ['item45'], expected_revision: 9, action: 'confirm', note: '原凭证已核对' })
  const returned = new URLSearchParams(router.state.location.search)
  expect(router.state.location.pathname).toBe('/todos')
  expect(returned.get('todo')).toBe('todo46')
  expect(returned.get('cursor')).toBe('pageFresh46')
  expect(returned.get('snapshot_token')).toBe('snapshotFresh')
  expect(returned.get('sort')).toBe('updated')
  expect(returned.get('disposition')).toBe('pending')
  const freshLocate = calls.find(path => path.includes('/todos/todo46?') && !path.includes('snapshot_token='))!
  expect(freshLocate).toContain('limit=20')
  expect(freshLocate).not.toContain('cursor=')
  expect(localStorage.getItem('clear-batch-disposition:bMonthly')).toBeNull()
}, 20000)
