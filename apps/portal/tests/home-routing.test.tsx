import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'
import { navigationHref } from '../src/clear-ui/navigation'

vi.mock('../src/features/agent/AgentWorkbench', () => ({ AgentWorkbench: () => <h1>审计对话工作台</h1> }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mount(url: string) {
  const writes: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (init?.method && init.method !== 'GET') writes.push(input)
    return { ok: true, json: async () => input.includes('/workspaces?')
      ? { items: [{ workspace_id: 'w1', name: '验收项目' }], next_cursor: null, snapshot_revision: 1 }
      : input.endsWith('/audit-overview') ? { tasks: { items: [] }, artifacts: { items: [] } } : { multiuser: false } }
  }))
  const router = createMemoryRouter(routes, { initialEntries: [url] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const view = render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, view, writes }
}

test('plain root is a portal with all tool and work areas and no background writes', async () => {
  const { writes, view } = mount('/')
  expect(await screen.findByRole('heading', { name: /开始今天的工作/ })).toBeInTheDocument()
  const tools = screen.getByRole('navigation', { name: '常用工具' })
  expect(within(tools).getAllByRole('link')).toHaveLength(6)
  expect(screen.getByRole('link', { name: '新任务' })).toHaveAttribute('href', '/agent')
  expect(screen.getByRole('link', { name: /全部工具与专项扩展/ })).toHaveAttribute('href', '/apps')
  expect(screen.queryByRole('heading', { name: '审计对话工作台' })).not.toBeInTheDocument()
  expect(view.container.querySelector('.agent-shell')).toBeNull()
  expect(writes).toEqual([])
})

test('a selected project stays on the portal and passes scope to AI and tools', async () => {
  const { router } = mount('/?workspace=w1&revision=7')
  const tools = await screen.findByRole('navigation', { name: '常用工具' })
  expect(within(tools).getByRole('link', { name: /文档识别/ })).toHaveAttribute('href', '/apps/ocr/start?workspace=w1&revision=7')
  fireEvent.click(screen.getByRole('link', { name: '新任务' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/agent'))
  expect(router.state.location.search).toBe('?workspace=w1&revision=7')
})

test('saved root conversation links redirect with session, continuation and source hash intact', async () => {
  const { router } = mount('/?workspace=w1&session=task1&continue=request1&revision=9#source')
  await screen.findByRole('heading', { name: '审计对话工作台' })
  expect(router.state.location.pathname).toBe('/agent')
  expect(router.state.location.search).toBe('?workspace=w1&session=task1&continue=request1&revision=9')
  expect(router.state.location.hash).toBe('#source')
  expect(router.state.historyAction).toBe('REPLACE')
})

test('brand goes to the portal without replaying the current AI task', async () => {
  const { router } = mount('/agent?workspace=w1&session=task1&continue=request1')
  await screen.findByRole('heading', { name: '审计对话工作台' })
  fireEvent.click(screen.getByRole('link', { name: 'Clear 智能审计 · 返回审计平台首页' }))
  await screen.findByRole('heading', { name: /开始今天的工作/ })
  expect(router.state.location.pathname).toBe('/')
  expect(router.state.location.search).toBe('?workspace=w1')
})

test('task navigation carries return context to tools and restores the AI session', async () => {
  const { router } = mount('/agent?workspace=w1&session=task1&continue=request1&revision=9')
  await screen.findByRole('heading', { name: '审计对话工作台' })
  const nav = screen.getByRole('menu', { name: '主导航' })
  fireEvent.click(within(nav).getByText('项目'))
  await waitFor(() => expect(router.state.location.pathname).toBe('/projects'))
  expect(router.state.location.search).toBe('?workspace=w1&revision=9&task_return=task1')
  fireEvent.click(within(nav).getByText('Agent'))
  await waitFor(() => expect(router.state.location.pathname).toBe('/agent'))
  expect(router.state.location.search).toBe('?workspace=w1&revision=9&session=task1')
})

test('native data session IDs cannot become AI session IDs when switching areas', () => {
  expect(navigationHref('/agent', '?workspace=w1&session=dataSession&revision=4', '', '/apps/data'))
    .toBe('/agent?workspace=w1&revision=4')
  expect(navigationHref('/agent', '?workspace=w1&session=dataSession&task_return=auditTask&revision=4', '', '/apps/data'))
    .toBe('/agent?workspace=w1&revision=4&session=auditTask')
})
