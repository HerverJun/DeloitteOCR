import '@testing-library/jest-dom/vitest'
import { createHash, randomUUID } from 'node:crypto'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
function mount(autoUpload = false, onUploaded?: (id: string) => void, compact = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<ConfigProvider><QueryClientProvider client={client}><UploadQueue workspaceId="wUpload" autoUpload={autoUpload} onUploaded={onUploaded} compact={compact} /></QueryClientProvider></ConfigProvider>)
}
function binding(path: string): UploadBinding {
  const url = new URL(path, 'http://local')
  return { schema_version: '1.0', workspace_id: 'wUpload', request_id: url.pathname.split('/').at(-1)!, name: url.searchParams.get('name')!, sha256: url.searchParams.get('sha256')!, size_bytes: Number(url.searchParams.get('size_bytes')) }
}

test('one failed file is isolated while nine independent files finish, then reload recovers only the original failed request', async () => {
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
  expect(submitted).toHaveLength(0)
  await waitFor(() => expect(screen.getByRole('button', { name: '上传未提交资料' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '上传未提交资料' }))
  await screen.findByText('成功 9 · 失败 / 待核实 1 · 未提交 0')
  const before = JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)
  expect(before.map((row: any) => row.state)).toEqual(['committed', 'committed', 'committed', 'failed', ...Array(6).fill('committed')])
  expect(before[0].receipt.asset.original_name).toBe('file1.csv')
  expect(submitted).toHaveLength(10)
  first.unmount(); mount()
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

test('removing a never-submitted ordinary file leaves other uploads available and sends no request for the removed file', async () => {
  const posted: UploadBinding[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => { const value = binding(path); posted.push(value); return response(committed(value)) }))
  mount()
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: [new File(['wrong'], '误选.csv'), new File(['right'], '正确.csv')] } })
  const row = await screen.findByRole('row', { name: /误选.csv/ })
  await waitFor(() => expect(within(row).getByRole('button', { name: '移出本次上传' })).toBeEnabled())
  fireEvent.click(within(row).getByRole('button', { name: '移出本次上传' }))
  expect(within(row).getByText('已移出，未提交文件')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '上传未提交资料' }))
  await screen.findByText('成功 1 · 失败 / 待核实 0 · 未提交 0')
  expect(posted.map(value => value.name)).toEqual(['正确.csv'])
  expect(JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)[0].state).toBe('cancelled')
})

test('same-name changed content requires an explicit new upload while the unknown original request remains intact', async () => {
  const posted: UploadBinding[] = [], uploaded = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (!init?.method) throw new Error('原回执暂不可读取')
    const value = binding(path); posted.push(value)
    if (posted.length === 1) throw new Error('原上传回包未知')
    return response(committed(value))
  }))
  mount(true, uploaded, true)
  const input = screen.getByLabelText('添加审计资料')
  fireEvent.change(input, { target: { files: [new File(['original'], '资料.csv')] } })
  await screen.findByText('原上传回包未知')
  const original = JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)[0]
  fireEvent.change(input, { target: { files: [new File(['revised content'], '资料.csv')] } })
  await screen.findByText('资料.csv：与已有同名文件内容不同')
  expect(posted).toHaveLength(1)
  await waitFor(() => expect(screen.getByRole('button', { name: '作为新资料上传' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '作为新资料上传' }))
  await waitFor(() => expect(uploaded).toHaveBeenCalledTimes(1))
  const rows = JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)
  expect(rows).toHaveLength(2)
  expect(rows[0]).toEqual(original)
  expect(rows[1].state).toBe('committed')
  expect(posted[1].name).toBe(posted[0].name)
  expect(posted[1].request_id).not.toBe(posted[0].request_id)
  expect(posted[1].sha256).not.toBe(posted[0].sha256)
  expect(screen.queryByRole('button', { name: '移出本次上传' })).not.toBeInTheDocument()
})

test.each([401, 403, 503])('HTTP %s stops the queue after reconciling the current upload, retaining later files', async status => {
  let posts = 0, reads = 0
  vi.stubGlobal('fetch', vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { posts++; return { ok: false, status, json: async () => ({ detail: '项目服务或权限不可用' }) } }
    reads++; return missing()
  }))
  mount()
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: [new File(['one'], '一.csv'), new File(['two'], '二.csv')] } })
  await screen.findByText('二.csv')
  await waitFor(() => expect(screen.getByRole('button', { name: '上传未提交资料' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '上传未提交资料' }))
  await screen.findByText(/上传已暂停，请恢复连接或项目权限后继续/)
  expect(posts).toBe(1); expect(reads).toBe(1)
  expect(JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!).map((row: any) => row.state)).toEqual(['failed', 'queued'])
})

test('a permission failure still stops later writes when reconciliation finds the current file already committed', async () => {
  let accepted: UploadStatus | undefined, posts = 0
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      posts++; accepted = committed(binding(path))
      return { ok: false, status: 403, json: async () => ({ detail: '写入权限已变化' }) }
    }
    return response(accepted)
  }))
  mount()
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: [new File(['one'], '一.csv'), new File(['two'], '二.csv')] } })
  await screen.findByText('二.csv')
  await waitFor(() => expect(screen.getByRole('button', { name: '上传未提交资料' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '上传未提交资料' }))
  await screen.findByText(/上传已暂停，请恢复连接或项目权限后继续/)
  expect(posts).toBe(1)
  expect(JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!).map((row: any) => row.state)).toEqual(['committed', 'queued'])
})

test('user pause wins over a late successful receipt and never submits the next file', async () => {
  let finish!: () => void
  const posted: UploadBinding[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const value = binding(path)
    if (init?.method === 'POST') { posted.push(value); await new Promise<void>(resolve => { finish = resolve }) }
    return response(committed(value))
  }))
  mount()
  fireEvent.change(screen.getByLabelText('添加批次资料'), { target: { files: [new File(['one'], '一.csv'), new File(['two'], '二.csv')] } })
  await screen.findByText('二.csv')
  await waitFor(() => expect(screen.getByRole('button', { name: '上传未提交资料' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '上传未提交资料' }))
  await waitFor(() => expect(posted).toHaveLength(1))
  fireEvent.click(screen.getByRole('button', { name: '暂停上传 / 校验' }))
  finish()
  await screen.findByText('成功 1 · 失败 / 待核实 0 · 未提交 1')
  expect(posted).toHaveLength(1)
})

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

test('opt-in selection uploads once and re-selecting the committed file attaches its verified original asset', async () => {
  const uploaded = vi.fn(), original = new File(['编号,金额\n001,01.10'], '附件.csv')
  let saved: UploadStatus | null = null, posts = 0, reads = 0
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { posts++; saved = committed(binding(path)); return response(saved) }
    reads++; return response(saved)
  }))
  const first = mount(true, uploaded, true)
  expect(screen.queryByRole('button', { name: '上传未提交资料' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [original] } })
  await waitFor(() => expect(uploaded).toHaveBeenCalledTimes(1))
  expect(posts).toBe(1)
  expect(screen.getByRole('list', { name: '上传文件列表' })).toHaveTextContent('附件.csv')
  expect(screen.getByText('已上传')).toBeVisible()
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
  expect(screen.queryByText(/成功 1 · 失败/)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '清除已完成上传记录' })).not.toBeInTheDocument()
  first.unmount(); uploaded.mockClear(); mount(true, uploaded, true)
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [original] } })
  await waitFor(() => expect(uploaded).toHaveBeenCalledWith(saved!.receipt!.asset.asset_id))
  expect(posts).toBe(1)
  expect(reads).toBe(1)
}, 15000)

test('completed upload history stays folded while unfinished files remain visible and are never restarted on load', () => {
  const previous: UploadBinding = { schema_version: '1.0', workspace_id: 'wUpload', request_id: 'rPrevious', name: '历史附件.csv', size_bytes: 20, sha256: 'a'.repeat(64) }
  const pending = { ...previous, request_id: 'rPending', name: '待恢复附件.csv' }
  localStorage.setItem('clear-upload-queue:wUpload', JSON.stringify([
    { binding: previous, state: 'committed', receipt: committed(previous).receipt },
    { binding: pending, state: 'uncertain', message: '请核实上次上传' },
  ]))
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  mount(true, undefined, true)
  expect(screen.getByText('历史附件.csv')).not.toBeVisible()
  expect(screen.getByText('待恢复附件.csv')).toBeVisible()
  expect(screen.getByRole('button', { name: '核实并继续上传' })).toBeEnabled()
  expect(fetch).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('已完成的上传记录（1）'))
  expect(screen.getByText('历史附件.csv')).toBeVisible()
  expect(fetch).not.toHaveBeenCalled()
})

test('automatic upload preserves an unknown request and only deliberate recovery consumes its original receipt', async () => {
  const uploaded = vi.fn(), original = new File(['收入\n01.10'], '自动附件.csv')
  let saved: UploadStatus | null = null, posts = 0, reads = 0, available = false
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { posts++; saved = committed(binding(path)); throw new Error('上传回包未知') }
    reads++
    if (!available) throw new Error('原回执暂时不可读')
    return response(saved)
  }))
  mount(true, uploaded, true)
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [original] } })
  await screen.findByText('上传回包未知')
  const pending = JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)[0]
  expect(pending.state).toBe('uncertain')
  fireEvent.change(screen.getByLabelText('添加审计资料'), { target: { files: [original] } })
  await waitFor(() => expect(screen.getByRole('button', { name: '核实并继续上传' })).toBeEnabled())
  expect(posts).toBe(1); expect(reads).toBe(1)
  available = true
  fireEvent.click(screen.getByRole('button', { name: '核实并继续上传' }))
  await waitFor(() => expect(uploaded).toHaveBeenCalledTimes(1))
  expect(posts).toBe(1)
  expect(reads).toBe(2)
  expect(JSON.parse(localStorage.getItem('clear-upload-queue:wUpload')!)[0].binding).toEqual(pending.binding)
}, 15000)
