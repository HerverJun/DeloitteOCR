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

test('business metadata retains both readings and submits selected source with cell edits and current review token', async () => {
  const actions: any[] = []
  const token = 'a'.repeat(64)
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/items/versionA')) return ok({ ...item, review_binding_token: token, original_url: '/original.png',
      tables: [{ columns: 1, cells: [{ row: 0, column: 0, text: '001.20' }] }],
      business_metadata: [{ kind: 'note', text: '表注：金额+20', source: 'primary_transcription', table: 0,
        alternative_text: '表注：金额-20', alternative_source: 'rapidocr', review_required: true, human_confirmed: false,
        transcription_evidence: { primary_text: '表注：金额+20', auxiliary_text: '表注：金额-20',
          primary_source: { result_id: 'nativeA', image_version: 'versionA' }, auxiliary_source: { source: 'rapidocr', evidence_refs: [7] } } }] })
    if (path.endsWith('/action')) { actions.push(JSON.parse(String(init?.body))); return ok(batch()) }
    return ok(batch())
  }))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '核对原图与结果' }))
  await screen.findByText('当前文字：表注：金额+20')
  expect(screen.getByText('另一识别原文：表注：金额-20（rapidocr）')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '选用辅助识别原文 1' }))
  expect(screen.getByLabelText('业务说明1复核值')).toHaveValue('表注：金额-20')
  fireEvent.change(screen.getByLabelText('表1行1列1'), { target: { value: '001.30' } })
  fireEvent.click(screen.getByRole('button', { name: '保存并确认本图' }))
  await waitFor(() => expect(actions).toHaveLength(1))
  expect(actions[0]).toMatchObject({ expected_revision: 3, expected_review_token: token,
    metadata_edits: [{ item_index: 0, before: '表注：金额+20', after: '表注：金额-20', choice: 'auxiliary' }],
    edits: [{ table_index: 0, row: 0, column: 0, before: '001.20', after: '001.30' }] })
}, 30000)

test('manual business metadata remains pending until explicit acceptance', async () => {
  const actions: any[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/items/versionA')) return ok({ ...item, original_url: '/original.png', tables: [],
      business_metadata: [{ kind: 'unit', text: '单位：元', source: 'rapidocr', review_required: true }] })
    if (path.endsWith('/action')) { actions.push(JSON.parse(String(init?.body))); return ok(batch()) }
    return ok(batch())
  }))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '核对原图与结果' }))
  fireEvent.change(await screen.findByLabelText('业务说明1复核值'), { target: { value: '单位：万元' } })
  fireEvent.click(screen.getByRole('button', { name: '保留当前结果' }))
  await waitFor(() => expect(actions).toHaveLength(1))
  expect(actions[0]).toMatchObject({ state: 'retain', metadata_edits: [{ item_index: 0, before: '单位：元', after: '单位：万元', choice: 'manual' }] })
}, 30000)

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

test('reopening review binds refreshed detail before a new metadata draft', async () => {
  const actions: any[] = []
  let revision = 3
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/items/versionA')) {
      await new Promise(resolve => setTimeout(resolve, 10))
      return ok({ ...item, current_result_revision: revision, review_binding_token: String(revision).repeat(64), original_url: '/original.png',
        business_metadata: [{ kind: 'note', text: `表注：版本${revision}`, source: 'human_manual' }], tables: [] })
    }
    if (path.endsWith('/action')) { actions.push(JSON.parse(String(init?.body))); revision++; return ok(batch()) }
    return ok(batch())
  }))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '核对原图与结果' }))
  await screen.findByLabelText('业务说明1复核值')
  fireEvent.click(screen.getByRole('button', { name: '保存并确认本图' }))
  await waitFor(() => expect(actions).toHaveLength(1))
  await waitFor(() => expect(screen.queryByRole('button', { name: '保留当前结果' })).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: '核对原图与结果' }))
  await waitFor(() => expect(screen.getByLabelText('业务说明1复核值')).toHaveValue('表注：版本4'))
  await waitFor(() => expect(screen.getByRole('button', { name: '保留当前结果' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('业务说明1复核值'), { target: { value: '表注：人工复核' } })
  fireEvent.click(screen.getByRole('button', { name: '保留当前结果' }))
  await waitFor(() => expect(actions).toHaveLength(2))
  expect(actions[1]).toMatchObject({ expected_revision: 4, expected_review_token: '4'.repeat(64), state: 'retain',
    metadata_edits: [{ item_index: 0, before: '表注：版本4', after: '表注：人工复核', choice: 'manual' }] })
}, 30000)

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

test('external editing uses the actual revision for comparison and review while numeric consent remains stale', async () => {
  const comparisons: any[] = [], actions: any[] = []
  const tables = [{ columns: 2, cells: [{ row: 1, column: 0, text: '000001' }, { row: 1, column: 1, text: '01.20' }] }]
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/items/versionA')) return ok({ ...item, review_state: 'accepted', human_confirmed: false, review_stale: true,
      current_result_revision: 4, result: { revision: 4, tables }, original_url: '/original.png', tables,
      candidates: [{ result_id: 'candidateB', engine: 'glm', tables }] })
    if (path.endsWith('/compare')) {
      comparisons.push(JSON.parse(String(init?.body)))
      return ok({ result_id: 'resultA', result_revision: 4, input_version_id: 'versionA', current_id: 'resultA', alternative_id: 'candidateB', recommended_candidate_id: 'resultA',
        conflicts: [{ code: 'candidate_value_conflict' }], selection_applied: false, values_modified: false })
    }
    if (path.endsWith('/action')) { actions.push(JSON.parse(String(init?.body))); return ok(batch()) }
    return ok(batch())
  }))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '核对原图与结果' }))
  await screen.findByLabelText('复核候选结果')
  expect(screen.getByLabelText('表1已确认数值列')).toBeDisabled()
  fireEvent.mouseDown(screen.getByLabelText('复核候选结果'))
  fireEvent.click(await screen.findByText('候选 1 · glm'))
  fireEvent.click(screen.getByRole('button', { name: '比较候选差异' }))
  await screen.findByText(/候选比较发现 1 项分歧/)
  expect(comparisons).toEqual([{ result_id: 'resultA', expected_revision: 4, candidate_id: 'candidateB' }])
  expect(actions).toHaveLength(0)
  expect(screen.getByLabelText('表1已确认数值列')).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '保存并确认本图' }))
  await waitFor(() => expect(actions).toHaveLength(1))
  expect(actions[0]).toMatchObject({ action: 'review', result_id: 'resultA', expected_revision: 4, state: 'accepted' })
}, 30000)
