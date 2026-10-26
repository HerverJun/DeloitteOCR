import '@testing-library/jest-dom/vitest'
import { beforeAll, beforeEach, afterEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
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
    input.includes('/forensics?') ? page([]) :
    input.includes('/workspaces?') ? page([{ workspace_id: 'a', name: '甲' }, { workspace_id: 'b', name: '乙' }]) :
    input.includes('/apps') ? page([app('ocr')]) :
    input.includes('/assets?') ? { items: [], total: 0, next_offset: null, facets: { media_types: [], tags: [] } } :
    { service: 'ready' } })))
  const { router } = mount('/assets?workspace=a')
  expect(await screen.findByRole('heading', { name: '资料库' })).toBeInTheDocument()
  fireEvent.click(screen.getByText('正式档案与运行详情'))
  fireEvent.click(screen.getByRole('link', { name: '运行任务' }))
  expect(await screen.findByText('此工作空间还没有已跟踪任务')).toBeInTheDocument()
  expect(router.state.location.search).toBe('?workspace=a')
  await router.navigate(-1)
  expect(await screen.findByRole('heading', { name: '资料库' })).toBeInTheDocument()
  await router.navigate('/absent?workspace=b')
  expect(await screen.findByText('页面不存在')).toBeInTheDocument()
  fireEvent.click(screen.getByText('返回首页 →'))
  expect(await screen.findByRole('heading', { name: /开始今天的工作/ })).toBeInTheDocument()
  expect(screen.queryByPlaceholderText('输入审计目标，或向大模型提问…')).not.toBeInTheDocument()
}, 15000)

test('project sections keep source identities and hashes on legacy deep links without changing task scope', async () => {
  const writes: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (init?.method) writes.push(input)
    return { ok: true, json: async () => input.includes('/workspaces?') ? page([{ workspace_id: 'a', name: '甲' }]) : input.includes('/assets?') ? { items: [], total: 0, next_offset: null, facets: { media_types: [], tags: [] } } : { id: 'task', workspace_id: 'a', manual: false, title: '任务' } }
  }))
  const query = '?workspace=a&session=task&bundle=fixed&task_return=task&revision=3'
  const { view } = mount(`/assets${query}#source`)
  const project = await screen.findByRole('navigation', { name: '项目内容' })
  for (const [label, path] of [['项目概况', '/projects'], ['资料', '/assets'], ['发现', '/findings'], ['成果', '/artifacts']]) expect(within(project).getByRole('link', { name: label })).toHaveAttribute('href', path + query + '#source')
  expect(view.container.querySelector('.ant-menu-item-selected')).toHaveTextContent('项目')
  expect(screen.getAllByRole('combobox', { name: '当前项目' })).toHaveLength(1)
  expect(writes).toHaveLength(0)
})

test('Clear tools share one directory and workflow groups preserve workspace navigation', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.includes('/workspaces?')) return { ok: true, json: async () => page([{ workspace_id: 'a', name: '甲' }]) }
    if (input.includes('/apps?')) return { ok: true, json: async () => page([app('classification'), app('ocr')]) }
    if (input.includes('/launches/current')) return { ok: false, status: 404 }
    if (input.includes('/installation')) return { ok: true, json: async () => ({ installed: false, launch_ready: false, state: 'not_registered' }) }
    if (input.includes('/data/health')) return { ok: true, json: async () => ({ ready: true }) }
    if (input.includes('/health')) return { ok: true, json: async () => ({ service: { state: 'unavailable', reason: '本机未登记安装包' } }) }
    return { ok: true, json: async () => page([]) }
  }))
  const { router, view } = mount('/apps?workspace=a')
  const directory = await screen.findByLabelText('Clear 应用目录')
  for (const name of ['ClearData', 'ClearClassify', 'ClearOCR', 'ClearForensics']) {
    expect(await within(directory).findByRole('link', { name })).toHaveAttribute('href', expect.stringContaining('workspace=a'))
  }
  expect(directory.querySelectorAll('.application-card')).toHaveLength(8)
  expect(within(directory).getByRole('button', { name: /主题对比/ })).toBeInTheDocument()
  expect(within(directory).getByRole('button', { name: '打开人脸比对' })).toBeInTheDocument()
  expect(within(directory).getByRole('button', { name: '打开音频分析' })).toBeInTheDocument()
  const navigation = screen.getByRole('menu', { name: '主导航' })
  for (const entry of ['工作首页', 'Agent', '项目', '工具']) expect(within(navigation).getByText(entry)).toBeInTheDocument()
  expect(within(navigation).getAllByRole('menuitem')).toHaveLength(4)
  expect(within(navigation).queryByText('成果文件')).not.toBeInTheDocument()
  expect(within(navigation).queryByText('图像取证')).not.toBeInTheDocument()
  expect(within(navigation).queryByText('ClearForensics')).not.toBeInTheDocument()
  fireEvent.click(within(directory).getByRole('button', { name: '打开图像取证' }))
  expect(await screen.findByRole('heading', { name: 'ClearForensics', level: 1 })).toBeInTheDocument()
  expect(router.state.location.pathname).toBe('/apps/forensics')
  expect(router.state.location.search).toBe('?workspace=a')
  expect(view.container.querySelector('.ant-menu-item-selected')).toHaveTextContent('工具')
  fireEvent.click(screen.getByRole('link', { name: '← 返回应用目录' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/apps'))
  expect(router.state.location.search).toBe('?workspace=a')
}, 15000)

test('legacy forensic report links retain all context when redirected to the application', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.includes('/workspaces?')) return { ok: true, json: async () => page([{ workspace_id: 'a', name: '甲' }]) }
    if (input.includes('/forensics/old-report')) return { ok: false, status: 404, json: async () => ({ detail: '报告不存在' }) }
    return { ok: true, json: async () => ({ ...page([]), total: 0 }) }
  }))
  const query = '?workspace=a&inspection=old-report&return_to=%2Faudit%3Fproject%3Dp1'
  const { router, view } = mount(`/forensics${query}#heatmap`)
  expect(await screen.findByRole('heading', { name: 'ClearForensics', level: 1 })).toBeInTheDocument()
  expect(router.state.location.pathname).toBe('/apps/forensics')
  expect(router.state.location.search).toBe(query)
  expect(router.state.location.hash).toBe('#heatmap')
  expect(router.state.historyAction).toBe('REPLACE')
  expect(view.container.querySelector('.ant-menu-item-selected')).toHaveTextContent('工具')
})

test('narrow navigation opens by keyboard-accessible button and closes after page selection', async () => {
  vi.spyOn(window, 'matchMedia').mockImplementation(query => ({ matches: query.includes('max-width'), media: query,
    onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }))
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => page([]) })))
  const { router } = mount('/settings')
  const open = await screen.findByRole('button', { name: '展开导航' })
  expect(open).toHaveAttribute('aria-expanded', 'false')
  fireEvent.click(open)
  expect(screen.getByRole('button', { name: '收起导航' })).toHaveAttribute('aria-expanded', 'true')
  fireEvent.click(screen.getByText('项目', { selector: '.ant-menu-title-content' }))
  expect(await screen.findByRole('heading', { name: '项目' })).toBeInTheDocument()
  expect(router.state.location.pathname).toBe('/projects')
  expect(screen.getByRole('button', { name: '展开导航' })).toHaveAttribute('aria-expanded', 'false')
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
  expect(await screen.findByText('ClearClassify', { selector: 'h1' })).toBeInTheDocument()
  expect(oldSignal?.aborted).toBe(true)
  finishOld!({ ok: true, json: async () => app('ocr') })
  await waitFor(() => expect(screen.queryByText('ClearOCR', { selector: 'h1' })).not.toBeInTheDocument())
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
  expect(await screen.findByText('ClearOCR')).toBeInTheDocument()
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
  expect(await screen.findByText('项目不存在或已失效')).toBeInTheDocument()
  fireEvent.click(screen.getByText('清除项目选择'))
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
  fireEvent.click(screen.getByText('新建项目'))
  fireEvent.click(screen.getByRole('button', { name: /创\s*建/ }))
  await screen.findByText('请输入名称')
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
  fireEvent.change(screen.getByPlaceholderText('例如：季度资料整理'), { target: { value: '新项目' } })
  fireEvent.click(screen.getByRole('button', { name: /创\s*建/ }))
  await waitFor(() => expect(router.state.location.search).toBe('?workspace=newA'))
  expect(screen.queryByText('项目不存在或已失效')).not.toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  expect(JSON.parse(fetcher.mock.calls.find(([, options]) => options?.method === 'POST')![1]!.body as string)).toEqual({ schema_version: '1.0', name: '新项目', request_id: expect.stringMatching(/^w/) })
})
