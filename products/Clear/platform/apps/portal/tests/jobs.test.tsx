import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

test('task center restores native counts and leaves cancellation unconfirmed until refresh', async () => {
  const job = {
    platform_job_id: 'jobA', app_id: 'ocr', module_project_id: 'projectA', link_id: 'linkA',
    native_job_ids: ['taskA'], status: 'running', native_status: 'running', stage: 'GPU queue',
    counts: { total: 1, succeeded: 0, failed: 0, cancelled: 0, pending: 1 },
    progress_done: 0, progress_total: 1, capabilities: ['cancel'], availability: 'ready',
    native_errors: [{ native_item_id: 'taskA', message: '原生错误' }], updated_at: '2026-09-26T00:00:00Z',
  }
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => ({ ok: true, json: async () => {
    if (input.includes('/jobs/jobA/actions/cancel') && init?.method === 'POST') return { request_state: 'sent', job }
    if (input.includes('/jobs/links')) return { items: [{ link_id: 'linkA', app_id: 'ocr', module_project_id: 'projectA', role: 'editor' }] }
    if (input.includes('/jobs?')) return { items: [job], next_offset: null }
    if (input.includes('/workspaces?')) return { items: [{ workspace_id: 'workspaceA', name: '工作空间' }], next_cursor: null, snapshot_revision: 1 }
    throw new Error(`Unexpected request ${input}`)
  } }))
  vi.stubGlobal('fetch', fetcher)
  const router = createMemoryRouter(routes, { initialEntries: ['/jobs?workspace=workspaceA'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)

  expect(await screen.findByText('共 1 · 完成 0 · 失败 0 · 取消 0 · 未完成 1')).toBeInTheDocument()
  expect(screen.getByText('taskA：原生错误')).toBeInTheDocument()
  expect(screen.getByText('GPU 等待信息：GPU queue')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }))
  expect(await screen.findByText('请求已送达原生工具；等待刷新确认实际状态。')).toBeInTheDocument()
  expect(screen.getByText('共 1 · 完成 0 · 失败 0 · 取消 0 · 未完成 1')).toBeInTheDocument()
  expect(fetcher.mock.calls.some(([url, init]) => url.includes('/actions/cancel') && init?.method === 'POST')).toBe(true)
})
