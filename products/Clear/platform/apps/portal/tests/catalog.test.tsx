// @vitest-environment-options {"url":"http://127.0.0.1:8765/"}
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'
import type { AppHealth } from '../../../packages/contracts/types.generated'
import { ApplicationDeclaration } from '../src/features/catalog/ApplicationDeclaration'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  vi.restoreAllMocks()
  sessionStorage.clear()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(cleanup)

test('GPU support stays distinct from runtime readiness and preserves probe results', () => {
  const declared = { capabilities: ['gpu' as const], resources: ['cpu' as const, 'gpu' as const],
    input_types: ['image/jpeg'], output_types: ['application/json'] }
  const probed = health('ready', 'Service identity verified') as AppHealth
  probed.gpu = { schema_version: '1.0', state: 'unknown', reason: 'GPU has not been probed' }
  const { rerender } = render(<ApplicationDeclaration declaration={declared} health={probed} />)
  expect(screen.getByText(/支持资源：CPU、GPU/)).toBeInTheDocument()
  expect(screen.getByText(/GPU 状态：未确认 · GPU 运行状态尚未检测/)).toBeInTheDocument()
  expect(screen.queryByText(/GPU 状态：已就绪/)).not.toBeInTheDocument()
  rerender(<ApplicationDeclaration declaration={declared} health={{ ...probed,
    gpu: { schema_version: '1.0', state: 'unavailable', reason: 'GPU 正被其他任务占用' } }} />)
  expect(screen.getByText(/GPU 状态：不可用 · GPU 正被其他任务占用/)).toBeInTheDocument()
})

const page = (items: unknown[]) => ({ schema_version: '1.0', items, next_cursor: null, snapshot_revision: 2 })
const app = { schema_version: '1.0', app_id: 'ocr', display_name: '文档识别', app_version: '1.0',
  adapter_version: '1.0', min_platform_version: '1.0', data_schema_version: '1', entry_mode: 'standalone_page',
  capabilities: ['import'], input_types: ['image/png'], output_types: [], resources: ['cpu'], license_refs: [] }
const health = (state: string, reason: string) => ({ schema_version: '1.0', service: { state, reason },
  ui: { state: 'unknown', reason: '' }, cpu: { state: 'unknown', reason: '' },
  gpu: { state: 'unknown', reason: '' }, models: { state: 'unknown', reason: '' } })
const receipt = { schema_version: '1.0', launch_id: 'launchA', app_id: 'ocr', instance_id: 'instanceA',
  pid: 123, creation_time: '2026-09-25T01:00:00Z', base_url: 'http://127.0.0.1:49123/',
  handshake_nonce: 'nonceA', ownership: 'owned', version: 'fixture-1' }
const authenticatedAddress = receipt.base_url + '#token=' + 's'.repeat(48)
const platformAddress = authenticatedAddress + '&platform_home=http%3A%2F%2F127.0.0.1%3A8765%2F&platform_instance=instanceA'

function mount() {
  const router = createMemoryRouter(routes, { initialEntries: ['/apps'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } })
  render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, client }
}

function baseFetch(input: string) {
  if (input.endsWith('/navigation')) return { ok: true, json: async () => ({ url: authenticatedAddress }) }
  if (input.startsWith('/api/platform/v1/workspaces')) return { ok: true, json: async () => page([]) }
  if (input.startsWith('/api/platform/v1/apps?')) return { ok: true, json: async () => page([app]) }
  if (input.endsWith('/health')) return { ok: true, json: async () => health('unknown', 'Not probed') }
  if (input.endsWith('/installation')) return { ok: true, json: async () => ({ schema_version: '1.0', app_id: 'ocr', installed: true, launch_ready: true, state: 'registered' }) }
  if (input.startsWith('/api/platform/v1/recent-work?')) return { ok: true, json: async () => page([]) }
  if (input.endsWith('/launches/current')) return { ok: false, status: 404 }
  throw new Error(`Unexpected request ${input}`)
}

test('unregistered bundle has a stable disabled state despite unfamiliar reason text', async () => {
  const fetcher = vi.fn(async (input: string) => {
    if (input.endsWith('/health')) return { ok: true, json: async () => health('unavailable', 'Application bundle is not registered') }
    if (input.endsWith('/installation')) return { ok: true, json: async () => ({ schema_version: '1.0', app_id: 'ocr', installed: false, launch_ready: false, state: 'not_registered' }) }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  mount()
  expect(await screen.findByText('未安装')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '启动并打开' })).toBeDisabled()
  expect(fetcher.mock.calls.every(([input]) => !String(input).includes('/launches/launch'))).toBe(true)
})

test('verification keeps launch disabled and refreshes to awaiting startup without reloading the page', async () => {
  let ready = false
  const fetcher = vi.fn(async (input: string, _options?: RequestInit) => {
    if (input.endsWith('/installation')) return { ok: true, json: async () => ({
      schema_version: '1.0', app_id: 'ocr', installed: ready, launch_ready: ready,
      state: ready ? 'registered' : 'verifying',
    }) }
    if (input.endsWith('/health')) return { ok: true, json: async () =>
      health('unknown', ready ? 'Service has not been probed' : '正在校验应用文件') }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  mount()
  expect(await screen.findByText('正在校验')).toBeInTheDocument()
  expect(screen.getByText('应用文件校验中，可先使用数据工作台')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '启动并打开' })).toBeDisabled()
  expect(screen.queryByText('未安装')).not.toBeInTheDocument()
  ready = true
  await waitFor(() => expect(screen.getByRole('button', { name: '启动并打开' })).toBeEnabled(), { timeout: 5000 })
  expect(await screen.findByText('待启动')).toBeInTheDocument()
  expect(screen.getByText('按需启动，打开时检查运行状态')).toBeInTheDocument()
  expect(fetcher.mock.calls.every(([, options]) => (options as RequestInit | undefined)?.method !== 'POST')).toBe(true)
})

test.each([
  { appId: 'ocr', brand: 'ClearOCR' },
  { appId: 'classification', brand: 'ClearClassify' },
])('$brand cold start is presented as awaiting startup without launching the service', async ({ appId, brand }) => {
  const appPath = `/api/platform/v1/apps/${appId}`
  const fetcher = vi.fn(async (input: string, _options?: RequestInit) => {
    if (input.startsWith('/api/platform/v1/apps?')) return { ok: true, json: async () => page([{ ...app, app_id: appId }]) }
    if (input === `${appPath}/installation`) return { ok: true, json: async () => ({
      schema_version: '1.0', app_id: appId, installed: true, launch_ready: true, state: 'registered',
    }) }
    if (input === `${appPath}/health`) return { ok: true, json: async () => health('unknown', 'Service has not been probed') }
    if (input === `${appPath}/launches/current`) return { ok: false, status: 404 }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  mount()
  const card = (await screen.findByRole('link', { name: brand })).closest('.application-card')
  expect(card).not.toBeNull()
  const tool = within(card as HTMLElement)
  expect(await tool.findByText('待启动')).toBeInTheDocument()
  expect(tool.getByText('按需启动，打开时检查运行状态')).toBeInTheDocument()
  expect(tool.getByRole('button', { name: '启动并打开' })).toBeEnabled()
  expect(tool.queryByText('已就绪')).not.toBeInTheDocument()
  expect(tool.queryByText('实例已核实')).not.toBeInTheDocument()
  for (const path of [`${appPath}/installation`, `${appPath}/health`, `${appPath}/launches/current`]) {
    expect(fetcher.mock.calls.some(([input, options]) => input === path && (options?.method || 'GET') === 'GET')).toBe(true)
  }
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})

test.each([
  { appId: 'ocr', brand: 'ClearOCR' },
  { appId: 'classification', brand: 'ClearClassify' },
])('$brand cached file verification does not hide an unavailable service or its failure reason', async ({ appId, brand }) => {
  const appPath = `/api/platform/v1/apps/${appId}`
  const failureReason = '本机服务身份核验失败：端口已被其他进程占用'
  const cachedMessage = '已复用首次完整校验记录'
  const fetcher = vi.fn(async (input: string, _options?: RequestInit) => {
    if (input.startsWith('/api/platform/v1/apps?')) return { ok: true, json: async () => page([{ ...app, app_id: appId }]) }
    if (input === `${appPath}/installation`) return { ok: true, json: async () => ({
      schema_version: '1.0', app_id: appId, installed: true, launch_ready: true, state: 'registered',
      verification: {
        schema_version: '1.0', phase: 'cached', checked_files: 0, total_files: 1000,
        elapsed_seconds: 65, current_file: null, message: cachedMessage,
      },
    }) }
    if (input === `${appPath}/health`) return { ok: true, json: async () => health('unavailable', failureReason) }
    if (input === `${appPath}/launches/current`) return { ok: false, status: 404 }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  mount()
  const card = (await screen.findByRole('link', { name: brand })).closest('.application-card')
  expect(card).not.toBeNull()
  const tool = within(card as HTMLElement)
  expect(await tool.findByText('暂不可用')).toBeInTheDocument()
  const state = within(card!.querySelector('.application-state') as HTMLElement)
  expect(state.getByText(failureReason)).toBeInTheDocument()
  expect(state.queryByText(cachedMessage)).not.toBeInTheDocument()
  expect(state.queryByText('已校验')).not.toBeInTheDocument()
  expect(state.queryByText('待启动')).not.toBeInTheDocument()
  expect(tool.queryByText('已就绪')).not.toBeInTheDocument()
  expect(tool.queryByText('实例已核实')).not.toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})

test('failed verification becomes visible and keeps launch disabled', async () => {
  let finished = false
  const fetcher = vi.fn(async (input: string, _options?: RequestInit) => {
    if (input.endsWith('/installation')) return { ok: true, json: async () => ({
      schema_version: '1.0', app_id: 'ocr', installed: false, launch_ready: false,
      state: finished ? 'not_registered' : 'verifying',
    }) }
    if (input.endsWith('/health')) return { ok: true, json: async () =>
      health(finished ? 'unavailable' : 'unknown', finished ? '可选应用包校验失败' : '正在校验应用文件') }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  const { client } = mount()
  expect(await screen.findByText('正在校验')).toBeInTheDocument()
  finished = true
  await client.invalidateQueries({ queryKey: ['app-installation', 'ocr'] })
  expect(await screen.findByText('可选应用包校验失败')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '启动并打开' })).toBeDisabled()
  expect(screen.queryByText('正在校验')).not.toBeInTheDocument()
  expect(fetcher.mock.calls.every(([, options]) => (options as RequestInit | undefined)?.method !== 'POST')).toBe(true)
})

test('file progress and completed waiting state remain visible until the launch gate opens', async () => {
  let stage = 0
  const fetcher = vi.fn(async (input: string) => {
    if (input.endsWith('/installation')) return { ok: true, json: async () => ({
      schema_version: '1.0', app_id: 'ocr', installed: stage === 2, launch_ready: stage === 2,
      state: stage === 2 ? 'registered' : 'verifying', verification: {
        schema_version: '1.0', phase: stage === 0 ? 'checking' : stage === 1 ? 'passed' : 'cached',
        checked_files: stage === 0 ? 250 : stage === 1 ? 1000 : 0,
        total_files: 1000, elapsed_seconds: 65, current_file: stage === 0 ? 'models/weights.bin' : null,
        message: stage === 0 ? '正在完整校验应用文件' : stage === 1 ? '校验完成，等待其他应用' : '已复用首次完整校验记录',
      },
    }) }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  const { client } = mount()
  expect(await screen.findByText('25%')).toBeInTheDocument()
  expect(screen.getByText(/已校验 250 \/ 1,000 个文件/)).toBeInTheDocument()
  expect(screen.getByText(/已用时 1 分 5 秒/)).toBeInTheDocument()
  expect(screen.getByText('当前文件：models/weights.bin')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '启动并打开' })).toBeDisabled()
  stage = 1
  await client.invalidateQueries({ queryKey: ['app-installation', 'ocr'] })
  expect(await screen.findByText('校验完成')).toBeInTheDocument()
  expect(screen.getByText('校验完成，等待其他应用')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '启动并打开' })).toBeDisabled()
  stage = 2
  await client.invalidateQueries({ queryKey: ['app-installation', 'ocr'] })
  expect(await screen.findByText('已复用首次完整校验记录')).toBeInTheDocument()
  expect(screen.queryByText('25%')).not.toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: '启动并打开' })).toBeEnabled())
})

test('waiting and failed file verification show feedback rather than an uninstalled label', async () => {
  let failed = false
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/installation')) return { ok: true, json: async () => ({
      schema_version: '1.0', app_id: 'ocr', installed: false, launch_ready: false,
      state: failed ? 'not_registered' : 'verifying', verification: {
        schema_version: '1.0', phase: failed ? 'failed' : 'waiting', checked_files: 0, total_files: 1000,
        elapsed_seconds: 0, current_file: null, message: failed ? '文件校验失败，请检查程序包' : '等待其他应用的完整校验',
      },
    }) }
    return baseFetch(input)
  }))
  const { client } = mount()
  expect(await screen.findByText('等待校验')).toBeInTheDocument()
  expect(screen.getByText('等待其他应用的完整校验')).toBeInTheDocument()
  failed = true
  await client.invalidateQueries({ queryKey: ['app-installation', 'ocr'] })
  expect(await screen.findByText('校验失败')).toBeInTheDocument()
  expect(screen.getByText('文件校验失败，请检查程序包')).toBeInTheDocument()
  expect(screen.queryByText('未安装')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '启动并打开' })).toBeDisabled()
})

test('lost launch response is verified and retried with exactly the same ID', async () => {
  let posts = 0
  const fetcher = vi.fn(async (input: string, options?: RequestInit) => {
    if (input.endsWith('/navigation')) return baseFetch(input)
    if (input.includes('/launches/l') && options?.method === 'POST') {
      posts++
      if (posts === 1) throw new Error('network response lost')
      return { ok: true, json: async () => receipt }
    }
    if (input.includes('/launches/l')) return { ok: false, status: 404 }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  const popup = { opener: {} as unknown, location: { replace: vi.fn() }, close: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '启动并打开' }, { timeout: 5000 }))
  expect(await screen.findByText('network response lost')).toBeInTheDocument()
  const pending = sessionStorage.getItem('workbench:pending-launch:ocr')
  expect(pending).toMatch(/^l[A-Za-z0-9_-]+$/)
  expect(popup.close).toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '核实原请求' }))
  await waitFor(() => expect(posts).toBe(2))
  const postPaths = fetcher.mock.calls.filter(([input, options]) => options?.method === 'POST' && !input.endsWith('/navigation')).map(([input]) => input)
  expect(postPaths).toEqual([postPaths[0], postPaths[0]])
  expect(postPaths[0]).toContain(pending)
  await waitFor(() => expect(popup.location.replace).toHaveBeenCalledWith(platformAddress))
  expect(sessionStorage.getItem('workbench:pending-launch:ocr')).toBeNull()
  expect(screen.queryByRole('link', { name: /已核实的本机实例/ })).not.toBeInTheDocument()
})

test('missing window handle offers retry; continuing a recent record requires a live check', async () => {
  const event = { schema_version: '1.0', recent_id: 'recentA', source: 'platform_launch', app_id: 'ocr',
    launch_id: 'launchA', instance_id: 'instanceA', opened_at: '2026-09-25T01:00:00Z' }
  let current = true
  const fetcher = vi.fn(async (input: string) => {
    if (input.includes('/recent-work?')) return { ok: true, json: async () => page([event]) }
    if (input.endsWith('/apps/ocr')) return { ok: true, json: async () => app }
    if (input.endsWith('/launches/current')) return current ? { ok: true, json: async () => receipt } : { ok: false, status: 404 }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  vi.spyOn(window, 'open').mockReturnValue(null)
  const { router } = mount()
  expect(await screen.findByText('平台已核验启动')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('link', { name: '继续（先核实实例）→' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/apps/ocr'))
  fireEvent.click(await screen.findByRole('button', { name: '打开工具' }))
  expect(await screen.findByRole('button', { name: '再次核实并打开 ↗' })).toBeInTheDocument()
  expect(screen.getByText(/未能创建新窗口/)).toBeInTheDocument()
  current = false
  vi.spyOn(window, 'open').mockReturnValue({ opener: null, location: { replace: vi.fn() }, close: vi.fn() } as unknown as Window)
  fireEvent.click(screen.getByRole('button', { name: '再次核实并打开 ↗' }))
  expect(await screen.findByText('原实例已退出；请明确发起新启动。')).toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([input]) => String(input).endsWith('/launches/current')).length).toBeGreaterThan(1)
})

test('opening a running OCR service establishes a session before navigating and does not relaunch it', async () => {
  const fetcher = vi.fn(async (input: string, _options?: RequestInit) => {
    if (input.endsWith('/launches/current')) return { ok: true, json: async () => receipt }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  const popup = { opener: {} as unknown, location: { replace: vi.fn() }, close: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '打开工具' }))
  await waitFor(() => expect(popup.location.replace).toHaveBeenCalledWith(platformAddress))
  const posts = fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')
  expect(posts.map(([path]) => path)).toEqual(['/api/platform/v1/apps/ocr/launches/launchA/navigation'])
  expect(sessionStorage.getItem('ocr-token')).toBeNull()
})

test('failed OCR session handoff closes the reserved window instead of opening an unauthenticated page', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    if (input.endsWith('/launches/current')) return { ok: true, json: async () => receipt }
    if (input.endsWith('/navigation')) return { ok: false, status: 503 }
    return baseFetch(input)
  }))
  const popup = { opener: {} as unknown, location: { replace: vi.fn() }, close: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '打开工具' }))
  expect(await screen.findByText('ClearOCR 工作台连接暂不可用，请重新核实并打开。')).toBeInTheDocument()
  expect(popup.location.replace).not.toHaveBeenCalled()
  expect(popup.close).toHaveBeenCalled()
})

test('an exited cached instance is not silently replaced when the user clicks open', async () => {
  let alive = true
  const fetcher = vi.fn(async (input: string, _options?: RequestInit) => {
    if (input.endsWith('/launches/current')) return alive ? { ok: true, json: async () => receipt } : { ok: false, status:404 }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch',fetcher)
  vi.spyOn(window,'open').mockReturnValue({ opener:null, close:vi.fn(), location:{ replace:vi.fn() } } as unknown as Window)
  mount()
  const button = await screen.findByRole('button',{name:'打开工具'})
  alive = false
  fireEvent.click(button)
  expect(await screen.findByText('原实例已退出；请明确发起新启动。')).toBeInTheDocument()
  expect(fetcher.mock.calls.some(([,options]) => options?.method === 'POST')).toBe(false)
  expect(await screen.findByRole('button',{name:'启动并打开'})).toBeEnabled()
})

test('configured bundle without durable storage cannot launch', async () => {
  const fetcher = vi.fn(async (input: string) => {
    if (input.endsWith('/installation')) return { ok: true, json: async () => ({
      schema_version: '1.0', app_id: 'ocr', installed: true, launch_ready: false, state: 'storage_unavailable',
    }) }
    return baseFetch(input)
  })
  vi.stubGlobal('fetch', fetcher)
  mount()
  expect(await screen.findByText('存储未配置')).toBeInTheDocument()
  expect(screen.getByText('请配置本机数据目录后启动')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '启动并打开' })).toBeDisabled()
  expect(fetcher.mock.calls.every(([input, options]) => options?.method !== 'POST')).toBe(true)
})

test('leaving a page closes its blank window and never navigates it after a late response', async () => {
  let resolveCurrent: ((value: unknown) => void) | undefined
  let checks = 0
  const fetcher = vi.fn((input: string) => {
    if (input.endsWith('/launches/current')) {
      if (++checks === 1) return Promise.resolve({ ok: false, status: 404 })
      return new Promise(resolve => { resolveCurrent = resolve })
    }
    return Promise.resolve(baseFetch(input))
  })
  vi.stubGlobal('fetch', fetcher)
  const popup = { opener: {} as unknown, location: { replace: vi.fn() }, close: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  const { router } = mount()
  fireEvent.click(await screen.findByRole('button', { name: '启动并打开' }, { timeout: 5000 }))
  await waitFor(() => expect(resolveCurrent).toBeDefined())
  await router.navigate('/')
  await waitFor(() => expect(popup.close).toHaveBeenCalledOnce())
  resolveCurrent!({ ok: true, json: async () => receipt })
  await waitFor(() => expect(fetcher.mock.calls.filter(([input]) => input.endsWith('/launches/current'))).toHaveLength(2))
  expect(popup.location.replace).not.toHaveBeenCalled()
})

test('rapid repeat click reserves only one blank window and one verification', async () => {
  let finish: ((value: unknown) => void) | undefined
  let checks = 0
  const fetcher = vi.fn((input: string) => {
    if (input.endsWith('/launches/current')) {
      if (++checks === 1) return Promise.resolve({ ok: true, json: async () => receipt })
      return new Promise(resolve => { finish = resolve })
    }
    return Promise.resolve(baseFetch(input))
  })
  vi.stubGlobal('fetch', fetcher)
  const popup = { opener: {} as unknown, location: { replace: vi.fn() }, close: vi.fn() }
  const opened = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  mount()
  const button = await screen.findByRole('button', { name: '打开工具' }, { timeout: 5000 })
  fireEvent.click(button)
  fireEvent.click(button)
  await waitFor(() => expect(finish).toBeDefined())
  expect(opened).toHaveBeenCalledOnce()
  expect(checks).toBe(2)
  finish!({ ok: true, json: async () => receipt })
  await waitFor(() => expect(popup.location.replace).toHaveBeenCalledOnce())
})
