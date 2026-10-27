import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })

function mount(url: string) {
  const writes: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (init?.method && init.method !== 'GET') writes.push(input)
    if (input.endsWith('/agent/sessions/auditTask/task')) return { ok: true, json: async () => ({ id: 'auditTask', workspace_id: 'w1', title: '原始任务', revision: 1, manual: true, status: 'paused', selected_assets: [], initial_refs: [], result_refs: [], job_refs: [] }) }
    return { ok: true, json: async () => input.includes('/workspaces?')
      ? { items: [{ workspace_id: 'w1', name: '验收项目' }], next_cursor: null, snapshot_revision: 1 }
      : input.endsWith('/audit-overview') ? { profile: {}, asset_count: 0, tasks: { items: [] }, findings: { items: [], total: 0 }, artifacts: { items: [], total: 0 }, formal_projects: { items: [], total: 0 } } : { multiuser: false } }
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
  expect(within(tools).getByRole('link', { name: /文档主题对比/ })).toHaveAttribute('href', '/apps/comparisons')
  expect(screen.getByRole('link', { name: '新任务' })).toHaveAttribute('href', '/agent')
  expect(screen.getByRole('link', { name: /查看全部工具/ })).toHaveAttribute('href', '/apps')
  expect(screen.getByRole('link', { name: /直接使用工具/ })).toHaveAttribute('href', '/apps')
  expect(within(tools).getByRole('link', { name: /整图 AI 生成检测/ })).toHaveAttribute('href', '/apps/forensics/aigc')
  expect(screen.queryByRole('heading', { name: '审计对话工作台' })).not.toBeInTheDocument()
  expect(view.container.querySelector('.agent-shell')).toBeNull()
  expect(writes).toEqual([])
})

test('the global tool picker includes specialist tools and keeps only project and Agent context', async () => {
  const { writes } = mount('/?workspace=w1&task_return=auditTask&analysis=oldImage')
  fireEvent.click(await screen.findByRole('button', { name: '选择工具' }))
  const picker = await screen.findByRole('navigation', { name: '工具快捷入口' })
  expect(within(picker).getAllByRole('link')).toHaveLength(11)
  expect(within(picker).getByRole('link', { name: /整图 AI 生成检测/ })).toHaveAttribute('href', '/apps/forensics/aigc?workspace=w1&task_return=auditTask')
  expect(within(picker).getByRole('link', { name: /印章识别/ })).toHaveAttribute('href', '/apps/ocr/seals?workspace=w1&task_return=auditTask')
  expect(within(picker).getByRole('link', { name: /SAFIRE 深度定位/ })).toHaveAttribute('href', '/apps/forensics/deep?workspace=w1&task_return=auditTask')
  expect(writes).toEqual([])
})

test('a selected project stays on the portal and passes scope to AI and tools', async () => {
  const { router } = mount('/?workspace=w1&revision=7')
  const tools = await screen.findByRole('navigation', { name: '常用工具' })
  expect(within(tools).getByRole('link', { name: /文档识别/ })).toHaveAttribute('href', '/apps/ocr/start?workspace=w1')
  fireEvent.click(screen.getByRole('link', { name: '新任务' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/agent'))
  expect(router.state.location.search).toBe('?workspace=w1')
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
  expect(router.state.location.search).toBe('?workspace=w1&task_return=task1')
  fireEvent.click(within(nav).getByText('Agent'))
  await waitFor(() => expect(router.state.location.pathname).toBe('/agent'))
  expect(router.state.location.search).toBe('?workspace=w1&session=task1')
})

test('native data session IDs cannot become AI session IDs when switching areas', () => {
  expect(navigationHref('/agent', '?workspace=w1&session=dataSession&revision=4', '', '/apps/data'))
    .toBe('/agent?workspace=w1')
  expect(navigationHref('/agent', '?workspace=w1&session=dataSession&task_return=auditTask&revision=4', '', '/apps/data'))
    .toBe('/agent?workspace=w1&session=auditTask')
})

test('home intent carries the selected purpose while project links open the same project overview', async () => {
  await import('../src/features/agent/ProjectHome')
  const { router, writes } = mount('/?workspace=w1')
  const intent = await screen.findByRole('link', { name: /文件与表格/ })
  expect(intent).toHaveAttribute('href', '/agent?workspace=w1&intent=files')
  await act(async () => { await router.navigate('/projects') })
  const project = await screen.findByRole('link', { name: '验收项目', exact: true })
  expect(project).toHaveAttribute('href', '/projects?workspace=w1')
  expect(screen.getByRole('link', { name: '开始任务' })).toHaveAttribute('href', '/agent?workspace=w1')
  fireEvent.click(project)
  expect(await screen.findByRole('heading', { name: '项目概况' })).toBeInTheDocument()
  expect(writes).toEqual([])
})
