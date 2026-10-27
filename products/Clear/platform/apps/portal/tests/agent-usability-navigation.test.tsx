import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import { AgentWorkbench } from '../src/features/agent/AgentWorkbench'
import { taskIntents } from '../src/features/home/taskIntents'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
const resources: { client: QueryClient; router: ReturnType<typeof createMemoryRouter> }[] = []
afterEach(() => { cleanup(); resources.splice(0).forEach(({ client, router }) => { router.dispose(); client.clear() }); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })
const ok = (body: unknown, status = 200) => ({ ok: status < 400, status, json: async () => body })
type Handler = (path: string, init?: RequestInit) => unknown
function mount(url: string, handle?: Handler) {
  const writes: { path: string; body: Record<string, unknown> }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method && init.method !== 'GET') writes.push({ path, body: JSON.parse(String(init.body)) })
    const custom = handle?.(path, init)
    if (custom !== undefined) return custom
    if (path.endsWith('/agent/health')) return ok({ enabled: true, direct_enabled: true })
    if (path.endsWith('/agent/connection')) return ok({ configured: true, available: true, model: 'connected' })
    if (path.includes('/workspaces?')) return ok({ items: [{ workspace_id: 'project', name: '季度核对' }], next_cursor: null, snapshot_revision: 1 })
    if (path.includes('/audit-tasks?')) return ok({ items: [] })
    if (path.includes('/events?')) return ok({ items: [], next_cursor: 0 })
    if (path.endsWith('/jobs')) return ok({ jobs: [], sampled_at: '2026-10-25T05:00:00Z' })
    if (path.endsWith('/task')) return ok({ id: 'conversation', workspace_id: 'project', title: '季度核对任务', revision: 1, status: 'draft', manual: false, selected_assets: [], plan: [], initial_refs: [], result_refs: [], job_refs: [], artifact_refs: [], runs: [] })
    if (path.endsWith('/agent/sessions/conversation')) return ok({ id: 'conversation', workspace_id: 'project', runs: [] })
    return ok({ items: [] })
  }))
  vi.stubGlobal('EventSource', class { readyState = 1; onopen = null; onmessage = null; onerror = null; close() {} })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }], { initialEntries: [url] })
  resources.push({ client, router })
  render(<ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, writes }
}

test('home intent fills an editable draft and focuses it without creating or sending work', async () => {
  const { router, writes } = mount('/agent?workspace=project&intent=files')
  const input = await screen.findByRole('textbox', { name: '任务或问题' })
  await waitFor(() => expect(input).toHaveValue(taskIntents[1].draft))
  expect(input).toHaveFocus()
  expect(router.state.location.search).toBe('?workspace=project')
  fireEvent.change(input, { target: { value: '只整理发票编号，不修改金额。' } })
  expect(JSON.parse(localStorage.getItem('clear-audit-draft:project:new')!).goal).toBe('只整理发票编号，不修改金额。')
  expect(writes).toEqual([])
})

test('an intent preserves an existing draft and only replaces it after an explicit choice', async () => {
  localStorage.setItem('clear-audit-draft:project:new', JSON.stringify({ goal: '尚未发送的真实需求', assets: ['original'] }))
  const { writes } = mount('/agent?workspace=project&intent=audit')
  const input = await screen.findByRole('textbox', { name: '任务或问题' })
  expect(input).toHaveValue('尚未发送的真实需求')
  fireEvent.click(await screen.findByRole('button', { name: '使用所选示例' }))
  expect(input).toHaveValue(taskIntents[2].draft)
  expect(JSON.parse(localStorage.getItem('clear-audit-draft:project:new')!).assets).toEqual(['original'])
  expect(writes).toEqual([])
})

test('recent tasks offers a project picker without falsely declaring an empty project', async () => {
  const { router, writes } = mount('/agent')
  fireEvent.click(await screen.findByRole('button', { name: '最近任务' }))
  const drawer = await screen.findByRole('dialog', { name: '最近任务' })
  expect(within(drawer).queryByText('本项目还没有任务')).not.toBeInTheDocument()
  expect(within(drawer).getByText('选择项目后查看并继续已有任务。')).toBeInTheDocument()
  fireEvent.mouseDown(within(drawer).getByRole('combobox', { name: '选择任务所在项目' }))
  fireEvent.click(await screen.findByText('季度核对', { selector: '.ant-select-item-option-content' }))
  await waitFor(() => expect(router.state.location.search).toBe('?workspace=project'))
  expect(await within(drawer).findByText('本项目还没有任务')).toBeInTheDocument()
  expect(writes).toEqual([])
})

test('recent tasks distinguishes loading, failure, recovered results and a search with no match', async () => {
  let finish!: (value: unknown) => void
  let reads = 0
  const { writes } = mount('/agent?workspace=project', path => {
    if (path.includes('/audit-tasks?')) {
      reads++
      if (reads === 1) return new Promise(resolve => { finish = resolve })
      if (path.endsWith('keyword=missing')) return ok({ items: [] })
      return ok({ items: [{ id: 'old', title: '上次完成的核对', status: 'succeeded' }] })
    }
  })
  fireEvent.click(await screen.findByRole('button', { name: '最近任务' }))
  const drawer = await screen.findByRole('dialog', { name: '最近任务' })
  expect(await within(drawer).findByText('正在读取最近任务…')).toBeInTheDocument()
  expect(within(drawer).queryByText('本项目还没有任务')).not.toBeInTheDocument()
  finish(ok({ detail: '临时服务错误' }, 503))
  fireEvent.click(await within(drawer).findByRole('button', { name: '重试读取任务' }))
  expect(await within(drawer).findByRole('button', { name: '上次完成的核对' })).toBeInTheDocument()
  fireEvent.change(within(drawer).getByRole('searchbox', { name: '查找任务' }), { target: { value: 'missing' } })
  expect(await within(drawer).findByText('没有匹配的任务，请换个关键词')).toBeInTheDocument()
  expect(writes).toEqual([])
})

test('automatic project naming remains stable when creation loses its response and the goal changes', async () => {
  let creates = 0
  const { writes } = mount('/agent', (path, init) => {
    if (path.endsWith('/workspaces') && init?.method === 'POST') {
      creates++
      if (creates === 1) return Promise.reject(new TypeError('创建响应丢失'))
      return ok({ workspace_id: 'project', name: JSON.parse(String(init.body)).name })
    }
    if (path.endsWith('/agent/sessions') && init?.method === 'POST') return ok({ id: 'conversation' })
    if (path.endsWith('/messages') && init?.method === 'POST') return ok({ id: 'run', status: 'succeeded' })
  })
  const input = await screen.findByRole('textbox', { name: '任务或问题' })
  fireEvent.change(input, { target: { value: '核对季度供应商台账' } })
  await waitFor(() => expect(screen.getByRole('button', { name: '发送', exact: true })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '发送', exact: true }))
  await screen.findByText('创建响应丢失')
  fireEvent.change(input, { target: { value: '核对季度供应商台账，保留原始编号' } })
  fireEvent.click(screen.getByRole('button', { name: '发送', exact: true }))
  await waitFor(() => expect(writes.some(item => item.path.endsWith('/messages'))).toBe(true))
  const requests = writes.filter(item => item.path.endsWith('/workspaces'))
  expect(requests).toHaveLength(2)
  expect(requests[0].body.name).toMatch(/^核对季度供应商台账 · /)
  expect(requests[1].body).toEqual(requests[0].body)
})

test('a pre-upgrade pending project creation keeps its original fixed name and request ID', async () => {
  sessionStorage.setItem('clear-audit-new-project', JSON.stringify({ request_id: 'original-request' }))
  const { writes } = mount('/agent', (path, init) => path.endsWith('/workspaces') && init?.method === 'POST' ? ok({ workspace_id: 'project' }) : undefined)
  await waitFor(() => expect(screen.getByRole('button', { name: '添加文件' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '添加文件' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0].body).toEqual({ schema_version: '1.0', name: '智能审计项目', request_id: 'original-request' })
})

test('selected library files must finish verification before sending and retain their fixed reference', async () => {
  let finish!: (value: unknown) => void
  const reference = { kind: 'asset_set', application: 'platform', workspace_id: 'project', object_id: 'selected-files', revision: 1,
    sources: [{ kind: 'asset', workspace_id: 'project', object_id: 'original-csv', revision: 1 }] }
  const { writes } = mount('/agent?workspace=project&asset_set=selected-files', (path, init) => {
    if (path.endsWith('/agent/references')) return new Promise(resolve => { finish = resolve })
    if (path.endsWith('/agent/sessions') && init?.method === 'POST') return ok({ id: 'conversation' })
    if (path.endsWith('/messages') && init?.method === 'POST') return ok({ id: 'run', status: 'succeeded' })
  })
  const input = await screen.findByRole('textbox', { name: '任务或问题' })
  fireEvent.change(input, { target: { value: '只核对这份采购台账' } })
  expect(await screen.findByText('正在核实所选资料…')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '发送', exact: true })).toBeDisabled()
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(writes.some(item => item.path.endsWith('/agent/sessions'))).toBe(false)
  await act(async () => { finish(ok({ reference })) })
  expect(await screen.findByText('已核实所选资料，可继续处理或与其它资料比较')).toBeInTheDocument()
  expect(screen.queryByText('已核实带入成果，可继续处理或与其它资料比较')).not.toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: '发送', exact: true })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '发送', exact: true }))
  await waitFor(() => expect(writes.some(item => item.path.endsWith('/messages'))).toBe(true))
  expect(writes.find(item => item.path.endsWith('/agent/sessions'))?.body.initial_refs).toEqual([reference])
})
