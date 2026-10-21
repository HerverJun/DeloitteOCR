import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { AgentWorkbench } from '../src/features/agent/AgentWorkbench'
import { readRecentVisits, rememberVisit } from '../src/clear-ui/recentVisits'
import { Workspaces } from '../src/App'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })

test('navigation history isolates accounts and strips launch credentials', () => {
  rememberVisit('alice', { href: '/agent?workspace=w1&session=s1&platform_ticket=secret', title: '核对台账', workspace: 'w1' })
  expect(readRecentVisits('alice')[0].href).toBe('/agent?workspace=w1&session=s1')
  expect(readRecentVisits('bob')).toEqual([])
  rememberVisit('alice', { href: 'https://outside.example/agent?session=s1', title: '外部地址', workspace: null })
  expect(readRecentVisits('alice')).toHaveLength(1)
})

test('unavailable storage does not prevent starting work', () => {
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Storage disabled') })
  expect(() => rememberVisit('single', { href: '/agent', title: 'Agent', workspace: null })).not.toThrow()
  write.mockRestore()
})

test('project search filters loaded projects without creating a project or starting tools', async () => {
  const writes: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method && init.method !== 'GET') writes.push(url)
    return { ok: true, json: async () => ({ items: [{ workspace_id: 'w1', name: '采购核对' }, { workspace_id: 'w2', name: '合同整理' }], snapshot_revision: 1, next_cursor: null }) }
  }))
  const router = createMemoryRouter([{ path: '*', element: <Workspaces /> }], { initialEntries: ['/projects'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  await screen.findByRole('link', { name: '采购核对' })
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索项目' }), { target: { value: '合同' } })
  expect(screen.queryByRole('link', { name: '采购核对' })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: '合同整理' })).toBeInTheDocument()
  expect(writes).toEqual([])
})

test('IME composition and Shift+Enter keep the draft; Enter submits exactly one existing-session message', async () => {
  const writes: { url: string; body: unknown }[] = []
  vi.stubGlobal('EventSource', class { close() {} })
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') { writes.push({ url, body: JSON.parse(String(init.body)) }); return { ok: true, json: async () => ({ id: 'run1', status: 'running' }) } }
    const value = url.endsWith('/agent/health') ? { enabled: true } : url.endsWith('/agent/connection') ? { available: true, model: 'test' } :
      url.endsWith('/task') ? { id: 's1', session_id: 's1', workspace_id: 'w1', title: '已有会话', goal: '', revision: 1, scope_revision: 1, status: 'answered', manual: false, selected_assets: [], plan: [], runs: [], result_refs: [], artifact_refs: [], job_refs: [], initial_refs: [] } :
        url.endsWith('/agent/sessions/s1') ? { id: 's1', workspace_id: 'w1', runs: [] } : { items: [], next_cursor: 0 }
    return { ok: true, json: async () => value }
  }))
  const router = createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }], { initialEntries: ['/agent?workspace=w1&session=s1'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  const input = await screen.findByRole('textbox', { name: '任务或问题' })
  await waitFor(() => expect(screen.getByRole('heading', { name: '已有会话' })).toBeInTheDocument())
  fireEvent.change(input, { target: { value: '核对金额' } })
  fireEvent.compositionStart(input)
  fireEvent.keyDown(input, { key: 'Enter', keyCode: 229, isComposing: true })
  fireEvent.compositionEnd(input)
  fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
  expect(writes).toEqual([])
  expect(input).toHaveValue('核对金额')
  fireEvent.keyDown(input, { key: 'Enter' })
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0].url).toBe('/api/platform/v1/agent/sessions/s1/messages')
  expect(writes[0].body).toMatchObject({ content: '核对金额' })
  expect(router.state.location.search).toBe('?workspace=w1&session=s1')
})
