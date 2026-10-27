import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { DocumentPanel } from '../src/features/documents/DocumentWorkbench'

vi.mock('../src/features/batches/fileHash', () => ({ AUDIO_CHUNK_BYTES: 8 * 1024 * 1024, fileSha256: async () => 'b'.repeat(64), chunkSha256: async () => 'b'.repeat(64) }))

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
const clients: QueryClient[] = []
afterEach(() => {
  cleanup(); clients.splice(0).forEach(client => client.clear())
  vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear()
})
const ok = (value: unknown) => ({ ok: true, json: async () => value })
const asset = { asset_id: 'from-library', revision: 7, blob_hash: 'a'.repeat(64), original_name: '扫描页.png', media_type: 'image/png' }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } } })
  clients.push(client)
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/apps/ocr/documents?asset=from-library']}>
    <DocumentPanel workspace="debug" />
  </MemoryRouter></QueryClientProvider>)
}
function delayedAssets() {
  let resolve!: (value: ReturnType<typeof ok>) => void
  const pending = new Promise<ReturnType<typeof ok>>(done => { resolve = done })
  const writes: unknown[] = []
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes('/assets?')) return pending
    if (url.endsWith('/documents/parse') && init?.method === 'POST') {
      writes.push(JSON.parse(String(init.body)))
      return ok({ document_id: 'parsed', status: 'succeeded', document: null })
    }
    if (url.includes('/documents?')) return ok({ items: [], total: 0, next_offset: null })
    if (url.endsWith('/documents/parsed')) return ok({ document_id: 'parsed', status: 'succeeded', document: null })
    throw new Error(`Unexpected document request ${url}`)
  })
  vi.stubGlobal('fetch', fetcher)
  return { resolve, writes }
}

test('a library-selected asset waits for its identity before submitting the exact returned version', async () => {
  const { resolve, writes } = delayedAssets()
  mount()
  const parse = screen.getByRole('button', { name: /解析文档/ })
  expect(parse).toBeDisabled()
  fireEvent.click(parse)
  expect(writes).toEqual([])
  expect(screen.queryByText('请先选择需要解析的资料。')).not.toBeInTheDocument()
  await act(async () => { resolve(ok({ items: [asset], total: 1, next_offset: null })) })
  await waitFor(() => expect(parse).toBeEnabled())
  fireEvent.click(parse)
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0]).toEqual({ schema_version: '1.0', request_id: expect.any(String),
    asset_id: asset.asset_id, asset_revision: 7, blob_hash: asset.blob_hash, ocr_policy: 'auto' })
})

test('a pending request can still be verified while the asset library is loading', async () => {
  const saved = { request_id: 'previous-request', asset_id: 'original-asset', asset_revision: 3, blob_hash: 'b'.repeat(64) }
  localStorage.setItem('clear-document-parse:debug', JSON.stringify(saved))
  const { resolve, writes } = delayedAssets()
  mount()
  const verify = await screen.findByRole('button', { name: '核实上次解析请求' })
  await waitFor(() => expect(verify).toBeEnabled())
  fireEvent.click(verify)
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0]).toEqual({ schema_version: '1.0', ...saved, ocr_policy: 'auto' })
  await waitFor(() => expect(localStorage.getItem('clear-document-parse:debug')).toBeNull())
  await act(async () => { resolve(ok({ items: [asset], total: 1, next_offset: null })) })
})

test('selecting a document uploads and selects it automatically, then waits for its returned identity', async () => {
  let finishUpload!: (value: ReturnType<typeof ok>) => void, finishAssets!: (value: ReturnType<typeof ok>) => void
  const uploadReply = new Promise<ReturnType<typeof ok>>(resolve => { finishUpload = resolve })
  const assetsReply = new Promise<ReturnType<typeof ok>>(resolve => { finishAssets = resolve })
  const writes: unknown[] = [], uploaded = { ...asset, asset_id: 'new-document', original_name: '新文档.pdf', revision: 2, blob_hash: 'b'.repeat(64) }
  let reads = 0, uploadBinding: Record<string, unknown> | undefined
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/assets?')) return ++reads === 1 ? ok({ items: [asset], next_offset: null }) : assetsReply
    if (path.includes('/assets/uploads/') && init?.method === 'POST') {
      const url = new URL(path, 'http://local')
      uploadBinding = { schema_version: '1.0', workspace_id: 'debug', request_id: url.pathname.split('/').at(-1), name: uploaded.original_name, size_bytes: Number(url.searchParams.get('size_bytes')), sha256: uploaded.blob_hash }
      return uploadReply
    }
    if (path.endsWith('/documents/parse') && init?.method === 'POST') { writes.push(JSON.parse(String(init.body))); return ok({ document_id: 'parsed', status: 'succeeded', document: null }) }
    if (path.includes('/documents?')) return ok({ items: [], total: 0 })
    if (path.endsWith('/documents/parsed')) return ok({ document_id: 'parsed', status: 'succeeded', document: null })
    throw new Error('Unexpected document request ' + path)
  }))
  mount()
  const parse = await screen.findByRole('button', { name: '解析文档' })
  await waitFor(() => expect(parse).toBeEnabled())
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [new File(['document'], uploaded.original_name, { type: 'application/pdf' })] } })
  expect(parse).toBeDisabled()
  await waitFor(() => expect(uploadBinding).toBeDefined())
  expect(screen.queryByRole('button', { name: '上传未提交资料' })).not.toBeInTheDocument()
  await act(async () => { finishUpload(ok({ status: 'committed', binding: uploadBinding, receipt: { binding: uploadBinding, asset: { ...uploaded, workspace_id: 'debug', size_bytes: 8 } } })) })
  await waitFor(() => expect(reads).toBeGreaterThan(1))
  expect(parse).toBeDisabled()
  expect(writes).toEqual([])
  await act(async () => { finishAssets(ok({ items: [asset, uploaded], next_offset: null })) })
  await waitFor(() => expect(parse).toBeEnabled())
  fireEvent.click(parse)
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0]).toMatchObject({ asset_id: uploaded.asset_id, asset_revision: 2, blob_hash: uploaded.blob_hash })
})

test('a failed replacement document does not leave the previous file ready to parse', async () => {
  const writes: unknown[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/assets?')) return ok({ items: [asset], next_offset: null })
    if (path.includes('/assets/uploads/')) {
      if (init?.method === 'POST') throw new Error('新文档上传失败')
      return { ok: false, status: 404, json: async () => ({ detail: '此上传未登记' }) }
    }
    if (path.endsWith('/documents/parse') && init?.method === 'POST') { writes.push(JSON.parse(String(init.body))); return ok({ document_id: 'parsed', status: 'succeeded', document: null }) }
    if (path.includes('/documents?')) return ok({ items: [], total: 0 })
    throw new Error('Unexpected document request ' + path)
  }))
  mount()
  const parse = await screen.findByRole('button', { name: '解析文档' })
  await waitFor(() => expect(parse).toBeEnabled())
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [new File(['replacement'], '替换文档.pdf', { type: 'application/pdf' })] } })
  expect(parse).toBeDisabled()
  await screen.findByText('新文档上传失败')
  expect(parse).toBeDisabled()
  fireEvent.click(parse)
  expect(writes).toEqual([])
})
