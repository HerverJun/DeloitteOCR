import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { MultimodalWorkbench, ResultView } from '../src/features/multimodal/MultimodalWorkbench'
import { ForensicWorkbench } from '../src/features/forensics/ForensicWorkbench'
import type { UploadBinding, UploadStatus } from '../../../packages/contracts/types.generated'

vi.mock('../src/App', () => ({ useWorkspaceId: () => 'wStart', Workspaces: () => null }))
vi.mock('../src/features/batches/fileHash', () => ({ AUDIO_CHUNK_BYTES: 8 * 1024 * 1024,
  fileSha256: async (file: File) => (file.name.startsWith('甲') ? 'a' : 'b').repeat(64), chunkSha256: async () => 'b'.repeat(64) }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
beforeEach(() => { vi.stubGlobal('crypto', { randomUUID }) })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear() })
const response = (value: unknown) => ({ ok: true, json: async () => value })
function mount(element: React.ReactNode, query = '', pathname = '/apps/tools') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element }], { initialEntries: [pathname + '?workspace=wStart' + query] })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  return router
}
function binding(path: string): UploadBinding {
  const url = new URL(path, 'http://local')
  return { schema_version: '1.0', workspace_id: 'wStart', request_id: url.pathname.split('/').at(-1)!,
    name: url.searchParams.get('name')!, sha256: url.searchParams.get('sha256')!, size_bytes: Number(url.searchParams.get('size_bytes')) }
}
function committed(value: UploadBinding, id: string): UploadStatus {
  const time = '2026-10-27T00:00:00Z'
  return { schema_version: '1.0', binding: value, payload_hash: 'c'.repeat(64), status: 'committed', created_at: time, updated_at: time,
    receipt: { schema_version: '1.0', binding: value, payload_hash: 'c'.repeat(64), created_at: time,
      asset: { schema_version: '1.0', asset_id: id, workspace_id: 'wStart', source_id: 'source', original_name: value.name, media_type: 'image/png',
        storage_mode: 'managed', blob_hash: value.sha256, size_bytes: value.size_bytes, revision: 2, created_at: time } } }
}

test('two-photo comparison retains the first upload while adding the second and starts only on explicit click', async () => {
  const uploaded: NonNullable<UploadStatus['receipt']>['asset'][] = [], submitted: { inputs: unknown[]; options: unknown }[] = []
  const finished = { job_id: 'comparison', kind: 'face', status: 'succeeded', stage: 'completed', revision: 1, inputs: [], result: null }
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/multimodal/capabilities')) return response({ items: [{ kind: 'face', cpu_ready: true, gpu_ready: false, missing: [] }] })
    if (path.includes('/assets/uploads/') && init?.method === 'POST') {
      const status = committed(binding(path), 'photo' + (uploaded.length + 1)); uploaded.push(status.receipt!.asset); return response(status)
    }
    if (path.includes('/assets?')) return response({ items: [...uploaded], next_offset: null })
    if (path.endsWith('/multimodal/jobs') && init?.method === 'POST') { submitted.push(JSON.parse(String(init.body))); return response(finished) }
    if (path.endsWith('/multimodal/jobs/comparison')) return response(finished)
    return response({ items: [], total: 0 })
  }))
  mount(<MultimodalWorkbench kind="face" />)
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '人脸分析方式' }))
  fireEvent.click(await screen.findByText('两张照片比对'))
  const start = screen.getByRole('button', { name: '开始两张照片比对' })
  const input = screen.getByLabelText('添加审计资料')
  expect(input).toHaveAttribute('multiple')
  fireEvent.change(input, { target: { files: [new File(['first'], '甲照片.png', { type: 'image/png' })] } })
  await screen.findByText('再选择另一张照片，即可开始比对。')
  expect(start).toBeDisabled()
  fireEvent.change(input, { target: { files: [new File(['second'], '乙照片.png', { type: 'image/png' })] } })
  await waitFor(() => expect(start).toBeEnabled())
  expect(uploaded).toHaveLength(2)
  expect(submitted).toHaveLength(0)
  expect(screen.queryByRole('button', { name: '上传未提交资料' })).not.toBeInTheDocument()
  fireEvent.click(start)
  await waitFor(() => expect(submitted).toHaveLength(1))
  expect(submitted[0]).toMatchObject({ options: { device: 'cpu', action: 'compare' }, inputs: [
    { asset_id: 'photo1', asset_revision: 2, blob_hash: 'a'.repeat(64) }, { asset_id: 'photo2', asset_revision: 2, blob_hash: 'b'.repeat(64) },
  ] })
}, 15000)

test('classic forensics automatically selects an upload even when it is absent from the current image page', async () => {
  let registered: NonNullable<UploadStatus['receipt']>['asset'] | undefined
  const submitted: { asset_id: string; asset_revision: number; blob_hash: string; tools: string[] }[] = []
  const selectedReads: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (raw: string, init?: RequestInit) => {
    const url = new URL(raw, 'http://local')
    if (url.pathname.includes('/assets/uploads/') && init?.method === 'POST') {
      const status = committed(binding(raw), 'uploadedPhoto'); registered = status.receipt!.asset; return response(status)
    }
    if (url.pathname.endsWith('/assets')) return response({ items: [], total: 80, next_offset: null })
    if (url.pathname.endsWith('/assets/uploadedPhoto')) { selectedReads.push(raw); return response(registered) }
    if (url.pathname.endsWith('/forensics') && init?.method === 'POST') {
      submitted.push(JSON.parse(String(init.body)))
      return response({ inspection_id: 'inspection', status: 'queued' })
    }
    if (url.pathname.endsWith('/forensics/inspection')) return response({ inspection_id: 'inspection', workspace_id: 'wStart',
      original_name: registered?.original_name, asset_id: 'uploadedPhoto', asset_revision: 2, revision: 1, status: 'queued', report: null, review: null, files: [] })
    return response({ items: [], total: 0 })
  }))
  const router = mount(<ForensicWorkbench />, '&task_return=taskFromAgent')
  const start = screen.getByRole('button', { name: '开始本地取证' })
  expect(start).toBeDisabled()
  const input = screen.getByLabelText('添加审计资料')
  expect(input).not.toHaveAttribute('multiple')
  fireEvent.change(input, { target: { files: [new File(['image'], '甲原图.png', { type: 'image/png' })] } })
  await waitFor(() => expect(start).toBeEnabled())
  expect(selectedReads.length).toBeGreaterThan(0)
  expect(submitted).toHaveLength(0)
  expect(screen.queryByRole('button', { name: '上传未提交资料' })).not.toBeInTheDocument()
  expect(screen.getByText(/处理设置 · 已选择 6 项检查/).closest('details')).not.toHaveAttribute('open')
  fireEvent.click(start)
  await waitFor(() => expect(submitted).toHaveLength(1))
  expect(submitted[0]).toMatchObject({ asset_id: 'uploadedPhoto', asset_revision: 2, blob_hash: 'a'.repeat(64),
    tools: ['metadata', 'noise', 'frequency', 'jpeg', 'jpeg_grid', 'copy_move'] })
  await waitFor(() => expect(new URLSearchParams(router.state.location.search).get('inspection')).toBe('inspection'))
  expect(new URLSearchParams(router.state.location.search).get('task_return')).toBe('taskFromAgent')
}, 15000)

test('selecting only the second photograph cannot start until an uploaded first photograph fills the empty slot', async () => {
  const submitted: unknown[] = []
  let firstPhoto: NonNullable<UploadStatus['receipt']>['asset'] | undefined
  const finished = { job_id: 'comparison', kind: 'face', status: 'succeeded', stage: 'completed', revision: 1, inputs: [], result: null }
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/multimodal/capabilities')) return response({ items: [{ kind: 'face', cpu_ready: true, gpu_ready: false, missing: [] }] })
    if (path.includes('/assets/uploads/') && init?.method === 'POST') {
      const status = committed(binding(path), 'first'); firstPhoto = status.receipt!.asset; return response(status)
    }
    if (path.includes('/assets?')) return response({ items: [
      { asset_id: 'second', revision: 1, blob_hash: 'b'.repeat(64), original_name: '仅选第二张.png', media_type: 'image/png' }, ...(firstPhoto ? [firstPhoto] : []),
    ], next_offset: null })
    if (path.endsWith('/multimodal/jobs') && init?.method === 'POST') { submitted.push(JSON.parse(String(init.body))); return response(finished) }
    if (path.endsWith('/multimodal/jobs/comparison')) return response(finished)
    return response({ items: [], total: 0 })
  }))
  mount(<MultimodalWorkbench kind="face" />)
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '人脸分析方式' }))
  fireEvent.click(await screen.findByText('两张照片比对'))
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '照片 2' }))
  fireEvent.click(await screen.findByText('仅选第二张.png'))
  const start = screen.getByRole('button', { name: '开始两张照片比对' })
  expect(start).toBeDisabled()
  fireEvent.click(start)
  expect(submitted).toHaveLength(0)
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [new File(['first'], '甲第一张.png', { type: 'image/png' })] } })
  await waitFor(() => expect(start).toBeEnabled())
  expect(submitted).toHaveLength(0)
  fireEvent.click(start)
  await waitFor(() => expect(submitted).toHaveLength(1))
  expect(submitted[0]).toMatchObject({ inputs: [
    { asset_id: 'first', asset_revision: 2, blob_hash: 'a'.repeat(64) }, { asset_id: 'second', asset_revision: 1, blob_hash: 'b'.repeat(64) },
  ] })
}, 15000)

test('classic forensic history and tool navigation retain the task without leaking the previous report identity', async () => {
  const inspection = { inspection_id: 'history', workspace_id: 'wStart', asset_id: 'original', asset_revision: 2, revision: 1,
    original_name: '历史图片.png', status: 'succeeded', report: null, files: [], review: null }
  vi.stubGlobal('fetch', vi.fn(async (raw: string) => {
    const url = new URL(raw, 'http://local')
    if (url.pathname.endsWith('/assets')) return response({ items: [], total: 0 })
    if (url.pathname.endsWith('/forensics/old')) return response({ ...inspection, inspection_id: 'old' })
    return response({ items: [inspection], total: 1 })
  }))
  mount(<ForensicWorkbench />, '&task_return=returnTask&inspection=old&pane=heatmap&revision=7', '/apps/forensics')
  expect(await screen.findByRole('link', { name: '查看报告与审阅' })).toHaveAttribute('href', '/apps/forensics?workspace=wStart&task_return=returnTask&inspection=history')
  expect(screen.getByRole('link', { name: '← 返回应用目录' })).toHaveAttribute('href', '/apps?workspace=wStart&task_return=returnTask')
  expect(screen.getByRole('link', { name: '打开 SAFIRE 深度定位 →' })).toHaveAttribute('href', '/apps/forensics/deep?workspace=wStart&task_return=returnTask')
}, 15000)

test('continuing from a fixed deep result preserves the task and explicit original image while keeping downloads pinned', () => {
  const analysis = { job_id: 'deep', kind: 'catnet' as const, status: 'succeeded', stage: 'completed', revision: 2, error: null, result_hash: 'c'.repeat(64),
    inputs: [{ original_name: '原图.png', asset_id: 'original', asset_revision: 3 }], created_at: '', review: null,
    result: { outcome: 'analyzed', algorithm: 'SAFIRE', device: 'cuda', elapsed_seconds: 1, limitations: [], width: 64, height: 64 } }
  mount(<ResultView item={analysis} workspaceId="wStart" refresh={() => {}} referenceName="" setReferenceName={() => {}} onReference={() => {}} referenceBusy={false} readOnly snapshotId="fixed" />,
    '&task_return=returnTask&analysis=deep&snapshot=fixed&sha256=oldHash&revision=2', '/apps/forensics/deep')
  expect(screen.getByRole('link', { name: '使用经典取证工具继续核查' })).toHaveAttribute('href', '/apps/forensics?workspace=wStart&task_return=returnTask&asset=original')
  expect(screen.getByRole('link', { name: '分析同一原图的整图生成信号' })).toHaveAttribute('href', '/apps/forensics/aigc?workspace=wStart&task_return=returnTask&asset=original')
  expect(screen.getByRole('link', { name: '下载此固定版本' })).toHaveAttribute('href', '/api/platform/v1/workspaces/wStart/multimodal/jobs/deep/snapshots/fixed/download')
})
