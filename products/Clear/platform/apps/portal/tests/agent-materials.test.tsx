import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Materials } from '../src/features/agent/Materials'

const uploadCallbacks = vi.hoisted(() => ({ current: null as null | { onBusy?: (busy: boolean) => void; onUploaded?: (id: string) => void } }))
vi.mock('../src/features/batches/UploadQueue', () => ({
  UploadQueue: ({ disabled, onBusy, onUploaded }: { disabled?: boolean; onBusy?: (busy: boolean) => void; onUploaded?: (id: string) => void }) => {
    uploadCallbacks.current = { onBusy, onUploaded }
    return <div>
    <input type="file" aria-label="上传测试文件" disabled={disabled} onChange={() => onBusy?.(true)} />
    <button disabled={disabled} onClick={() => { onUploaded?.('uploaded'); onBusy?.(false) }}>完成上传</button>
  </div> },
}))
beforeAll(() => {
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mount(save = vi.fn(async (_ids: string[]) => {}), selected: string[] = [], respond?: (url: string) => unknown) {
  const close = vi.fn(), requests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    requests.push(url)
    return { ok: true, json: async () => respond ? respond(url) : ({ items: [{ asset_id: 'existing', original_name: '付款台账.xlsx', size_bytes: 2048, revision: 7 }], total: 1 }) }
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const element = (open: boolean) => <QueryClientProvider client={client}><Materials open={open} workspace="w" selected={selected} close={close} save={save} /></QueryClientProvider>
  const view = render(element(true))
  return { save, close, requests, rerender: (open: boolean) => view.rerender(element(open)) }
}

test('adding files starts at upload and only committed uploads can be added to the conversation', async () => {
  const { save, close, requests } = mount()
  expect(screen.getByRole('tab', { name: '上传文件' })).toHaveAttribute('aria-selected', 'true')
  expect(requests).toEqual([])
  expect(screen.queryByRole('button', { name: '整批转 Excel' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /全部图片/ })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '加入对话' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('上传测试文件'), { target: { files: [new File(['file'], '测试.pdf')] } })
  expect(screen.getByRole('button', { name: '加入对话' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '完成上传' }))
  expect(screen.getByRole('status')).toHaveTextContent('已选择 1 份文件')
  expect(screen.getByRole('button', { name: '加入对话' })).toBeEnabled()
  fireEvent.change(screen.getByLabelText('上传测试文件'), { target: { files: [new File(['next'], '下一份.pdf')] } })
  expect(screen.getByRole('button', { name: '加入对话' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '完成上传' }))
  fireEvent.click(screen.getByRole('button', { name: '加入对话' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(['uploaded']))
  expect(close).toHaveBeenCalledTimes(1)
  expect(requests.every(url => url.includes('/assets?'))).toBe(true)
})

test('existing file selection can be cancelled without changing the conversation', async () => {
  const { save, close, rerender, requests } = mount()
  expect(requests).toEqual([])
  fireEvent.click(screen.getByRole('tab', { name: '已有文件' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /付款台账.xlsx/ }))
  expect(requests).toHaveLength(1)
  expect(screen.getByRole('status')).toHaveTextContent('已选择 1 份文件')
  expect(screen.queryByText('v7')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /^取\s*消$/ }))
  expect(close).toHaveBeenCalledTimes(1)
  expect(save).not.toHaveBeenCalled()
  rerender(false); rerender(true)
  expect(screen.getByRole('status')).toHaveTextContent('尚未选择文件')
  expect(screen.getByRole('tab', { name: '上传文件' })).toHaveAttribute('aria-selected', 'true')
})

test('a failed scope save preserves the selection and permits a deliberate retry', async () => {
  const save = vi.fn(async (_ids: string[]) => { throw new Error('资料范围已改变，请重新核对') })
  const { close } = mount(save)
  fireEvent.click(screen.getByRole('tab', { name: '已有文件' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /付款台账.xlsx/ }))
  fireEvent.click(screen.getByRole('button', { name: '加入对话' }))
  expect(await screen.findByText('资料范围已改变，请重新核对')).toBeVisible()
  expect(close).not.toHaveBeenCalled()
  expect(screen.getByRole('checkbox', { name: /付款台账.xlsx/ })).toBeChecked()
  save.mockImplementationOnce(async () => {})
  await waitFor(() => expect(screen.getByRole('button', { name: '加入对话' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '加入对话' }))
  await waitFor(() => expect(close).toHaveBeenCalledTimes(1))
  expect(save.mock.calls).toEqual([[['existing']], [['existing']]])
})

test('a late save completion cannot close a newly opened file picker', async () => {
  let finish!: () => void
  const save = vi.fn((_ids: string[]) => new Promise<void>(resolve => { finish = resolve }))
  const { close, rerender } = mount(save, ['existing'])
  fireEvent.click(screen.getByRole('button', { name: '加入对话' }))
  rerender(false); rerender(true)
  await act(async () => finish())
  expect(close).not.toHaveBeenCalled()
  await waitFor(() => expect(screen.getByRole('dialog', { name: '添加文件' })).toBeVisible())
  expect(screen.getByRole('button', { name: '加入对话' })).toBeEnabled()
})

test('closing an upload picker prevents a late receipt from changing a reopened selection', async () => {
  const { rerender } = mount()
  const previous = uploadCallbacks.current!
  rerender(false); rerender(true)
  await act(async () => { previous.onUploaded?.('late-upload'); previous.onBusy?.(false) })
  expect(screen.getByRole('status')).toHaveTextContent('尚未选择文件')
  expect(screen.getByRole('button', { name: '加入对话' })).toBeDisabled()
})

test('existing files load one server page at a time and keyword search reaches files beyond 1000 while preserving selections', async () => {
  const asset = (id: string, name: string) => ({ asset_id: id, original_name: name, size_bytes: 2048, revision: 1 })
  const targetKeyword = '第1201份 & 付款'
  const { save, requests } = mount(undefined, ['already-selected'], raw => {
    const url = new URL(raw, 'http://local')
    if (url.searchParams.get('keyword')) return { items: [asset('later-match', '第1201份付款.xlsx')], total: 1 }
    return { items: [Number(url.searchParams.get('offset')) ? asset('second-page', '第二页资料.xlsx') : asset('first-page', '第一页资料.xlsx')], total: 1250 }
  })
  fireEvent.click(screen.getByRole('tab', { name: '已有文件' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /第一页资料/ }))
  expect(requests).toHaveLength(1)
  expect(new URL(requests[0], 'http://local').searchParams.get('limit')).toBe('50')
  fireEvent.click(screen.getByTitle('2'))
  fireEvent.click(await screen.findByRole('checkbox', { name: /第二页资料/ }))
  expect(new URL(requests[1], 'http://local').searchParams.get('offset')).toBe('50')
  fireEvent.change(screen.getByLabelText('搜索已有文件'), { target: { value: targetKeyword } })
  fireEvent.click(await screen.findByRole('checkbox', { name: /第1201份付款/ }))
  const search = new URL(requests.at(-1)!, 'http://local')
  expect(search.searchParams.get('keyword')).toBe(targetKeyword)
  expect(search.searchParams.get('offset')).toBe('0')
  expect(screen.getByRole('status')).toHaveTextContent('已选择 4 份文件')
  fireEvent.change(screen.getByLabelText('搜索已有文件'), { target: { value: '' } })
  expect(await screen.findByRole('checkbox', { name: /第一页资料/ })).toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: '加入对话' }))
  await waitFor(() => expect(save).toHaveBeenCalledWith(['already-selected', 'first-page', 'second-page', 'later-match']))
})
