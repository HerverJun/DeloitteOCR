import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { beforeAll, beforeEach, afterEach, test, expect, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { MemoryRouter } from 'react-router-dom'
import { ScreenBatch } from '../src/features/ocr/ScreenBatch'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => { sessionStorage.clear(); vi.stubGlobal('crypto', { randomUUID }); const computed = window.getComputedStyle.bind(window); vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element)) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const ok = (data: unknown) => ({ ok: true, json: async () => data })
const item = { input_version_id: 'versionA', image_id: 'imageA', asset_id: 'assetA', name: '屏拍.png', status: 'needs_review', stage: 'review', result_id: 'resultA', result_revision: 3, issues: [{ code: 'critical_difference', message: '金额有差异' }] }
const batch = () => ({ batch_id: 'batchA', status: 'running', counts: { total: 240, completed: 230, needs_review: 1, failed: 1, cancelled: 0, pending: 8 }, items: [item], filtered_total: 240, capabilities: ['pause', 'cancel'] })
function mount(start = '/ocr?screen_batch=batchA') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<ConfigProvider><QueryClientProvider client={client}><MemoryRouter initialEntries={[start]}><ScreenBatch workspaceId="wtest" linkId="linkA" setId="setA" revision={1} count={240} ready /></MemoryRouter></QueryClientProvider></ConfigProvider>)
}

test('240-image progress can export confirmed files while outstanding inputs remain visible', async () => {
  const payloads: any[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/export')) { payloads.push(JSON.parse(String(init?.body))); return ok({ bundle_id: 'bundleA', files: [{ name: 'ocr-results.zip' }] }) }
    return ok(batch())
  }))
  mount()
  await screen.findByText('完成 230'); await screen.findByText('处理中 8')
  fireEvent.click(screen.getByRole('button', { name: '下载逐图 Excel ZIP' }))
  const link = await screen.findByRole('link', { name: '下载 Excel ZIP' })
  expect(link).toHaveAttribute('href', '/api/platform/v1/workspaces/wtest/artifacts/bundleA/files/ocr-results.zip')
  expect(payloads).toEqual([{ include_pending_review: false }])
  expect(screen.getByText('处理中 8')).toBeInTheDocument()
})

test('stage estimate is bounded to the current queue and remains unknown when paused or unsupported', async () => {
  const eta = { remaining_seconds: 125, stage: { label: 'Hunyuan 首轮识别' }, sample_count: 5, scope: 'current_queued_stage', reason: 'estimated', not_included: ['future_upgrades', 'export', 'human_review'] }
  let value = { ...batch(), eta }
  vi.stubGlobal('fetch', vi.fn(async () => ok(value)))
  mount()
  await screen.findByText(/当前阶段预计还需约 3 分钟（Hunyuan 首轮识别）/)
  expect(screen.getByText(/导出和人工复核另计/)).toBeInTheDocument()
  value = { ...batch(), status: 'paused', eta }
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  await screen.findByText(/已暂停，继续后更新当前阶段估计/)
  expect(screen.queryByText(/预计还需约/)).not.toBeInTheDocument()
  value = { ...batch(), eta: { ...eta, scope: 'whole_batch' } }
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  await screen.findByText(/当前阶段剩余时间暂无法估计/)
  expect(screen.queryByText(/预计还需约/)).not.toBeInTheDocument()
})

test('lost submission response preserves the exact request identity on retry', async () => {
  const submitted: any[] = []
  vi.stubGlobal('fetch', vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { submitted.push(JSON.parse(String(init.body))); if (submitted.length === 1) throw new Error('回包丢失') }
    return ok(batch())
  }))
  mount('/ocr')
  fireEvent.click(screen.getByRole('button', { name: '开始整批转 Excel' }))
  await screen.findByText('回包丢失')
  fireEvent.click(screen.getByRole('button', { name: '开始整批转 Excel' }))
  await screen.findByText('完成 230')
  expect(submitted).toHaveLength(2)
  expect(submitted[1]).toEqual(submitted[0])
})

test('review keeps original/crop selector aligned and writes against the pinned result revision', async () => {
  const actions: any[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/items/versionA')) return ok({ ...item, original_url: '/original.png', tables: [{ columns: 2, cells: [{ row: 0, column: 0, text: '编号' }, { row: 1, column: 0, text: '000001' }, { row: 1, column: 1, text: '01.10' }] }], preparation: { prepared_images: [{ id: 'cropA', kind: 'table', url: '/crop.png' }] } })
    if (path.endsWith('/action')) { actions.push(JSON.parse(String(init?.body))); return ok(batch()) }
    return ok(batch())
  }))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '核对原图与结果' }))
  const image = await screen.findByRole('img', { name: '屏拍.png' })
  expect(image).toHaveAttribute('src', '/original.png')
  fireEvent.change(screen.getByLabelText('表1行2列2'), { target: { value: '01.20' } })
  fireEvent.click(screen.getByRole('button', { name: '保存并确认本图' }))
  await waitFor(() => expect(actions).toHaveLength(1))
  expect(actions[0]).toMatchObject({ action: 'review', version_ids: ['versionA'], result_id: 'resultA', expected_revision: 3, state: 'accepted', edits: [{ table_index: 0, row: 1, column: 1, before: '01.10', after: '01.20' }] })
})

test('candidate review preserves the old result guard and confirmed numeric columns are scoped to one image and table', async () => {
  const actions: any[] = [], exports: any[] = []
  let accepted = false
  const tables = (value: string) => [{ columns: 2, cells: [{ row: 1, column: 0, text: '000001' }, { row: 1, column: 1, text: value }] }]
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/items/versionA')) return ok({ ...item, review_state: accepted ? 'accepted' : 'automatic', original_url: '/original.png', tables: tables(accepted ? '01.30' : '01.10'), candidates: [{ result_id: 'candidateB', engine: 'hunyuan', tables: tables('01.20') }] })
    if (path.endsWith('/action')) { actions.push(JSON.parse(String(init?.body))); accepted = true; return ok(batch()) }
    if (path.endsWith('/export')) { exports.push(JSON.parse(String(init?.body))); return ok({ bundle_id: 'bundleA', files: [{ name: 'ocr-results.zip' }] }) }
    return ok(batch())
  }))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '核对原图与结果' }))
  await screen.findByLabelText('复核候选结果')
  expect(screen.getByLabelText('表1已确认数值列')).toBeDisabled()
  fireEvent.mouseDown(screen.getByLabelText('复核候选结果'))
  fireEvent.click(await screen.findByText('候选 1 · hunyuan'))
  await waitFor(() => expect(screen.getByLabelText('表1行2列2')).toHaveValue('01.20'))
  fireEvent.change(screen.getByLabelText('表1行2列2'), { target: { value: '01.30' } })
  fireEvent.click(screen.getByRole('button', { name: '保存并确认本图' }))
  await waitFor(() => expect(actions).toHaveLength(1))
  expect(actions[0]).toMatchObject({ result_id: 'resultA', expected_revision: 3, candidate_id: 'candidateB', edits: [{ before: '01.20', after: '01.30', table_index: 0, row: 1, column: 1 }] })
  await waitFor(() => expect(screen.queryByRole('button', { name: '保存并确认本图' })).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: '核对原图与结果' }))
  await waitFor(() => expect(screen.getByLabelText('表1已确认数值列')).toBeEnabled())
  fireEvent.mouseDown(screen.getByLabelText('表1已确认数值列'))
  fireEvent.click(await screen.findByText('第 2 列'))
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: '保留当前结果' })).not.toBeInTheDocument())
  expect(actions).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '下载逐图 Excel ZIP' }))
  await waitFor(() => expect(exports).toHaveLength(1))
  expect(exports[0]).toEqual({ include_pending_review: false, numeric_columns: { versionA: { '0': [1] } } })
}, 30000)
