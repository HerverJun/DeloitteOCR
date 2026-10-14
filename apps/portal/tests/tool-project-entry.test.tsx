import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'

type Project = { workspace_id: string; name: string }
type CreateRequest = { schema_version: string; name: string; request_id: string }

const mounted: ReturnType<typeof mount>[] = []
const page = (items: unknown[]) => ({ schema_version: '1.0', items, next_cursor: null, snapshot_revision: 1, total: items.length })
const ok = (value: unknown) => ({ ok: true, json: async () => value })
const existing = { workspace_id: 'wExisting', name: '已有取证项目' }
const created = { schema_version: '1.0', workspace_id: 'wCreated', name: '合同图片核查', revision: 1,
  created_at: '2026-10-06T01:00:00Z', updated_at: '2026-10-06T01:00:00Z' }

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.stubGlobal('crypto', { randomUUID })
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => {
  cleanup()
  for (const item of mounted.splice(0)) { item.router.dispose(); item.client.clear() }
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
})

function mount(url: string) {
  const router = createMemoryRouter(routes, { initialEntries: [url] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const view = render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  const item = { router, client, view }
  mounted.push(item)
  return item
}

function toolRead(input: string, projects: Project[]) {
  const url = new URL(input, 'http://localhost')
  if (url.pathname === '/api/platform/v1/workspaces') return ok(page(projects))
  if (/\/workspaces\/[^/]+\/assets$/.test(url.pathname)) return ok({ items: [], total: 0, next_offset: null })
  if (/\/workspaces\/[^/]+\/forensics$/.test(url.pathname)) return ok({ items: [], total: 0, next_offset: null })
  if (/\/workspaces\/[^/]+\/(ocr|classification)\/projects$/.test(url.pathname)) return ok({ items: [] })
  throw new Error(`Unexpected request ${input}`)
}

function expectContext(router: ReturnType<typeof createMemoryRouter>, path: string, workspace: string) {
  expect(router.state.location.pathname).toBe(path)
  const params = new URLSearchParams(router.state.location.search)
  expect(params.get('workspace')).toBe(workspace)
  expect(params.get('source')).toBe('catalog')
  expect(params.get('bundle')).toBe('fixed')
  expect(params.has('create')).toBe(false)
  expect(router.state.location.hash).toBe('#evidence')
}

test.each(['/apps/forensics', '/apps/ocr/start', '/apps/classification/start'])(
  '%s selects an existing project and continues in the original tool without creating anything', async path => {
    const popup = vi.spyOn(window, 'open')
    const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
      if (init?.method && init.method !== 'GET') throw new Error(`Unexpected write ${input}`)
      return toolRead(input, [existing])
    })
    vi.stubGlobal('fetch', fetcher)
    const { router } = mount(`${path}?source=catalog&bundle=fixed#evidence`)
    expect(await screen.findByRole('heading', { name: '选择项目后继续' })).toBeInTheDocument()
    const link = await screen.findByRole('link', { name: existing.name })
    expect(link).toHaveAttribute('href', expect.stringContaining(path))
    expect(fetcher.mock.calls.filter(([, init]) => init?.method && init.method !== 'GET')).toHaveLength(0)
    fireEvent.click(link)
    if (path === '/apps/forensics') {
      expect(await screen.findByText('选择图片与取证工具')).toBeInTheDocument()
    } else {
      expect(await screen.findByRole('button', { name: '开始处理' })).toBeInTheDocument()
    }
    expectContext(router, path, existing.workspace_id)
    expect(screen.queryByRole('heading', { name: '选择项目后继续' })).not.toBeInTheDocument()
    expect(fetcher.mock.calls.filter(([, init]) => init?.method && init.method !== 'GET')).toHaveLength(0)
    expect(fetcher.mock.calls.every(([url]) => !url.includes('/agent/'))).toBe(true)
    expect(popup).not.toHaveBeenCalled()
  }, 15000,
)

test('first forensic use creates one project and proceeds to its workbench while retaining the original context', async () => {
  const projects: Project[] = [], requests: CreateRequest[] = []
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (input === '/api/platform/v1/workspaces' && init?.method === 'POST') {
      requests.push(JSON.parse(init.body as string))
      projects.push(created)
      return ok(created)
    }
    if (init?.method && init.method !== 'GET') throw new Error(`Unexpected write ${input}`)
    return toolRead(input, projects)
  })
  vi.stubGlobal('fetch', fetcher)
  const { router } = mount('/apps/forensics?source=catalog&create=1&bundle=fixed#evidence')
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText('新建项目')).toBeInTheDocument()
  expect(requests).toHaveLength(0)
  fireEvent.change(within(dialog).getByRole('textbox', { name: '名称' }), { target: { value: created.name } })
  fireEvent.click(within(dialog).getByRole('button', { name: '创建并继续' }))
  expect(await screen.findByText('选择图片与取证工具')).toBeInTheDocument()
  expectContext(router, '/apps/forensics', created.workspace_id)
  expect(requests).toEqual([{ schema_version: '1.0', name: created.name, request_id: expect.stringMatching(/^w[A-Za-z0-9]+$/) }])
  expect(localStorage.getItem('clear-workspace-create')).toBeNull()
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  expect(screen.queryByRole('heading', { name: '从一个审计目标开始' })).not.toBeInTheDocument()
  expect(fetcher.mock.calls.every(([url]) => !url.includes('/agent/'))).toBe(true)
}, 15000)

test('a lost project creation reply survives refresh and resumes the same request into the original tool', async () => {
  const projects: Project[] = [], requests: CreateRequest[] = []
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (input === '/api/platform/v1/workspaces' && init?.method === 'POST') {
      requests.push(JSON.parse(init.body as string))
      if (requests.length === 1) { projects.push(created); throw new TypeError('响应丢失') }
      return ok(created)
    }
    if (init?.method && init.method !== 'GET') throw new Error(`Unexpected write ${input}`)
    return toolRead(input, projects)
  })
  vi.stubGlobal('fetch', fetcher)
  const url = '/apps/forensics?source=catalog&create=1&bundle=fixed#evidence'
  const first = mount(url)
  const firstDialog = await screen.findByRole('dialog')
  expect(within(firstDialog).getByText('新建项目')).toBeInTheDocument()
  fireEvent.change(within(firstDialog).getByRole('textbox', { name: '名称' }), { target: { value: created.name } })
  fireEvent.click(within(firstDialog).getByRole('button', { name: '创建并继续' }))
  await screen.findByText('响应丢失')
  const pending = JSON.parse(localStorage.getItem('clear-workspace-create')!) as CreateRequest
  expect(pending).toEqual(requests[0])
  expect(first.router.state.location.pathname).toBe('/apps/forensics')
  expect(new URLSearchParams(first.router.state.location.search).has('workspace')).toBe(false)
  first.view.unmount()
  first.router.dispose()
  first.client.clear()
  const second = mount(url)
  const secondDialog = await screen.findByRole('dialog')
  expect(within(secondDialog).getByText('新建项目')).toBeInTheDocument()
  await waitFor(() => expect(within(secondDialog).getByRole('textbox', { name: '名称' })).toHaveValue(created.name))
  expect(requests).toHaveLength(1)
  fireEvent.click(within(secondDialog).getByRole('button', { name: '创建并继续' }))
  expect(await screen.findByText('选择图片与取证工具')).toBeInTheDocument()
  expectContext(second.router, '/apps/forensics', created.workspace_id)
  expect(requests).toEqual([pending, pending])
  expect(projects).toHaveLength(1)
  expect(localStorage.getItem('clear-workspace-create')).toBeNull()
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(2)
  expect(fetcher.mock.calls.every(([input, init]) => init?.method !== 'POST' || input === '/api/platform/v1/workspaces')).toBe(true)
}, 20000)

test('a forensic link with an existing project opens directly without showing project setup or performing a write', async () => {
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (init?.method && init.method !== 'GET') throw new Error(`Unexpected write ${input}`)
    return toolRead(input, [existing])
  })
  vi.stubGlobal('fetch', fetcher)
  const { router } = mount('/apps/forensics?workspace=wExisting&source=catalog&bundle=fixed#evidence')
  expect(await screen.findByText('选择图片与取证工具')).toBeInTheDocument()
  expectContext(router, '/apps/forensics', existing.workspace_id)
  expect(screen.queryByRole('heading', { name: '选择项目后继续' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '新建项目' })).not.toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([, init]) => init?.method && init.method !== 'GET')).toHaveLength(0)
})

test('a late creation reply records the project without navigating away from the user’s chosen project', async () => {
  const projects: Project[] = [existing]
  let completeCreation: ((value: ReturnType<typeof ok>) => void) | undefined
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (input === '/api/platform/v1/workspaces' && init?.method === 'POST') {
      return new Promise<ReturnType<typeof ok>>(resolve => { completeCreation = resolve })
    }
    if (init?.method && init.method !== 'GET') throw new Error(`Unexpected write ${input}`)
    return toolRead(input, projects)
  })
  vi.stubGlobal('fetch', fetcher)
  const { router, client } = mount('/apps/forensics?source=catalog&create=1&bundle=fixed#evidence')
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText('新建项目')).toBeInTheDocument()
  fireEvent.change(within(dialog).getByRole('textbox', { name: '名称' }), { target: { value: created.name } })
  fireEvent.click(within(dialog).getByRole('button', { name: '创建并继续' }))
  await waitFor(() => expect(completeCreation).toBeDefined())
  expect(within(dialog).getByRole('button', { name: /取\s*消/ })).toBeDisabled()
  expect(localStorage.getItem('clear-workspace-create')).not.toBeNull()
  await router.navigate('/apps/forensics?workspace=wExisting&source=catalog&bundle=fixed#evidence')
  expect(await screen.findByText('选择图片与取证工具')).toBeInTheDocument()
  expectContext(router, '/apps/forensics', existing.workspace_id)
  projects.push(created)
  completeCreation!(ok(created))
  await waitFor(() => expect(localStorage.getItem('clear-workspace-create')).toBeNull())
  await waitFor(() => expect(client.getQueryData<Project[]>(['workspaces'])).toContainEqual(created))
  expectContext(router, '/apps/forensics', existing.workspace_id)
  expect(screen.getByText('选择图片与取证工具')).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '选择项目后继续' })).not.toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
}, 15000)
