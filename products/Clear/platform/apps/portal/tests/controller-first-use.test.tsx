import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { ControllerSettings } from '../src/features/agent/ControllerSettings'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const current = { configured: false, protocol: 'openai_chat_completions', available: false }
const response = (body: unknown) => ({ ok: true, json: async () => body })
function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <ConfigProvider theme={{ token: { motion: false } }}><ControllerSettings open close={() => {}} /></ConfigProvider>
  </QueryClientProvider>)
}
test('a first-time Anthropic connection can choose protocol, authenticate, select a model and save with advanced settings collapsed', async () => {
  const listed: Record<string, unknown>[] = [], saved: Record<string, unknown>[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/models')) { listed.push(JSON.parse(String(init?.body))); return response({ models: [{ id: 'claude-example' }] }) }
    if (init?.method === 'PUT') saved.push(JSON.parse(String(init.body)))
    return response(current)
  }))
  mount()
  await screen.findByText('OpenAI 兼容工具调用')
  expect(screen.getByRole('combobox', { name: '接口协议' }).closest('.ant-select')).toBeVisible()
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '接口协议' }))
  fireEvent.click(await screen.findByText('Anthropic Messages'))
  fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://example.test/v1' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'test-secret' } })
  fireEvent.click(screen.getByRole('button', { name: '读取服务模型列表' }))
  await screen.findByRole('combobox', { name: '服务模型列表' })
  expect(listed[0]).toMatchObject({ protocol: 'anthropic_messages', base_url: 'https://example.test/v1', api_key: 'test-secret' })
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '服务模型列表' }))
  fireEvent.click(await screen.findByText('claude-example · 容量未知'))
  expect(screen.getByLabelText('模型')).toHaveValue('claude-example')
  expect(screen.getByLabelText('模型能力来源')).not.toBeVisible()
  expect(screen.getByText('高级连接参数').parentElement).not.toHaveAttribute('open')
  fireEvent.click(screen.getByRole('button', { name: '测试并保存' }))
  await waitFor(() => expect(saved[0]).toMatchObject({ protocol: 'anthropic_messages', api_key: 'test-secret', model: 'claude-example' }))
}, 15000)
test('an unavailable model list still allows a manually supplied model ID to be tested and saved', async () => {
  let saved: Record<string, unknown> | undefined
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/models')) return { ok: false, status: 502, json: async () => ({ detail: '服务未提供模型列表，可手填模型ID' }) }
    if (init?.method === 'PUT') saved = JSON.parse(String(init.body))
    return response(current)
  }))
  mount()
  await screen.findByText('OpenAI 兼容工具调用')
  fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'http://127.0.0.1:9000/v1' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'local' } })
  fireEvent.click(screen.getByRole('button', { name: '读取服务模型列表' }))
  await screen.findByText('服务未提供模型列表，可手填模型ID')
  fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'local-model' } })
  fireEvent.click(screen.getByRole('button', { name: '测试并保存' }))
  await waitFor(() => expect(saved).toMatchObject({ model: 'local-model', api_key: 'local' }))
}, 15000)
