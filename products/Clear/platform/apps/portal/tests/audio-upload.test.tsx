import '@testing-library/jest-dom/vitest'
import { createHash, randomUUID } from 'node:crypto'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { UploadQueue } from '../src/features/batches/UploadQueue'
import { AUDIO_CHUNK_BYTES, readBlob } from '../src/features/batches/fileHashSlices'

beforeAll(() => {
  configure({ asyncUtilTimeout: 15000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  localStorage.clear()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
  vi.stubGlobal('crypto', { randomUUID, subtle: { digest: async (_: string, value: ArrayBuffer) => {
    const digest = createHash('sha256').update(Buffer.from(value)).digest()
    return digest.buffer.slice(digest.byteOffset, digest.byteOffset + digest.byteLength)
  } } })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function mount(allowLargeAudio = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<ConfigProvider><QueryClientProvider client={client}><UploadQueue workspaceId="wAudio" allowLargeAudio={allowLargeAudio} /></QueryClientProvider></ConfigProvider>)
}
const response = (value: unknown) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(value)) })
const missing = () => ({ ok: false, status: 404, json: async () => ({ detail: '未登记' }) })
function server(lostFirst = false, failBeforeSecond = false) {
  let status: any = null
  let lost = lostFirst, fail = failBeforeSecond
  const mutations: string[] = []
  const reads: number[] = []
  const chunks: number[] = []
  const time = '2026-10-14T03:00:00Z'
  vi.stubGlobal('fetch', vi.fn(async (raw: string, init?: RequestInit) => {
    const url = new URL(raw, 'http://local')
    if (!init?.method) { reads.push(status?.next_offset ?? -1); return status ? response(status) : missing() }
    mutations.push(url.pathname.split('/').at(-1)!)
    if (init.method === 'POST' && !url.pathname.endsWith('/complete') && !url.pathname.endsWith('/cancel')) {
      const binding = JSON.parse(init.body as string)
      status = { schema_version: '1.0', binding, payload_hash: 'a'.repeat(64), status: 'uploading', next_offset: 0, chunk_bytes: AUDIO_CHUNK_BYTES, receipt: null, error: null, created_at: time, updated_at: time }
    } else if (init.method === 'PUT') {
      const offset = Number(url.searchParams.get('offset'))
      chunks.push(offset)
      if (fail && offset === AUDIO_CHUNK_BYTES) { fail = false; throw new Error('second chunk connection lost before acceptance') }
      const content = await readBlob(init.body as Blob)
      expect(url.searchParams.get('sha256')).toBe(createHash('sha256').update(Buffer.from(content)).digest('hex'))
      expect(offset).toBe(status.next_offset)
      status.next_offset += (init.body as Blob).size
      if (lost) { lost = false; throw new Error('first chunk accepted but response lost') }
    } else if (url.pathname.endsWith('/complete')) {
      status.status = 'committed'
      status.receipt = { schema_version: '1.0', binding: status.binding, payload_hash: status.payload_hash, created_at: time,
        asset: { schema_version: '1.0', asset_id: 'aAudio', workspace_id: 'wAudio', source_id: 'sAudio', original_name: status.binding.name, media_type: 'audio/wav', storage_mode: 'managed', blob_hash: status.binding.sha256, size_bytes: status.binding.size_bytes, revision: 1, created_at: time } }
    } else if (url.pathname.endsWith('/cancel')) status.status = 'cancelled'
    return response(status)
  }))
  return { mutations, reads, chunks, status: () => status }
}
const select = (file: File) => fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: [file] } })
async function uploadButton() {
  await waitFor(() => expect(screen.getByRole('button', { name: /上传未提交资料|核实并恢复失败与未提交/ })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: /上传未提交资料|核实并恢复失败与未提交/ }))
}

test('an accepted chunk with a lost response is queried and the next chunk proceeds without retransmitting it', async () => {
  const service = server(true)
  mount()
  select(new File([new Uint8Array(AUDIO_CHUNK_BYTES + 17).fill(23)], '长录音.wav'))
  await screen.findByText('长录音.wav')
  await uploadButton()
  await screen.findByText('成功 1 · 失败 / 待核实 0 · 未提交 0')
  expect(service.chunks).toEqual([0, AUDIO_CHUNK_BYTES])
  expect(service.reads).toContain(AUDIO_CHUNK_BYTES)
  expect(service.status().status).toBe('committed')
}, 30000)

test('reload resumes from the server offset after reselecting the matching original file', async () => {
  const service = server(false, true)
  const file = new File([new Uint8Array(AUDIO_CHUNK_BYTES + 29).fill(31)], '可恢复.wav')
  const first = mount(); select(file)
  await screen.findByText('可恢复.wav'); await uploadButton()
  await screen.findByText('成功 0 · 失败 / 待核实 1 · 未提交 0')
  expect(service.status().next_offset).toBe(AUDIO_CHUNK_BYTES)
  first.unmount(); mount()
  select(file)
  await waitFor(() => expect(screen.getByRole('button', { name: '核实并恢复失败与未提交' })).toBeEnabled())
  await uploadButton()
  await screen.findByText('成功 1 · 失败 / 待核实 0 · 未提交 0')
  expect(service.chunks).toEqual([0, AUDIO_CHUNK_BYTES, AUDIO_CHUNK_BYTES])
  expect(service.mutations.filter(value => value.startsWith('r'))).toHaveLength(1)
}, 30000)

test('large non-audio remains limited to 128 MiB and cancelling a queued audio preserves its record', async () => {
  const service = server()
  mount()
  const large = new File(['not actually allocated'], '资料.pdf')
  Object.defineProperty(large, 'size', { value: 128 * 1024 * 1024 + 1 })
  select(large)
  await screen.findByText('资料.pdf：请选择非空且不超过 128 MB 的文件')
  select(new File(['audio'], '未提交.wav'))
  await screen.findByText('未提交.wav')
  await waitFor(() => expect(screen.getByRole('button', { name: '取消此上传' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '取消此上传' }))
  await screen.findByText('已取消')
  expect(service.mutations).toEqual([])
  expect(JSON.parse(localStorage.getItem('clear-upload-queue:wAudio')!)[0].state).toBe('cancelled')
}, 20000)
