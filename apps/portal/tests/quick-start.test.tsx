import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(cleanup)
const ok = (value: unknown) => ({ ok: true, json: async () => value })
function base(input: string) {
  if (input.includes('/workspaces?')) return ok({ items: [{ workspace_id: 'a', name: '甲项目' }], next_cursor: null, snapshot_revision: 1 })
  if (input.includes('/apps?')) return ok({ items: [], next_cursor: null, snapshot_revision: 1 })
  if (input.includes('/data/health')) return ok({ ready: true })
  if (input.includes('/data/sessions')) return ok({ items: [] })
  throw new Error(`Unexpected URL ${input}`)
}
function mount(url: string) {
  const router = createMemoryRouter(routes, { initialEntries: [url] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const view = render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, view }
}

test('project and tool selection enter processing without audit setup', async () => {
  const fetcher = vi.fn(async (input: string) => base(input))
  vi.stubGlobal('fetch', fetcher)
  const { router, view } = mount('/manual?workspace=a')
  expect(await screen.findByRole('heading', { name: '手动工具' })).toBeInTheDocument()
  expect(view.container.querySelector('.ant-menu-item-selected')).toHaveTextContent('手动工具')
  fireEvent.click(await screen.findByRole('button', { name: '打开数据工作台' }))
  expect(await screen.findByRole('button', { name: '开始新的比对' })).toBeInTheDocument()
  expect(router.state.location.pathname).toBe('/apps/data')
  expect(router.state.location.search).toBe('?workspace=a')
  expect(fetcher.mock.calls.some(([url]) => url.includes('/sessions?workspace_id=a'))).toBe(true)
  expect(fetcher.mock.calls.some(([url]) => url.includes('/audit/'))).toBe(false)
})

test('first OCR use creates a linked job once and keeps its reserved window through cache updates', async () => {
  const projects: unknown[] = []
  const popup = { opener: {}, close: vi.fn(), location: { replace: vi.fn() } }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (input.endsWith('/ocr/projects')) {
      if (init?.method === 'POST') { const value = { project_id: 'nativeA', name: '甲项目', link_id: 'linkA' }; projects.push(value); return ok(value) }
      return ok({ items: [...projects] })
    }
    if (input.endsWith('/quick-navigation')) return ok({ url: `http://127.0.0.1:49000/#platform_ticket=${'x'.repeat(48)}` })
    return base(input)
  })
  vi.stubGlobal('fetch', fetcher)
  mount('/apps/ocr/start?workspace=a')
  const start = await screen.findByRole('button', { name: '开始处理' })
  await waitFor(() => expect(start).toBeEnabled())
  fireEvent.click(start)
  await waitFor(() => expect(popup.location.replace).toHaveBeenCalledOnce())
  expect(popup.close).not.toHaveBeenCalled()
  const creates = fetcher.mock.calls.filter(([url, init]) => url.endsWith('/ocr/projects') && init?.method === 'POST')
  expect(creates).toHaveLength(1)
  expect(JSON.parse(creates[0][1]!.body as string)).toEqual({ name: '甲项目', request_id: expect.stringMatching(/^n/) })
})

test('lost native creation reply refreshes the linked job instead of creating another', async () => {
  const projects: unknown[] = []
  const popup = { opener: {}, close: vi.fn(), location: { replace: vi.fn() } }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (input.endsWith('/ocr/projects')) {
      if (init?.method === 'POST') {
        const value = { project_id: 'nativeA', name: '甲项目', link_id: 'linkA' }
        if (!projects.length) { projects.push(value); throw new Error('response lost') }
        return ok(value)
      }
      return ok({ items: [...projects] })
    }
    if (input.endsWith('/quick-navigation')) return ok({ url: `http://127.0.0.1:49000/#platform_ticket=${'x'.repeat(48)}` })
    return base(input)
  })
  vi.stubGlobal('fetch', fetcher)
  mount('/apps/ocr/start?workspace=a')
  const start = await screen.findByRole('button', { name: '开始处理' })
  await waitFor(() => expect(start).toBeEnabled())
  fireEvent.click(start)
  await screen.findByText('response lost')
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  const resume = await screen.findByRole('button', { name: '继续确认并打开' })
  await waitFor(() => expect(resume).toBeEnabled())
  fireEvent.click(resume)
  await waitFor(() => expect(popup.location.replace).toHaveBeenCalledOnce())
  const creates = fetcher.mock.calls.filter(([url, init]) => url.endsWith('/ocr/projects') && init?.method === 'POST')
  expect(creates).toHaveLength(2)
  expect(creates[0][1]?.body).toBe(creates[1][1]?.body)
})

test('legacy selected native tool reaches the same project page and preserves its context without relaunch', async () => {
  const open = vi.spyOn(window, 'open')
  const manifest = { app_id: 'ocr', display_name: 'OCR', input_types: [], output_types: [], capabilities: [], resources: [] }
  const receipt = { launch_id: 'launchA', instance_id: 'instanceA', base_url: 'http://127.0.0.1:49000/' }
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (input.includes('/apps?')) return ok({ items: [manifest], next_cursor: null, snapshot_revision: 1 })
    if (input.endsWith('/launches/current')) return ok(receipt)
    if (input.endsWith('/installation')) return ok({ installed: true, launch_ready: true, state: 'registered' })
    if (input.endsWith('/apps/ocr/health')) return ok({ service: { state: 'ready' } })
    if (input.endsWith('/ocr/projects')) return ok({ items: [{ project_id: 'nativeA', name: '甲项目', link_id: 'linkA' }] })
    return base(input)
  })
  vi.stubGlobal('fetch', fetcher)
  const { router } = mount('/manual?workspace=a&tool=ocr')
  expect(await screen.findByRole('heading', { name: 'ClearOCR', level: 1 })).toBeInTheDocument()
  expect(router.state.location.pathname).toBe('/apps/ocr/start')
  expect(router.state.location.search).toBe('?workspace=a&tool=ocr')
  expect(open).not.toHaveBeenCalled()
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
})

test.each(['ocr', 'classification'] as const)('failed %s creation remains retryable after reload with its original intent', async appId => {
  const popup = { opener: {}, close: vi.fn(), location: { replace: vi.fn() } }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  const attempts: string[] = []
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (input.endsWith(`/${appId}/projects`)) {
      if (init?.method === 'POST') {
        attempts.push(String(init.body))
        if (attempts.length === 1) return { ok: false, status: 503 }
        return ok({ project_id: 'nativeRecovered', name: '甲项目', link_id: 'linkRecovered' })
      }
      return ok({ items: [] })
    }
    if (input.endsWith('/quick-navigation')) return ok({ url: `http://127.0.0.1:49000/#platform_ticket=${'x'.repeat(48)}` })
    return base(input)
  })
  vi.stubGlobal('fetch', fetcher)
  const first = mount(`/apps/${appId}/start?workspace=a`)
  if (appId === 'classification') fireEvent.change(await screen.findByRole('textbox', { name: '分类类别' }), { target: { value: '甲\n乙' } })
  const start = await screen.findByRole('button', { name: '开始处理' })
  await waitFor(() => expect(start).toBeEnabled())
  fireEvent.click(start)
  await screen.findByText('上次创建尚未确认')
  first.view.unmount()
  mount(`/apps/${appId}/start?workspace=a`)
  const resume = await screen.findByRole('button', { name: '继续确认并打开' })
  await waitFor(() => expect(resume).toBeEnabled())
  fireEvent.click(resume)
  await waitFor(() => expect(popup.location.replace).toHaveBeenCalledOnce())
  expect(attempts).toHaveLength(2)
  expect(attempts[0]).toBe(attempts[1])
  expect(localStorage.getItem(`clear-quick-native-create:${appId}:a`)).toBeNull()
})

test('switching project from the second output page starts at the first page', async () => {
  const requests: string[] = []
  const fetcher = vi.fn(async (input: string) => {
    requests.push(input)
    if (input.includes('/workspaces?')) return ok({ items: [{ workspace_id: 'a', name: '甲' }, { workspace_id: 'b', name: '乙' }], next_cursor: null })
    if (input.includes('/data/outputs') || input.endsWith('/projects')) return ok({ items: [], total: 0 })
    if (input.includes('/artifacts?')) return ok({ total: input.includes('/a/') ? 21 : 1, items: [{
      bundle_id: input.includes('/a/') ? 'bundleA' : 'bundleB', kind: 'data_result', title: input.includes('/a/') ? '甲输出' : '乙输出',
      created_at: '2026-10-03T00:00:00Z', files: [], inputs: [],
    }] })
    return base(input)
  })
  vi.stubGlobal('fetch', fetcher)
  const { router, view } = mount('/artifacts?workspace=a')
  await screen.findByRole('heading', { name: '甲输出' })
  fireEvent.click(view.container.querySelector('.ant-pagination-item-2')!)
  await waitFor(() => expect(requests.some(url => url.includes('/a/artifacts?offset=20'))).toBe(true))
  await router.navigate('/artifacts?workspace=b')
  await screen.findByRole('heading', { name: '乙输出' })
  expect(requests.some(url => url.includes('/b/artifacts?offset=20'))).toBe(false)
  expect(requests.some(url => url.includes('/b/artifacts?offset=0'))).toBe(true)
})
