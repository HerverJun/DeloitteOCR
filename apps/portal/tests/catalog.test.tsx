import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
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
  sessionStorage.clear()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(cleanup)

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

function mount() {
  const router = createMemoryRouter(routes, { initialEntries: ['/apps'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } })
  render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, client }
}

function baseFetch(input: string) {
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

test('lost launch response is verified and retried with exactly the same ID', async () => {
  let posts = 0
  const fetcher = vi.fn(async (input: string, options?: RequestInit) => {
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
  fireEvent.click(await screen.findByRole('button', { name: '启动并打开' }))
  expect(await screen.findByText('network response lost')).toBeInTheDocument()
  const pending = sessionStorage.getItem('workbench:pending-launch:ocr')
  expect(pending).toMatch(/^l[A-Za-z0-9_-]+$/)
  expect(popup.close).toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '核实原请求' }))
  await waitFor(() => expect(posts).toBe(2))
  const postPaths = fetcher.mock.calls.filter(([, options]) => options?.method === 'POST').map(([input]) => input)
  expect(postPaths).toEqual([postPaths[0], postPaths[0]])
  expect(postPaths[0]).toContain(pending)
  await waitFor(() => expect(popup.location.replace).toHaveBeenCalledWith(receipt.base_url))
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
  fireEvent.click(screen.getByRole('button', { name: '再次核实并打开 ↗' }))
  expect(await screen.findByText('原实例已退出；请明确发起新启动。')).toBeInTheDocument()
  expect(fetcher.mock.calls.filter(([input]) => String(input).endsWith('/launches/current')).length).toBeGreaterThan(1)
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
  fireEvent.click(await screen.findByRole('button', { name: '启动并打开' }))
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
  const button = await screen.findByRole('button', { name: '打开工具' })
  fireEvent.click(button)
  fireEvent.click(button)
  await waitFor(() => expect(finish).toBeDefined())
  expect(opened).toHaveBeenCalledOnce()
  expect(checks).toBe(2)
  finish!({ ok: true, json: async () => receipt })
  await waitFor(() => expect(popup.location.replace).toHaveBeenCalledOnce())
})
