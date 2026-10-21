import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TaskCenter } from '../src/features/jobs/TaskCenter'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
let client: QueryClient
afterEach(() => { cleanup(); client?.clear(); vi.unstubAllGlobals() })

test('local task center includes CPU document source and result links while preserving native jobs', async () => {
  const native = { platform_job_id: 'native-job', app_id: 'ocr', module_project_id: 'project', link_id: 'link',
    native_job_ids: ['native-task'], status: 'succeeded', native_status: 'succeeded', stage: 'complete',
    counts: { total: 1, succeeded: 1, failed: 0, cancelled: 0, pending: 0 }, progress_done: 1, progress_total: 1,
    capabilities: [], availability: 'ready', native_errors: [], updated_at: '2026-10-22T00:00:00Z' }
  const fetcher = vi.fn(async (url: string) => ({ ok: true, json: async () => {
    if (url.includes('/documents?')) return { items: [
      { document_id: 'cpu-document', status: 'succeeded', source: { original_name: 'CPU合同.docx' } },
      { document_id: 'scanned-document', status: 'running', source: { original_name: '扫描页.png' } },
    ], total: 2, next_offset: null }
    if (url.includes('/forensics?')) return { items: [], total: 0 }
    if (url.includes('/jobs/links')) return { items: [] }
    if (url.includes('/jobs?')) return { items: [native], next_offset: null }
    throw new Error(`Unexpected local task request ${url}`)
  } }))
  vi.stubGlobal('fetch', fetcher)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/jobs?workspace=debug']}>
    <TaskCenter />
  </MemoryRouter></QueryClientProvider>)
  const original = await screen.findByText('CPU合同.docx')
  const cpuRow = within(original.closest('tr')!)
  expect(cpuRow.getByText('执行成功')).toBeInTheDocument()
  expect(cpuRow.getByText('cpu-document')).toBeInTheDocument()
  expect(cpuRow.getByRole('link', { name: '查看解析结果' })).toHaveAttribute('href', '/apps/ocr/documents?workspace=debug&document=cpu-document')
  const scanRow = within((await screen.findByText('扫描页.png')).closest('tr')!)
  expect(scanRow.getByRole('link', { name: '查看解析结果' })).toHaveAttribute('href', '/apps/ocr/documents?workspace=debug&document=scanned-document')
  expect(await screen.findByLabelText('ocr 任务 native-job')).toBeInTheDocument()
  expect(fetcher.mock.calls.every(([url]) => !url.includes('/api/platform/v2/'))).toBe(true)
})
