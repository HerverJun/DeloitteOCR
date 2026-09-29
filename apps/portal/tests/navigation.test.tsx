import '@testing-library/jest-dom/vitest'
import { beforeAll, beforeEach, afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  vi.restoreAllMocks()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(cleanup)

const page = (items: unknown[]) => ({ schema_version: '1.0', items, next_cursor: null, snapshot_revision: 2 })
const app = (id: string) => ({ schema_version: '1.0', app_id: id, display_name: id === 'ocr' ? '文档识别' : '图像分类', app_version: '1.0',
  adapter_version: '1.0', min_platform_version: '1.0', data_schema_version: '1', entry_mode: 'standalone_page',
  capabilities: [], input_types: [], output_types: [], resources: [], license_refs: [] })

function mount(url: string) {
  const router = createMemoryRouter(routes, { initialEntries: [url] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const view = render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, client, view }
}

test('deep links, context-aware navigation and history reach distinct pages', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => ({ ok: true, json: async () =>
    input.includes('/jobs/links') ? { items: [] } :
    input.includes('/jobs?') ? { items: [], next_offset: null } :
    input.includes('/workspaces?') ? page([{ workspace_id: 'a', name: '甲' }, { workspace_id: 'b', name: '乙' }]) :
    input.includes('/apps') ? page([app('ocr')]) :
    input.includes('/assets?') ? { items: [], total: 0, next_offset: null, facets: { media_types: [], tags: [] } } :
    { service: 'ready' } })))
  const { router } = mount('/assets?workspace=a')
  expect(await screen.findByRole('heading', { name: '资料库' })).toBeInTheDocument()
  fireEvent.click(screen.getByText('任务', { selector: '.ant-menu-title-content' }))
  expect(await screen.findByText('此工作空间还没有已跟踪任务')).toBeInTheDocument()
  expect(router.state.location.search).toBe('?workspace=a')
  await router.navigate(-1)
  expect(await screen.findByRole('heading', { name: '资料库' })).toBeInTheDocument()
  await router.navigate('/absent?workspace=b')
  expect(await screen.findByText('页面不存在')).toBeInTheDocument()
  fireEvent.click(screen.getByText('返回首页 →'))
  expect(await screen.findByText('从这里开始作业')).toBeInTheDocument()
})

test('workspace and application query identities cannot display a late old response', async () => {
  let finishOld: ((value: unknown) => void) | undefined
  let oldSignal: AbortSignal | undefined
  const fetcher = vi.fn((input: string, options?: RequestInit) => {
    if (input.includes('/workspaces')) return Promise.resolve({ ok: true, json: async () => page([{ workspace_id: 'a', name: '甲' }, { workspace_id: 'b', name: '乙' }]) })
    if (input.includes('/health')) return Promise.resolve({ ok: true, json: async () => ({ service: { state: 'ready', reason: '' }, ui: { state: 'unknown', reason: '' }, cpu: { state: 'unknown', reason: '' }, gpu: { state: 'unknown', reason: '' }, models: { state: 'unknown', reason: '' } }) })
    if (input.includes('/apps?')) return Promise.resolve({ ok: true, json: async () => page([app('ocr'), app('classification')]) })
    if (input.includes('/apps/ocr')) { oldSignal = options?.signal as AbortSignal; return new Promise(resolve => { finishOld = resolve }) }
    if (input.includes('/apps/classification')) return Promise.resolve({ ok: true, json: async () => app('classification') })
    throw new Error(`Unexpected URL ${input}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const { router, client } = mount('/apps/ocr?workspace=a')
  await waitFor(() => expect(finishOld).toBeDefined())
  await router.navigate('/apps/classification?workspace=b')
  expect(await screen.findByText('图像分类', { selector: 'h1' })).toBeInTheDocument()
  expect(oldSignal?.aborted).toBe(true)
  finishOld!({ ok: true, json: async () => app('ocr') })
  await waitFor(() => expect(screen.queryByText('文档识别', { selector: 'h1' })).not.toBeInTheDocument())
  expect(client.getQueryData(['app', 'b', 'classification'])).toBeDefined()
  expect(fetcher.mock.calls.some(([url]) => url.includes('/apps/classification'))).toBe(true)
})

test('failed catalog request offers a working retry', async () => {
  let attempts = 0
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.includes('/workspaces')) return { ok: true, json: async () => page([]) }
    if (input.includes('/apps?')) {
      if (++attempts === 1) return { ok: false, status: 503 }
      return { ok: true, json: async () => page([app('ocr')]) }
    }
    if (input.includes('/launches/current')) return { ok: false, status: 404 }
    if (input.includes('/installation')) return { ok: true, json: async () => ({ schema_version: '1.0', app_id: 'ocr', installed: false, launch_ready: false, state: 'not_registered' }) }
    if (input.includes('/health')) return { ok: true, json: async () => ({ service: { state: 'unavailable', reason: 'Not registered' } }) }
    return { ok: true, json: async () => page([]) }
  }))
  mount('/apps')
  expect(await screen.findByText('暂时无法取得服务数据')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }))
  expect(await screen.findByText('文档识别')).toBeInTheDocument()
})

test('clearing selection removes URL scope; unknown scope and unsafe app path never fetch', async () => {
  const fetcher = vi.fn(async (input: string) => ({ ok: true, json: async () =>
    input.includes('/workspaces') ? page([{ workspace_id: 'a', name: '甲' }]) : page([app('ocr')]) }))
  vi.stubGlobal('fetch', fetcher)
  const { router, view } = mount('/assets?workspace=a')
  await waitFor(() => expect(view.container.querySelector('.ant-select-clear')).toBeTruthy())
  fireEvent.mouseDown(view.container.querySelector('.ant-select-clear')!)
  fireEvent.click(view.container.querySelector('.ant-select-clear')!)
  await waitFor(() => expect(router.state.location.search).toBe(''))
  await router.navigate(-1)
  expect(router.state.location.search).toBe('?workspace=a')
  await router.navigate('/assets')
  await router.navigate('/assets?workspace=missing')
  expect(await screen.findByText('工作空间不存在或已失效')).toBeInTheDocument()
  fireEvent.click(screen.getByText('清除工作空间'))
  await waitFor(() => expect(router.state.location.search).toBe(''))
  await router.navigate('/apps/%2Fprivate')
  expect(await screen.findByText('页面不存在')).toBeInTheDocument()
  expect(fetcher.mock.calls.every(([url]) => !url.includes('private'))).toBe(true)
})

test('create rejects a blank name and uses persisted identity before slow list refresh', async () => {
  const created = { schema_version: '1.0', workspace_id: 'newA', name: '新项目', revision: 1,
    created_at: '2026-09-25T01:00:00Z', updated_at: '2026-09-25T01:00:00Z' }
  let listCalls = 0
  const fetcher = vi.fn((input: string, options?: RequestInit) => {
    if (input.includes('/workspaces') && options?.method === 'POST') return Promise.resolve({ ok: true, json: async () => created })
    if (input.includes('/workspaces')) {
      if (++listCalls > 1) return new Promise(() => {})
      return Promise.resolve({ ok: true, json: async () => page([]) })
    }
    return Promise.resolve({ ok: true, json: async () => page([]) })
  })
  vi.stubGlobal('fetch', fetcher)
  const { router } = mount('/workspaces')
  await screen.findByText('目前没有记录')
  fireEvent.click(screen.getByText('新建工作空间'))
  fireEvent.click(screen.getByRole('button', { name: /创\s*建/ }))
  await screen.findByText('请输入名称')
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
  fireEvent.change(screen.getByPlaceholderText('例如：季度资料整理'), { target: { value: '新项目' } })
  fireEvent.click(screen.getByRole('button', { name: /创\s*建/ }))
  await waitFor(() => expect(router.state.location.search).toBe('?workspace=newA'))
  expect(screen.queryByText('工作空间不存在或已失效')).not.toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  expect(JSON.parse(fetcher.mock.calls.find(([, options]) => options?.method === 'POST')![1]!.body as string)).toEqual({ schema_version: '1.0', name: '新项目' })
})
