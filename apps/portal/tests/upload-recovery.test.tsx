import '@testing-library/jest-dom/vitest'
import { createHash, randomUUID } from 'node:crypto'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { UploadQueue } from '../src/features/batches/UploadQueue'
import type { UploadBinding, UploadStatus } from '../../../packages/contracts/types.generated'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  localStorage.clear()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
  vi.stubGlobal('crypto', { randomUUID, subtle: { digest: async (_: string, value: ArrayBuffer) => { const hash = createHash('sha256').update(Buffer.from(value)).digest(); return hash.buffer.slice(hash.byteOffset, hash.byteOffset + hash.byteLength) } } })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = (value: unknown) => ({ ok: true, json: async () => value })
const missing = () => ({ ok: false, status: 404, json: async () => ({ detail: '未知原请求' }) })
function committed(binding: UploadBinding): UploadStatus {
  const time = '2026-10-03T03:00:00Z'
  return { schema_version: '1.0', binding, payload_hash: 'a'.repeat(64), status: 'committed', created_at: time, updated_at: time,
    receipt: { schema_version: '1.0', binding, payload_hash: 'a'.repeat(64), created_at: time,
      asset: { schema_version: '1.0', asset_id: 'a' + binding.request_id, workspace_id: 'wUpload', source_id: 'sUpload', original_name: binding.name, media_type: 'text/csv', storage_mode: 'managed', blob_hash: binding.sha256, size_bytes: binding.size_bytes, revision: 1, created_at: time } } }
}
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<ConfigProvider><QueryClientProvider client={client}><UploadQueue workspaceId="wUpload" /></QueryClientProvider></ConfigProvider>)
}
function binding(path: string): UploadBinding {
  const url = new URL(path, 'http://local')
  return { schema_version: '1.0', workspace_id: 'wUpload', request_id: url.pathname.split('/').at(-1)!, name: url.searchParams.get('name')!, sha256: url.searchParams.get('sha256')!, size_bytes: Number(url.searchParams.get('size_bytes')) }
}

test('ten files continue past a file failure, persist receipts, then reload retries only the failed file with its original metadata', async () => {
  const original = Array.from({ length: 10 }, (_, index) => new File([`编号,金额\n${index},01.10`], `file${index + 1}.csv`))
  const submitted: UploadBinding[] = []
  const registered = new Map<string, UploadStatus>()
  let failFourth = true
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      const value = binding(path); submitted.push(value)
      if (value.name === 'file4.csv' && failFourth) { failFourth = false; throw new Error('第 4 个文件连接中断') }
      registered.set(value.request_id, committed(value)); return response(registered.get(value.request_id))
    }
    const id = path.split('/').at(-1)!
    return registered.has(id) ? response(registered.get(id)) : missing()
  }))
  const first = mount()
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: original } })
  await screen.findByText('file10.csv')
  await waitFor(() => expect(screen.getByRole('button', { name: '上传未提交资料' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '上传未提交资料' }))
  await screen.findByText('成功 9 · 失败 / 待核实 1 · 未提交 0')
  const before = JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)
  expect(before.map((row: any) => row.state)).toEqual(['committed', 'committed', 'committed', 'failed', ...Array(6).fill('committed')])
  expect(before[0].receipt.asset.original_name).toBe('file1.csv')
  expect(submitted).toHaveLength(10)
  first.unmount(); mount()
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: [new File(['别的内容'], 'file4.csv')] } })
  await screen.findByText('file4.csv：名称相同但大小或 SHA256 不同，不能替换原恢复请求')
  expect(JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)[3].binding).toEqual(before[3].binding)
  expect(submitted).toHaveLength(10)
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: [original[3]] } })
  await waitFor(() => expect(screen.getByRole('button', { name: '核实并恢复失败与未提交' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '核实并恢复失败与未提交' }))
  await screen.findByText('成功 10 · 失败 / 待核实 0 · 未提交 0')
  expect(submitted).toHaveLength(11)
  expect(submitted[10]).toEqual(submitted[3])
  expect(submitted.filter(value => ['file1.csv', 'file2.csv', 'file3.csv'].includes(value.name))).toHaveLength(3)
  expect(registered.size).toBe(10)
  const saved = JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)
  expect(saved.every((row: any) => row.receipt && !('file' in row))).toBe(true)
}, 30000)

test('a lost upload response is recovered from its durable request without uploading the successful body again', async () => {
  let saved: UploadStatus | null = null
  let posts = 0, reads = 0
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { posts++; saved = committed(binding(path)); throw new Error('响应丢失') }
    reads++; return response(saved)
  }))
  mount()
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: [new File(['收入\n01.10'], '收入.csv')] } })
  await screen.findByText('收入.csv')
  await waitFor(() => expect(screen.getByRole('button', { name: '上传未提交资料' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '上传未提交资料' }))
  await screen.findByText('成功 1 · 失败 / 待核实 0 · 未提交 0')
  expect(posts).toBe(1); expect(reads).toBe(1)
  expect(JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)[0].receipt).toEqual(saved!.receipt)
}, 15000)

test('uploads are bounded to three concurrent bodies and every input eventually has a receipt', async () => {
  const releases: (() => void)[] = [], saved = new Map<string, UploadStatus>()
  let active = 0, maximum = 0, posts = 0
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method !== 'POST') return saved.has(path.split('/').at(-1)!) ? response(saved.get(path.split('/').at(-1)!)) : missing()
    const value = binding(path); active++; posts++; maximum = Math.max(maximum, active)
    await new Promise<void>(resolve => releases.push(resolve)); active--
    saved.set(value.request_id, committed(value)); return response(saved.get(value.request_id))
  }))
  mount()
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: Array.from({ length: 8 }, (_, index) => new File([String(index)], `input${index}.png`)) } })
  await screen.findByText('input7.png')
  await waitFor(() => expect(screen.getByRole('button', { name: '上传未提交资料' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '上传未提交资料' }))
  await waitFor(() => expect(posts).toBe(3))
  expect(active).toBe(3)
  for (let index = 0; index < 8; index++) {
    await waitFor(() => expect(releases.length).toBeGreaterThan(index))
    releases[index]()
  }
  await screen.findByText('成功 8 · 失败 / 待核实 0 · 未提交 0')
  expect(maximum).toBe(3); expect(posts).toBe(8); expect(saved.size).toBe(8)
}, 30000)
