import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { MultimodalWorkbench } from '../src/features/multimodal/MultimodalWorkbench'
import type { UploadBinding, UploadStatus } from '../../../packages/contracts/types.generated'

vi.mock('../src/App', () => ({ useWorkspaceId: () => 'workspace', Workspaces: () => null }))
vi.mock('../src/features/batches/fileHash', () => ({
  AUDIO_CHUNK_BYTES: 8 * 1024 * 1024,
  fileSha256: async () => 'b'.repeat(64),
  chunkSha256: async () => 'b'.repeat(64),
}))

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear() })

const response = (value: unknown) => ({ ok: true, json: async () => value })
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function mount(query = '') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element: <MultimodalWorkbench kind="aigc_image" /> }], {
    initialEntries: ['/apps/forensics/aigc?workspace=workspace' + query],
  })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
}
const capabilities = { items: [{ kind: 'aigc_image', state: 'ready', cpu_ready: false, gpu_ready: true, missing: [] }] }
const existing = { asset_id: 'previous', original_name: '旧图.png', media_type: 'image/png', revision: 1, blob_hash: 'a'.repeat(64) }
const uploaded = { asset_id: 'uploaded', original_name: '新图.png', media_type: 'image/png', revision: 3, blob_hash: 'b'.repeat(64) }
const finished = { job_id: 'analysis', kind: 'aigc_image', status: 'succeeded', stage: 'completed', revision: 1,
  error: null, result_hash: null, inputs: [{ original_name: uploaded.original_name, asset_id: uploaded.asset_id, asset_revision: uploaded.revision }],
  result: null, created_at: '', review: null }
function binding(path: string): UploadBinding {
  const url = new URL(path, 'http://local')
  return { schema_version: '1.0', workspace_id: 'workspace', request_id: url.pathname.split('/').at(-1)!,
    name: url.searchParams.get('name')!, sha256: url.searchParams.get('sha256')!, size_bytes: Number(url.searchParams.get('size_bytes')) }
}
function committed(value: UploadBinding): UploadStatus {
  const time = '2026-10-26T10:00:00Z'
  return { schema_version: '1.0', binding: value, payload_hash: 'c'.repeat(64), status: 'committed', created_at: time, updated_at: time,
    receipt: { schema_version: '1.0', binding: value, payload_hash: 'c'.repeat(64), created_at: time,
      asset: { schema_version: '1.0', ...uploaded, workspace_id: 'workspace', source_id: 'source', storage_mode: 'managed',
        size_bytes: value.size_bytes, created_at: time } } }
}

test('selecting a PNG uploads automatically, waits for current asset metadata and starts only the selected image', async () => {
  const uploadReply = deferred<ReturnType<typeof response>>(), assetReply = deferred<ReturnType<typeof response>>()
  const uploads: UploadBinding[] = [], analyses: { kind: string; inputs: unknown[]; options: unknown }[] = []
  let mediaReads = 0
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/multimodal/capabilities')) return response(capabilities)
    if (path.includes('/assets/uploads/') && init?.method === 'POST') {
      uploads.push(binding(path))
      return uploadReply.promise
    }
    if (path.includes('/assets?')) {
      mediaReads++
      return mediaReads === 1 ? response({ items: [existing], next_offset: null }) : assetReply.promise
    }
    if (path.endsWith('/multimodal/jobs') && init?.method === 'POST') {
      analyses.push(JSON.parse(String(init.body)))
      return response(finished)
    }
    if (path.endsWith('/multimodal/jobs/analysis')) return response(finished)
    if (path.includes('/multimodal/jobs?')) return response({ items: [], total: 0 })
    throw new Error('Unexpected request: ' + path)
  }))
  mount()
  const start = screen.getByRole('button', { name: '开始整图 AI 生成检测' })
  await waitFor(() => expect(screen.getByText('GPU 已就绪')).toBeVisible())
  expect(start).toBeDisabled()
  expect(screen.queryByRole('button', { name: '上传未提交资料' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [new File(['image content'], uploaded.original_name, { type: 'image/png' })] } })
  await waitFor(() => expect(uploads).toHaveLength(1))
  expect(start).toBeDisabled()
  fireEvent.click(start)
  expect(analyses).toHaveLength(0)

  await act(async () => { uploadReply.resolve(response(committed(uploads[0]))) })
  await waitFor(() => expect(mediaReads).toBeGreaterThan(1))
  expect(start).toBeDisabled()
  fireEvent.click(start)
  expect(analyses).toHaveLength(0)

  await act(async () => { assetReply.resolve(response({ items: [existing, uploaded], next_offset: null })) })
  await waitFor(() => expect(start).toBeEnabled())
  expect(screen.getByText('已上传')).toBeVisible()
  expect(screen.queryByRole('button', { name: '上传未提交资料' })).not.toBeInTheDocument()
  fireEvent.click(start)
  await waitFor(() => expect(analyses).toHaveLength(1))
  expect(uploads[0]).toMatchObject({ name: uploaded.original_name, sha256: uploaded.blob_hash })
  expect(analyses[0]).toMatchObject({ kind: 'aigc_image', inputs: [{ asset_id: uploaded.asset_id, asset_revision: uploaded.revision, blob_hash: uploaded.blob_hash }],
    options: { device: 'cuda', action: 'analyze' } })
  expect(analyses[0].inputs).toHaveLength(1)
}, 15000)

test('a failed replacement upload cannot start detection on the previously selected image', async () => {
  const analyses: unknown[] = []
  let uploads = 0
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/multimodal/capabilities')) return response(capabilities)
    if (path.includes('/assets?')) return response({ items: [existing], next_offset: null })
    if (path.includes('/assets/uploads/')) {
      if (init?.method === 'POST') { uploads++; throw new Error('新图上传失败') }
      return { ok: false, status: 404, json: async () => ({ detail: '未登记此上传' }) }
    }
    if (path.endsWith('/multimodal/jobs') && init?.method === 'POST') { analyses.push(JSON.parse(String(init.body))); return response(finished) }
    if (path.includes('/multimodal/jobs?')) return response({ items: [], total: 0 })
    throw new Error('Unexpected request: ' + path)
  }))
  mount('&asset=' + existing.asset_id)
  const start = screen.getByRole('button', { name: '开始整图 AI 生成检测' })
  await waitFor(() => expect(start).toBeEnabled())
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [new File(['replacement'], uploaded.original_name, { type: 'image/png' })] } })
  expect(start).toBeDisabled()
  await screen.findByText('新图上传失败')
  await waitFor(() => expect(screen.getByRole('button', { name: '核实并继续上传' })).toBeEnabled())
  expect(uploads).toBe(1)
  expect(start).toBeDisabled()
  fireEvent.click(start)
  expect(analyses).toHaveLength(0)
}, 15000)
