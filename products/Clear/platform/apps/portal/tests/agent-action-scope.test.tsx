import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import { AgentWorkbench } from '../src/features/agent/AgentWorkbench'

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
function mount(url: string, handle: Handler) {
  const writes: { path: string; body: Record<string, unknown> }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method && init.method !== 'GET') writes.push({ path, body: JSON.parse(String(init.body)) })
    const custom = handle(path, init)
    if (custom !== undefined) return custom
    if (path.endsWith('/agent/health')) return ok({ enabled: true, direct_enabled: true })
    if (path.endsWith('/agent/connection')) return ok({ configured: true, available: true, model: 'connected' })
    if (path.includes('/workspaces?')) return ok({ items: [], next_cursor: null, snapshot_revision: 1 })
    if (path.includes('/audit-tasks?')) return ok({ items: [] })
    if (path.includes('/events?')) return ok({ items: [], next_cursor: 0 })
    if (path.endsWith('/jobs')) return ok({ jobs: [], sampled_at: '' })
    if (path.endsWith('/task')) return ok({ id: 'conversation', workspace_id: 'original', title: '原项目任务', revision: 1, scope_revision: 1,
      status: 'draft', manual: false, selected_assets: [], authorized_reference_ids: [], plan: [], initial_refs: [], result_refs: [], job_refs: [], artifact_refs: [], runs: [] })
    if (path.includes('/agent/sessions/')) return ok({ id: 'conversation', workspace_id: 'original', runs: [] })
    return ok({ items: [], total: 0 })
  }))
  vi.stubGlobal('EventSource', class { readyState = 1; onopen = null; onmessage = null; onerror = null; close() {} })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }], { initialEntries: [url] })
  resources.push({ client, router })
  render(<ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, client, writes }
}

test.each([true, false])('a late send success=%s keeps the current project draft and uses the original attachment snapshot', async succeeds => {
  const original = { goal: '只核对原项目台账', assets: ['original-asset'], authorized_reference_ids: ['original-reference'], delivery_policy: 'draft' }
  const other = { goal: '新项目尚未发送的需求', assets: ['other-asset'], authorized_reference_ids: ['other-reference'], delivery_policy: 'reviewed' }
  localStorage.setItem('clear-audit-draft:original:new', JSON.stringify(original))
  localStorage.setItem('clear-audit-draft:other:new', JSON.stringify(other))
  let finish!: (value: unknown) => void
  const { router, client, writes } = mount('/agent?workspace=original', (path, init) => {
    if (path.endsWith('/workspaces/original/agent/sessions') && init?.method === 'POST') return new Promise(resolve => { finish = resolve })
    if (path.endsWith('/messages') && init?.method === 'POST') return ok({ id: 'run', status: 'succeeded' })
  })
  const invalidation = vi.spyOn(client, 'invalidateQueries')
  await waitFor(() => expect(screen.getByRole('textbox', { name: '任务或问题' })).toHaveValue(original.goal))
  await waitFor(() => expect(screen.getByRole('button', { name: '发送', exact: true })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '发送', exact: true }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0].body).toMatchObject({ selected_assets: original.assets, authorized_reference_ids: original.authorized_reference_ids, delivery_policy: 'draft' })
  await act(async () => { await router.navigate('/agent?workspace=other') })
  await waitFor(() => expect(screen.getByRole('textbox', { name: '任务或问题' })).toHaveValue(other.goal))
  await act(async () => { finish(succeeds ? ok({ id: 'conversation' }) : ok({ detail: '原项目创建响应失败' }, 503)) })
  await waitFor(() => expect(screen.getByRole('button', { name: '发送', exact: true })).not.toHaveClass('ant-btn-loading'))
  expect(router.state.location.search).toBe('?workspace=other')
  expect(localStorage.getItem('clear-audit-draft:other:new')).toBe(JSON.stringify(other))
  expect(screen.getByRole('textbox', { name: '任务或问题' })).toHaveValue(other.goal)
  expect(screen.queryByText('原项目创建响应失败')).not.toBeInTheDocument()
  if (succeeds) {
    expect(JSON.parse(localStorage.getItem('clear-audit-draft:original:conversation')!)).toEqual({ ...original, goal: '' })
    expect(writes[1]).toMatchObject({ path: '/api/platform/v1/agent/sessions/conversation/messages', body: { content: original.goal } })
    expect(invalidation).toHaveBeenCalledWith({ queryKey: ['audit-tasks', 'original'] })
    expect(localStorage.getItem('clear-audit-draft:original:new')).toBeNull()
  } else expect(localStorage.getItem('clear-audit-draft:original:new')).toBe(JSON.stringify(original))
}, 15000)

test('a late project creation from add files archives its original draft without reopening it over another project', async () => {
  const other = { goal: '另一个项目草稿', assets: ['other-asset'] }
  localStorage.setItem('clear-audit-draft:other:new', JSON.stringify(other))
  let finish!: (value: unknown) => void
  const { router, writes } = mount('/agent', (path, init) => {
    if (path.endsWith('/workspaces') && init?.method === 'POST') return new Promise(resolve => { finish = resolve })
  })
  const input = await screen.findByRole('textbox', { name: '任务或问题' })
  fireEvent.change(input, { target: { value: '准备原项目文件' } })
  await waitFor(() => expect(screen.getByRole('button', { name: '添加文件' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '添加文件' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  await act(async () => { await router.navigate('/agent?workspace=other') })
  await waitFor(() => expect(input).toHaveValue(other.goal))
  await act(async () => { finish(ok({ workspace_id: 'created', name: writes[0].body.name })) })
  expect(router.state.location.search).toBe('?workspace=other')
  expect(screen.queryByRole('dialog', { name: '添加文件' })).not.toBeInTheDocument()
  expect(JSON.parse(localStorage.getItem('clear-audit-draft:created:new')!)).toMatchObject({ goal: '准备原项目文件', assets: [] })
  expect(localStorage.getItem('clear-audit-draft:other:new')).toBe(JSON.stringify(other))
  expect(sessionStorage.getItem('clear-audit-new-project')).toBeNull()
}, 15000)

test('confirmed add-files project creation is not reused by the next unscoped goal', async () => {
  let created = 0
  const { router, writes } = mount('/agent', (path, init) => {
    if (path.endsWith('/workspaces') && init?.method === 'POST') return ok({ workspace_id: ++created === 1 ? 'first' : 'second', name: JSON.parse(String(init.body)).name })
    if (path.endsWith('/workspaces/second/agent/sessions') && init?.method === 'POST') return ok({ id: 'second-session' })
    if (path.endsWith('/messages') && init?.method === 'POST') return ok({ id: 'run', status: 'succeeded' })
  })
  const input = await screen.findByRole('textbox', { name: '任务或问题' })
  fireEvent.change(input, { target: { value: '第一份资料' } })
  await waitFor(() => expect(screen.getByRole('button', { name: '添加文件' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '添加文件' }))
  const materials = await screen.findByRole('dialog', { name: '添加文件' })
  await waitFor(() => expect(router.state.location.search).toBe('?workspace=first'))
  expect(sessionStorage.getItem('clear-audit-new-project')).toBeNull()
  fireEvent.click(within(materials).getByRole('button', { name: /^取\s*消$/ }))
  await act(async () => { await router.navigate('/agent') })
  fireEvent.change(input, { target: { value: '第二份独立资料' } })
  fireEvent.click(screen.getByRole('button', { name: '发送', exact: true }))
  await waitFor(() => expect(writes.some(item => item.path.endsWith('/agent/sessions/second-session/messages'))).toBe(true))
  const creations = writes.filter(item => item.path.endsWith('/workspaces'))
  expect(creations).toHaveLength(2)
  expect(creations[0].body.name).toMatch(/^第一份资料 · /)
  expect(creations[1].body.name).toMatch(/^第二份独立资料 · /)
  expect(creations[1].body.request_id).not.toBe(creations[0].body.request_id)
  expect(writes.some(item => item.path.endsWith('/workspaces/first/agent/sessions'))).toBe(false)
}, 15000)

test.each(['failed', 'partial'])('a recoverable %s run exposes stop only when the backend accepts cancellation', async status => {
  const { writes } = mount('/agent?workspace=original&session=conversation', path => {
    if (path.endsWith('/agent/sessions/conversation')) return ok({ id: 'conversation', workspace_id: 'original', runs: [{ id: 'run', status, can_resume: true }] })
    if (path.endsWith('/runs/run/cancel')) return ok({ id: 'run', status: 'cancelled' })
  })
  await screen.findByRole('button', { name: '继续原任务' })
  if (status === 'failed') {
    expect(screen.queryByRole('button', { name: '停止任务', exact: true })).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('更多', { selector: 'summary' }))
    expect(screen.queryByRole('button', { name: '停止后续步骤', exact: true })).not.toBeInTheDocument()
    expect(writes).toHaveLength(0)
  } else {
    fireEvent.click(screen.getByRole('button', { name: '停止任务', exact: true }))
    await waitFor(() => expect(writes).toEqual([{ path: '/api/platform/v1/agent/runs/run/cancel', body: { cancel_owned_jobs: true } }]))
  }
}, 15000)
