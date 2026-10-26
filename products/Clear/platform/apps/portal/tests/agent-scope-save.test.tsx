import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { AgentWorkbench } from '../src/features/agent/AgentWorkbench'

vi.mock('../src/features/agent/Materials', () => ({
  Materials: ({ open, selected, save }: { open: boolean; selected: string[]; save: (ids: string[]) => Promise<void> }) => open ? <section aria-label="附件选择测试">
    <output aria-label="已选附件">{selected.join(',')}</output>
    <button onClick={() => { void save(['old-asset']) }}>保存旧附件</button>
  </section> : null,
  ProjectBackground: () => null,
}))

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })

const response = (value: unknown) => ({ ok: true, json: async () => value })
class SilentSource { readyState = 1; onopen = null; onmessage = null; onerror = null; close() {} }

test('a late attachment save updates only its original task after switching to another project draft', async () => {
  vi.stubGlobal('EventSource', SilentSource)
  const originalDraft = { goal: '旧会话草稿', assets: ['original-asset'], authorized_reference_ids: [], delivery_policy: 'auto' }
  const otherDraft = { goal: '新项目草稿', assets: ['other-asset'], authorized_reference_ids: [], delivery_policy: 'auto' }
  localStorage.setItem('clear-audit-draft:original:conversation', JSON.stringify(originalDraft))
  localStorage.setItem('clear-audit-draft:other:new', JSON.stringify(otherDraft))
  let originalTask = { id: 'conversation', session_id: 'conversation', workspace_id: 'original', title: '旧项目任务', goal: '',
    revision: 7, scope_revision: 2, status: 'draft', manual: false, pending_scope: null, selected_assets: ['original-asset'],
    authorized_reference_ids: [], initial_refs: [], plan: [], job_refs: [], result_refs: [], artifact_refs: [], runs: [] }
  const writes: { path: string; body: Record<string, unknown> }[] = []
  let completeSave!: () => void
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method) {
      writes.push({ path, body: JSON.parse(String(init.body)) })
      return new Promise(resolve => { completeSave = () => {
        originalTask = { ...originalTask, revision: 8, selected_assets: ['old-asset'] }
        resolve(response(originalTask))
      } })
    }
    return response(path.endsWith('/health') ? { enabled: true, direct_enabled: true } :
      path.endsWith('/connection') ? { configured: true, available: true, model: 'connected' } :
      path.endsWith('/agent/sessions/conversation/task') ? originalTask :
      path.endsWith('/agent/sessions/conversation') ? { id: 'conversation', workspace_id: 'original', runs: [] } :
      path.includes('/events?') ? { items: [], next_cursor: 0 } :
      path.endsWith('/jobs') ? { jobs: [], sampled_at: '2026-10-25T05:00:00Z' } : { items: [] })
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const invalidation = vi.spyOn(client, 'invalidateQueries')
  const router = createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }], {
    initialEntries: ['/agent?workspace=original&session=conversation'],
  })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  await screen.findByRole('heading', { name: '旧项目任务', level: 2 })
  fireEvent.click(screen.getByRole('button', { name: '添加文件' }))
  fireEvent.click(await screen.findByRole('button', { name: '保存旧附件' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0]).toMatchObject({ path: '/api/platform/v1/agent/sessions/conversation/task', body: {
    expected_revision: 7, selected_assets: ['old-asset'], request_id: expect.any(String),
  } })

  await act(async () => { await router.navigate('/agent?workspace=other') })
  await waitFor(() => expect(screen.getByRole('textbox', { name: '任务或问题' })).toHaveValue(otherDraft.goal))
  invalidation.mockClear()
  await act(async () => { completeSave() })
  await waitFor(() => expect(invalidation).toHaveBeenCalledWith({ queryKey: ['audit-tasks', 'original'] }))

  expect(router.state.location.search).toBe('?workspace=other')
  expect(localStorage.getItem('clear-audit-draft:other:new')).toBe(JSON.stringify(otherDraft))
  expect(JSON.parse(localStorage.getItem('clear-audit-draft:original:conversation')!)).toMatchObject({ ...originalDraft, assets: ['old-asset'] })
  expect(screen.getByRole('textbox', { name: '任务或问题' })).toHaveValue(otherDraft.goal)
  if (!screen.queryByLabelText('已选附件')) fireEvent.click(screen.getByRole('button', { name: '添加文件' }))
  expect(await screen.findByLabelText('已选附件')).toHaveTextContent('other-asset')
  expect(screen.getByLabelText('已选附件')).not.toHaveTextContent('old-asset')
  expect(invalidation.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
    ['audit-task', 'conversation'], ['agent-session', 'conversation'], ['audit-tasks', 'original'],
  ])
  expect(originalTask).toMatchObject({ workspace_id: 'original', revision: 8, selected_assets: ['old-asset'] })
  expect(writes).toHaveLength(1)
})
