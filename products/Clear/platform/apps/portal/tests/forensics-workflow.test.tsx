import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { ForensicWorkbench } from '../src/features/forensics/ForensicWorkbench'
import type { Inspection, InspectionCreate, InspectionReview } from '../../../packages/contracts/types.generated'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear() })
const response = (value: unknown) => ({ ok: true, json: async () => value })
function mount() {
  const router = createMemoryRouter([{ path: '/forensics', element: <ForensicWorkbench /> }], { initialEntries: ['/forensics?workspace=wImage'] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<ConfigProvider><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
}

test('lost inspection response survives reload with its original request, readable skipped tools and conditional human review', async () => {
  localStorage.clear()
  vi.stubGlobal('crypto', { randomUUID })
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
  const asset = { asset_id: 'aImage', revision: 3, blob_hash: 'a'.repeat(64), original_name: '业务截图.png', media_type: 'image/png' }
  const saved: InspectionCreate = { schema_version: '1.0', request_id: 'rOriginalInspection', asset_id: asset.asset_id,
    asset_revision: asset.revision, blob_hash: asset.blob_hash, tools: ['metadata', 'jpeg'] }
  localStorage.setItem('clear-forensic-start:wImage', JSON.stringify(saved))
  const time = '2026-10-03T05:00:00Z'
  let item: Inspection = { ...saved, workspace_id: 'wImage', inspection_id: 'iImage', revision: 1, original_name: asset.original_name,
    status: 'succeeded', error: null, report_hash: 'b'.repeat(64), files: [], review: null, review_history: [], created_at: time, updated_at: time,
    report: { input: { width: 256, height: 160, sha256: asset.blob_hash }, verdict: null, evidence: [{ tool: 'jpeg', status: 'skipped', version: '0.1.0', elapsed_ms: 0,
      map_path: null, reason: 'source file is not JPEG with quantization tables', limitations: ['不适用工具不能构成未篡改证据'], metrics: {} }] } }
  const submissions: InspectionCreate[] = [], reviews: InspectionReview[] = []
  let loseResponse = true
  vi.stubGlobal('fetch', vi.fn(async (raw: string, init?: RequestInit) => {
    const url = new URL(raw, 'http://localhost')
    if (url.pathname.endsWith('/assets')) return response({ items: [asset], total: 1 })
    if (url.pathname.endsWith('/forensics') && init?.method === 'POST') {
      submissions.push(JSON.parse(init.body as string))
      if (loseResponse) { loseResponse = false; throw new TypeError('响应丢失') }
      return response(item)
    }
    if (url.pathname.endsWith('/forensics')) return response({ items: submissions.length ? [item] : [], total: submissions.length ? 1 : 0, next_offset: null })
    if (url.pathname.endsWith('/iImage/review')) { const payload = JSON.parse(init!.body as string) as InspectionReview; reviews.push(payload)
      item = { ...item, revision: 2, review: { ...payload, revision: 2 }, review_history: [payload] }; return response(item) }
    if (url.pathname.endsWith('/iImage')) return response(item)
    throw new Error(raw)
  }))
  const first = mount()
  fireEvent.click(await screen.findByRole('button', { name: '查询并续接原提交' }))
  await screen.findByText('响应丢失')
  first.unmount()
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '查询并续接原提交' }))
  await screen.findByText('人工审阅与调查判断')
  expect(screen.getByRole('link', { name: '下载完整报告（含图片）' })).toHaveAttribute('href', '/api/platform/v1/workspaces/wImage/forensics/iImage/download')
  expect(submissions).toEqual([saved, saved])
  expect(screen.getByText('不适用 · 已跳过')).toBeVisible()
  expect(screen.getByText('不适用工具不能构成未篡改证据')).toBeVisible()
  fireEvent.click(screen.getByText('可选：添加人工审阅说明'))
  fireEvent.change(screen.getByLabelText('本机审阅人'), { target: { value: '业务审阅人' } })
  fireEvent.change(screen.getByLabelText('审阅说明（可疑位置、依据与限制）'), { target: { value: '缺少原图，当前无法判断。' } })
  fireEvent.click(screen.getByRole('button', { name: '保存人工审阅' }))
  await waitFor(() => expect(reviews).toHaveLength(1))
  expect(reviews[0].expected_revision).toBe(1)
  expect(reviews[0].assessment).toBe('inconclusive')
  expect(reviews[0].actor.display_name).toBe('业务审阅人')
  await waitFor(() => expect(localStorage.getItem('clear-forensic-review:wImage:iImage')).toBeNull())
  expect(screen.getByRole('link', { name: '进入审计项目，将此证据关联调查事项' })).toHaveAttribute('href', '/audit?workspace=wImage')
}, 30000)
