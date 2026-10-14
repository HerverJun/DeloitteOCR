import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { ResultView } from '../src/features/multimodal/MultimodalWorkbench'

vi.mock('../src/App', () => ({ useWorkspaceId: () => 'wReview', Workspaces: () => null }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear() })

test('audio corrections retain global row identities across pages and reopening', async () => {
  vi.stubGlobal('crypto', { randomUUID })
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
  const rows = Array.from({ length: 65 }, (_, index) => ({ row_id: `rSegment${index}`, review_index: index,
    start_ms: index * 1000, end_ms: (index + 1) * 1000, text: `原文 ${index + 1}`, speaker: '说话人 1' }))
  const item = { job_id: 'mReview', kind: 'audio' as const, status: 'succeeded', stage: 'completed', revision: 1,
    error: null, result_hash: 'a'.repeat(64), inputs: [{ original_name: '公开录音.wav', asset_id: 'aAudio', asset_revision: 1 }],
    result: { outcome: 'transcribed', algorithm: '工程测试', device: 'cpu', elapsed_seconds: 1, limitations: [],
      segments: rows, audio: { duration_ms: 65000, original_channels: 1, original_sample_rate: 16000 } }, created_at: '', review: null }
  let payload: { edits: { row_id: string; index: number; text: string }[] } | undefined
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    payload = JSON.parse(init!.body as string)
    return { ok: true, json: async () => ({ ...item, revision: 2, review: payload }) }
  }))
  const mount = (value: typeof item) => render(<ConfigProvider><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><ResultView item={value} workspaceId="wReview" refresh={() => {}} referenceName="" setReferenceName={() => {}} onReference={() => {}} referenceBusy={false} /></MemoryRouter>
  </QueryClientProvider></ConfigProvider>)
  mount(item)
  fireEvent.change(screen.getByLabelText('第1段转写'), { target: { value: '第一页校订' } })
  fireEvent.click(screen.getByTitle('2'))
  fireEvent.change(await screen.findByLabelText('第21段转写'), { target: { value: '第二页校订' } })
  fireEvent.click(screen.getByTitle('4'))
  fireEvent.change(await screen.findByLabelText('第65段转写'), { target: { value: '末页校订' } })
  const actor = screen.getByText('复核人').closest('label')!.querySelector('input')!
  fireEvent.change(actor, { target: { value: '工程复核员' } })
  fireEvent.change(screen.getByPlaceholderText('填写复核依据、修改说明或待查事项'), { target: { value: '跨页保存验证' } })
  fireEvent.click(screen.getByRole('button', { name: '保存复核与校正' }))
  await waitFor(() => expect(payload?.edits).toHaveLength(3))
  expect(payload!.edits.map(edit => [edit.row_id, edit.index, edit.text])).toEqual([
    ['rSegment0', 0, '第一页校订'], ['rSegment20', 20, '第二页校订'], ['rSegment64', 64, '末页校订'],
  ])
  cleanup()
  mount({ ...item, revision: 2, review: { ...payload, actor: '工程复核员', note: '跨页保存验证', assessment: 'inconclusive' } } as typeof item)
  expect(screen.getByLabelText('第1段转写')).toHaveValue('第一页校订')
  fireEvent.click(screen.getByTitle('2'))
  expect(await screen.findByLabelText('第21段转写')).toHaveValue('第二页校订')
  expect(screen.getByLabelText('第22段转写')).toHaveValue('原文 22')
}, 30000)
