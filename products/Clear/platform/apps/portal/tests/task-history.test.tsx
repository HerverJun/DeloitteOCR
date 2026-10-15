import '@testing-library/jest-dom/vitest'
import { beforeAll, afterEach, test, expect, vi } from 'vitest'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import { TaskHistory, formatTaskDuration, formatTaskSize } from '../src/features/tasks/TaskHistory'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const task = { id: 'task-a', task_code: 'TK-20261015-00000001', name: '票据 CSV 导入', description: '按项目保留执行历史', status: 'succeeded', tool_id: 'data', action: 'import',
  project_name_snapshot: '年度审计项目', creator_name_snapshot: '编制人员', input_total_bytes: 1024, created_at: '2026-10-15T00:00:00Z', elapsed_ms: 2500, run_count: 2, revision: 4, summary: {}, capabilities: ['view'] }
function mount() {
  const router = createMemoryRouter([{ path: '/jobs', element: <TaskHistory /> }], { initialEntries: ['/jobs'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<ConfigProvider><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
}
test('shows persisted task fields and sends a keyword filter to the paged API', async () => {
  const fetcher = vi.fn(async (url: string) => ({ ok: true, json: async () => url.includes('/ai-services') ? { items: [] } : { items: [task], next_cursor: null } }))
  vi.stubGlobal('fetch', fetcher)
  mount()
  expect(await screen.findByText(task.task_code)).toBeInTheDocument()
  expect(screen.getByText(task.project_name_snapshot)).toBeInTheDocument()
  expect(screen.getByText('1.0 KB')).toBeInTheDocument()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索任务' }), { target: { value: '票据' } })
  fireEvent.keyDown(screen.getByRole('searchbox', { name: '搜索任务' }), { key: 'Enter', code: 'Enter' })
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes('keyword='))).toBe(true))
})
test('represents missing historical sizes and durations honestly', () => {
  expect(formatTaskSize(null)).toBe('未记录')
  expect(formatTaskDuration(null)).toBe('未记录')
  expect(formatTaskDuration(125000)).toBe('2 分 5 秒')
})
