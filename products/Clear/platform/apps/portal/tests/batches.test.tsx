import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'
import { type Batch, type BatchDisposition, type Issue, type IssuePage, type ReviewBundle, type Template } from '../src/features/batches/api'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  localStorage.clear()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const template: Template = { template_id: 'tMonthly', version: 1, name: '月度模板', scenario: 'data_compare', config: {}, config_hash: 'a'.repeat(64) }
const inputs = [{ asset_id: 'aLeft', original_name: '左账.csv', revision: 1, blob_hash: 'b'.repeat(64), media_type: 'text/csv' },
  { asset_id: 'aRight', original_name: '右账.csv', revision: 1, blob_hash: 'c'.repeat(64), media_type: 'text/csv' }]
const issue = (id: string, title: string): Issue => ({ item_id: id, title, category: 'different', detail: '金额需要解释', source: { name: '左账.csv', source_row: 2 }, raw_output: { left: '01.10', right: '1.20' }, disposition: 'pending', note: '' })
function batch(issues: Issue[] = [], revision = 1): Batch { return { batch_id: 'bMonthly', workspace_id: 'wMonthly', template, template_id: template.template_id, template_version: 1,
  revision, coverage: { index_complete: true, result_rows: 2, required_items: issues.length, indexed_items: issues.length, pending: issues.filter(item => item.disposition === 'pending').length, confirmed: issues.filter(item => item.disposition === 'confirmed').length, deferred: issues.filter(item => item.disposition === 'deferred').length }, delivery: { revision: null, bundle_id: null, status: 'none', kind: null },
  status: 'succeeded', stage: 'done', summary: { total: 2, processed: 2, succeeded: 2, failed: 0, pending: issues.filter(item => item.disposition === 'pending').length, confirmed: issues.filter(item => item.disposition === 'confirmed').length, deferred: issues.filter(item => item.disposition === 'deferred').length, coverage: { same_pairs: 1, different_pairs: 1, only_left: 0, only_right: 0, ambiguous_left_rows: 0, ambiguous_right_rows: 0 }, execution_complete: true, review_complete: issues.every(item => item.disposition === 'confirmed'), pending_operations: 0, pending_operation: null }, issues, receipts: {}, artifact_state: 'collected',
  artifacts: [{ bundle_id: 'bundleMonthly', title: '固定比对成果', files: [{ name: '差异.csv', availability: 'available' }] }], capabilities: [], created_at: '2026-10-02T02:00:00Z', updated_at: '2026-10-02T02:00:00Z' } }
const response = (value: unknown) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(value)) })
const failure = (status: number, detail: string) => ({ ok: false, status, json: async () => ({ detail }) })
function issuePage(records: Issue[], revision = 1): IssuePage { const value = batch(records, revision); return { items: records, total: records.length, revision, coverage: value.coverage, summary: value.summary } }
function reviewer(name = '张复核') { fireEvent.change(screen.getByRole('textbox', { name: '本机复核人' }), { target: { value: name } }) }
function shared(input: string) {
  if (input.includes('/workspaces?')) return response({ items: [{ workspace_id: 'wMonthly', name: '月度空间' }], next_cursor: null, snapshot_revision: 1 })
  if (input.includes('/batch-templates?')) return response({ items: [template] })
  if (input.includes('/assets?')) return response({ items: inputs, next_offset: null })
  if (input.endsWith('/data/sessions')) return response({ items: [] })
  if (input.includes('/history?')) return response({ schema_version: '1.0', batch_id: 'bMonthly', revision: 1, items: [], total: 0, offset: 0 })
  return null
}
function mount(url = '/batches?workspace=wMonthly') {
  const router = createMemoryRouter(routes, { initialEntries: [url] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const view = render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, view }
}
async function choose(label: string, option: string) {
  fireEvent.mouseDown(await screen.findByRole('combobox', { name: label }))
  fireEvent.click((await screen.findAllByText(option, { selector: '.ant-select-item-option-content' })).at(-1)!)
}

test('one start submits fixed identities after preflight and shows separate execution, review and downloadable artifacts', async () => {
  const calls: { path: string; init?: RequestInit }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    calls.push({ path, init })
    const common = shared(path)
    if (common) return common
    if (path.endsWith('/batches/preflight')) return response({ ok: true, issues: [] })
    if (path.endsWith('/batches') && init?.method === 'POST') return response(batch([issue('iDifference', '金额差异')]))
    if (path.includes('/batches/bMonthly/issues')) return response(issuePage([issue('iDifference', '金额差异')]))
    if (path.includes('/batches/bMonthly')) return response(batch([issue('iDifference', '金额差异')]))
    if (path.includes('/batches?')) return response({ items: [] })
    throw new Error(path)
  }))
  const { router } = mount()
  await choose('已确认的处理模板', '月度模板 · 两表清洗与比对 · v1')
  await choose('批次左表资料', '左账.csv · 版本 1')
  await choose('批次右表资料', '右账.csv · 版本 1')
  fireEvent.click(screen.getByRole('button', { name: '预检并启动一次' }))
  expect(await screen.findByText('复核尚未完成')).toBeInTheDocument()
  expect(screen.getByText('执行已结束')).toBeInTheDocument()
  expect(screen.getByLabelText('表格比对覆盖')).toHaveTextContent('相同 1 · 差异 1 · 仅左 0 · 仅右 0 · 重复键左 0 · 重复键右 0')
  expect(screen.getByRole('link', { name: '下载 差异.csv' })).toHaveAttribute('href', '/api/platform/v1/workspaces/wMonthly/artifacts/bundleMonthly/files/%E5%B7%AE%E5%BC%82.csv')
  expect(router.state.location.search).toContain('batch=bMonthly')
  const submitted = calls.filter(call => call.path.endsWith('/batches') && call.init?.method === 'POST')
  expect(submitted).toHaveLength(1)
  const payload = JSON.parse(submitted[0].init!.body as string)
  expect(payload.request_key).toMatch(/^r/)
  expect(payload.inputs).toEqual(inputs.map((asset, index) => ({ role: index === 0 ? 'left' : 'right', asset_id: asset.asset_id, asset_revision: asset.revision, blob_hash: asset.blob_hash })))
  expect(localStorage.getItem('clear-batch-request:wMonthly')).toBeNull()
  expect(calls.some(call => call.path.includes('/actions') && call.init?.method === 'POST')).toBe(false)
}, 20000)

test('after a lost response and page reload the original request is queried without another submission', async () => {
  const calls: { path: string; init?: RequestInit }[] = []
  let reloaded = false
  let originalKey = ''
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    calls.push({ path, init })
    const common = shared(path)
    if (common) return common
    if (path.endsWith('/batches/preflight')) return response({ ok: true, issues: [] })
    if (path.endsWith('/batches') && init?.method === 'POST') { originalKey = JSON.parse(init.body as string).request_key; throw new Error('提交响应丢失') }
    if (path.includes('request_key=')) { if (!reloaded) throw new Error('连接暂不可用'); return response({ items: [batch()] }) }
    if (path.includes('/batches/bMonthly/issues')) return response(issuePage([]))
    if (path.includes('/batches/bMonthly')) return response(batch())
    if (path.includes('/batches?')) return response({ items: [] })
    throw new Error(path)
  }))
  const first = mount()
  await choose('已确认的处理模板', '月度模板 · 两表清洗与比对 · v1')
  await choose('批次左表资料', '左账.csv · 版本 1')
  await choose('批次右表资料', '右账.csv · 版本 1')
  fireEvent.click(screen.getByRole('button', { name: '预检并启动一次' }))
  await screen.findByText('连接暂不可用')
  expect(JSON.parse(localStorage.getItem('clear-batch-request:wMonthly')!).request_key).toBe(originalKey)
  first.view.unmount()
  reloaded = true
  mount()
  expect(await screen.findByText('原请求状态需要核实')).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: '已确认的处理模板' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '核实原请求并继续' }))
  expect(await screen.findByText('执行已结束')).toBeInTheDocument()
  expect(calls.some(call => call.path.includes(`request_key=${originalKey}`))).toBe(true)
  expect(calls.filter(call => call.path.endsWith('/batches') && call.init?.method === 'POST')).toHaveLength(1)
  expect(localStorage.getItem('clear-batch-request:wMonthly')).toBeNull()
}, 20000)

test('continuous review preserves the filter and bulk review sends only the explicitly selected items', async () => {
  const records = [issue('iFirst', '差异一'), issue('iSecond', '差异二'), issue('iThird', '差异三')]
  const saved: BatchDisposition[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path)
    if (common) return common
    if (path.endsWith('/dispositions')) {
      const payload = JSON.parse(init!.body as string)
      saved.push(payload)
      records.filter(item => payload.item_ids.includes(item.item_id)).forEach(item => { item.disposition = payload.action === 'defer' ? 'deferred' : 'confirmed'; item.note = payload.note })
      return response(batch(records, saved.length + 1))
    }
    if (path.includes('/issues')) return response(issuePage(records.filter(item => item.disposition === 'pending'), saved.length + 1))
    if (path.includes('/batches/bMonthly')) return response(batch(records, saved.length + 1))
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByRole('heading', { name: '差异一' })
  expect(screen.getByRole('button', { name: '确认并下一项' })).toBeDisabled()
  reviewer()
  fireEvent.change(screen.getByRole('textbox', { name: '复核说明' }), { target: { value: '已核对原账' } })
  fireEvent.click(screen.getByRole('button', { name: '确认并下一项' }))
  expect(await screen.findByRole('heading', { name: '差异二' })).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: '复核状态筛选' })).toBeInTheDocument()
  expect(saved[0]).toMatchObject({ item_ids: ['iFirst'], action: 'confirm', note: '已核对原账', expected_revision: 1, actor: { display_name: '张复核', source: 'local' } })
  expect(saved[0].request_id).toMatch(/^r[a-f0-9]{32}$/)
  fireEvent.click(screen.getAllByRole('checkbox')[1])
  expect(screen.getByText('本页已明确选中 1 项')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '确认选中 1 项' }))
  await waitFor(() => expect(saved).toHaveLength(2))
  expect(saved[1].item_ids).toEqual(['iSecond'])
  expect(saved[1].expected_revision).toBe(2)
  expect(saved[1].request_id).not.toBe(saved[0].request_id)
  expect(await screen.findByRole('heading', { name: '差异三' })).toBeInTheDocument()
  expect(await screen.findByText('已确认 2')).toBeInTheDocument()
}, 20000)

test('OCR template configuration reports review-only capabilities and does not offer a recognition start', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    const common = shared(path)
    if (common) return common
    if (path.includes('/batches?')) return response({ items: [] })
    if (path.includes('/ocr/projects')) return response({ items: [{ project_id: 'pOcr', name: '已有文档', link_id: 'lOcr' }] })
    if (path.includes('/batch-templates/options')) return response({ recognition: false, reason: '实例处于仅校对模式', engines: [] })
    throw new Error(path)
  }))
  mount()
  fireEvent.click(await screen.findByText('首次配置或新增模板'))
  await choose('模板处理场景', '文档识别与提取')
  await choose('模板原生项目', '已有文档 · 已关联')
  expect(await screen.findByText('当前能力不足，不能自动识别')).toBeInTheDocument()
  expect(screen.getByText('实例处于仅校对模式')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '确认并保存模板' })).toBeDisabled()
  expect(screen.queryByRole('combobox', { name: '模板识别引擎' })).not.toBeInTheDocument()
}, 20000)

test('409 keeps the explicit selection and note; replay uses the entire original payload unchanged', async () => {
  const record = issue('iConflict', '需核对金额')
  const saved: BatchDisposition[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (path.endsWith('/dispositions')) {
      saved.push(JSON.parse(init!.body as string))
      if (saved.length === 1) return failure(409, '所见版本冲突')
      record.disposition = 'confirmed'; return response(batch([record], 5))
    }
    if (path.includes('/issues')) return response(issuePage([record], 4))
    if (path.includes('/batches/bMonthly')) return response(batch([record], 4))
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByRole('heading', { name: '需核对金额' }); reviewer()
  fireEvent.click(screen.getAllByRole('checkbox')[1])
  fireEvent.change(screen.getByRole('textbox', { name: '复核说明' }), { target: { value: '原账凭证已核对' } })
  fireEvent.click(screen.getByRole('button', { name: '确认选中 1 项' }))
  await screen.findByText('所见版本冲突，说明和原请求已保留')
  expect(screen.getByRole('textbox', { name: '复核说明' })).toHaveValue('原账凭证已核对')
  expect(screen.getByText('本页已明确选中 1 项')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '确认选中 1 项' })).toBeDisabled()
  expect(screen.getByRole('textbox', { name: '复核说明' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '按原请求核实并重放' }))
  await waitFor(() => expect(saved).toHaveLength(2))
  expect(saved[1]).toEqual(saved[0])
  expect(saved[1].expected_revision).toBe(4)
  expect(saved[1].note).toBe('原账凭证已核对')
  await waitFor(() => expect(localStorage.getItem('clear-batch-disposition:bMonthly')).toBeNull())
}, 20000)

test('a new CAS request requires an explicit refresh after conflict and preserves the draft note', async () => {
  const record = issue('iFresh', '新版本金额')
  const saved: BatchDisposition[] = []; let revision = 6
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (path.endsWith('/dispositions')) {
      saved.push(JSON.parse(init!.body as string))
      if (saved.length === 1) { revision = 7; return failure(409, '处置版本冲突，请重新检查') }
      record.disposition = 'confirmed'; revision = 8; return response(batch([record], revision))
    }
    if (path.includes('/issues')) return response(issuePage([record], revision))
    if (path.includes('/batches/bMonthly')) return response(batch([record], revision))
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByRole('heading', { name: '新版本金额' }); reviewer()
  fireEvent.change(screen.getByRole('textbox', { name: '复核说明' }), { target: { value: '待再次核对的说明' } })
  fireEvent.click(screen.getByRole('button', { name: '确认并下一项' }))
  await screen.findByText('所见版本冲突，说明和原请求已保留')
  expect(saved).toHaveLength(1)
  expect(screen.getByRole('button', { name: '确认并下一项' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '刷新核对并结束冲突请求' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '确认并下一项' })).toBeEnabled())
  expect(screen.getByRole('textbox', { name: '复核说明' })).toHaveValue('待再次核对的说明')
  fireEvent.click(screen.getByRole('button', { name: '确认并下一项' }))
  await waitFor(() => expect(saved).toHaveLength(2))
  expect(saved[1].expected_revision).toBe(7)
  expect(saved[1].request_id).not.toBe(saved[0].request_id)
  expect(saved[1].note).toBe(saved[0].note)
}, 20000)

test('lost disposition response survives reload and recovers the original operation with an empty body and no new disposition', async () => {
  const record = issue('iUnknown', '原操作待核实事项')
  const writes: { path: string; body: unknown }[] = []; let payload: BatchDisposition; let unknown = false; let recovered = false
  function latest() { const value = batch([record], recovered ? 2 : 1); if (unknown && !recovered) {
    value.summary.pending_operations = 1; value.summary.pending_operation = { operation_id: 'opOriginal', request_id: payload.request_id, state: 'needs_verification' }; value.summary.review_complete = false
  } return value }
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (init?.method === 'POST') writes.push({ path, body: JSON.parse(init.body as string) })
    if (path.endsWith('/dispositions')) { payload = JSON.parse(init!.body as string); unknown = true; throw new Error('响应连接中断') }
    if (path.endsWith('/operations/opOriginal/recover')) { recovered = true; record.disposition = 'confirmed'; return response(latest()) }
    if (path.endsWith('/operations/opOriginal')) return response({ schema_version: '1.0', operation_id: 'opOriginal', batch_id: 'bMonthly', request_id: payload.request_id, payload_hash: 'd'.repeat(64), expected_revision: 1, actor: payload.actor, state: recovered ? 'committed' : 'needs_verification', receipt: {}, created_at: '2026-10-03T01:00:00Z', updated_at: '2026-10-03T01:00:00Z' })
    if (path.includes('/issues')) return response(issuePage([record], recovered ? 2 : 1))
    if (path.includes('/batches/bMonthly')) return response(latest())
    throw new Error(path)
  }))
  const first = mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByRole('heading', { name: '原操作待核实事项' }); reviewer('原复核员')
  fireEvent.change(screen.getByRole('textbox', { name: '复核说明' }), { target: { value: '恢复时须沿用此说明' } })
  fireEvent.click(screen.getByRole('button', { name: '确认并下一项' }))
  await screen.findByText('原操作待核实'); first.view.unmount()
  mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByText('原操作待核实')
  expect(await screen.findByRole('textbox', { name: '复核说明' })).toHaveValue('恢复时须沿用此说明')
  expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: '按原请求核实并重放' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '恢复原操作' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeEnabled())
  expect(writes.filter(item => item.path.endsWith('/dispositions'))).toHaveLength(1)
  expect(writes.find(item => item.path.endsWith('/recover'))?.body).toEqual({})
  expect(localStorage.getItem('clear-batch-disposition:bMonthly')).toBeNull()
}, 20000)

test.each(['classification', 'ocr'] as const)('%s requires explicit adoption of the read native revision and shows identified history', async scenario => {
  const record: Issue = { ...issue('iAdopt', '固定原始建议'), category: 'review', raw_output: scenario === 'classification' ? { prediction_label_id: 'A', ordinal: 0 } : { text: '原始 OCR 文本', ordinal: 0 } }
  const saved: BatchDisposition[] = []; let nativeRevision = 1
  const value = () => ({ ...batch([record], saved.length ? 9 : 8), template: { ...template, scenario, config: { link_id: 'lNative' } } })
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/history?')) return response({ schema_version: '1.0', batch_id: 'bMonthly', revision: 9, total: saved.length, offset: 0, items: saved.length ? [{ event_id: 'eAdopt', batch_id: 'bMonthly', item_id: record.item_id, operation_id: 'opAdopt', revision: 9, action: 'adopt', note: '采用明确的人工修订', actor: saved[0].actor, previous_state: { disposition: 'pending' }, current_state: { disposition: 'confirmed' }, adoption: saved[0].adoption, legacy: false, created_at: '2026-10-03T02:00:00Z' }] : [] })
    const common = shared(path); if (common) return common
    if (path.endsWith('/projects')) return response({ items: [{ project_id: 'pNative', link_id: 'lNative' }] })
    if (path.includes('/native-revisions')) return response({ schema_version: '1.0', batch_id: 'bMonthly', item_id: record.item_id, revision: value().revision, scenario, original: record.raw_output, source: record.source, adoption: record.adoption || null, instance_id: 'instB', project_id: 'pNative', offset: 0, limit: 30, current_revision: nativeRevision, adoptable: true, native_review_qualification: scenario === 'ocr' ? 'independent' : 'native_disposition', current: scenario === 'classification' ? { human_label_id: 'B', origin: 'manual', disposition_revision: nativeRevision } : { id: 'resultOcr', version_id: 'versionOcr', edited: { text: `人工 OCR 修订 ${nativeRevision}` }, revision: nativeRevision, origin: 'manual' }, history: { items: [], total: nativeRevision } })
    if (path.endsWith('/dispositions')) { saved.push(JSON.parse(init!.body as string)); record.disposition = 'confirmed'; record.adoption = { ...saved[0].adoption, actor: saved[0].actor, origin: 'manual' }; return response(value()) }
    if (path.includes('/issues')) return response({ ...issuePage([record], value().revision), summary: value().summary })
    if (path.includes('/batches/bMonthly')) return response(value())
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByRole('button', { name: '采用此项原生修订 1' }); reviewer('李复核')
  expect(saved).toHaveLength(0)
  nativeRevision = 2; fireEvent.click(screen.getByRole('button', { name: '读取原生修订' }))
  await screen.findByRole('button', { name: '采用此项原生修订 2' })
  fireEvent.change(screen.getByRole('textbox', { name: '复核说明' }), { target: { value: '采用明确的人工修订' } })
  fireEvent.click(screen.getByRole('button', { name: '采用此项原生修订 2' }))
  await waitFor(() => expect(saved).toHaveLength(1))
  expect(saved[0]).toMatchObject({ expected_revision: 8, item_ids: ['iAdopt'], action: 'adopt', actor: { display_name: '李复核', source: 'local' }, adoption: scenario === 'classification' ? { native_revision: 2, human_label_id: 'B' } : { native_revision: 2, native_result_id: 'resultOcr', native_version_id: 'versionOcr' } })
  expect(await screen.findByText(/李复核 · 采用修订/)).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: '平台已采用修订' })).toBeInTheDocument()
  if (scenario === 'ocr') expect(screen.getByText(/OCR 原生复核资格独立，未表示原生复核通过/)).toBeInTheDocument()
  expect(record.raw_output).toEqual(scenario === 'classification' ? { prediction_label_id: 'A', ordinal: 0 } : { text: '原始 OCR 文本', ordinal: 0 })
}, 20000)

function reviewBundle(overrides: Partial<ReviewBundle> = {}): ReviewBundle {
  return { bundle_id: 'bundleReviewOld', workspace_id: 'wMonthly', kind: 'batch_review', batch_id: 'bMonthly', batch_revision: 3, delivery_kind: 'final', predecessor_bundle_id: null, successor_bundle_ids: ['bundleReviewNew'], successor_count: 1, status: 'outdated', request_id: 'rReviewOld', payload_hash: 'd'.repeat(64), snapshot_hash: 'e'.repeat(64), title: '月度正式复核包', created_at: '2026-10-03T02:00:00Z',
    files: ['results.csv', 'results.xlsx', 'provenance.json', 'original-1-差异.csv'].map((name, index) => ({ name, artifact_id: `artifact${index}`, revision: 1, media_type: 'application/octet-stream', size_bytes: 100 + index, availability: 'available', blob_hash: String(index).repeat(64) })),
    inputs: inputs.map((asset, index) => ({ role: index ? 'right' : 'left', asset_id: asset.asset_id, asset_revision: asset.revision, blob_hash: asset.blob_hash })), coverage: batch().coverage, unresolved_count: 0, unresolved_preview: [], execution_status: 'succeeded', pending_operations: 0, ...overrides }
}

test('only intermediate publication is enabled while unresolved, lost publication replays four fixed fields, and reopen preserves old downloads', async () => {
  const record = issue('iPublish', '未完成的复核事项')
  let revision = 3; let published = false
  const writes: { request_id: string; expected_revision: number; actor: unknown; kind: string }[] = []
  function latest() { const value = batch([record], revision); if (published) { value.delivery = { bundle_id: 'bundleReviewOld', revision: 3, status: revision === 3 ? 'current' : 'outdated', kind: 'intermediate' }; value.artifacts.push({ bundle_id: 'bundleReviewOld', title: '固定中间复核包', kind: 'batch_review', files: [{ name: 'results.xlsx', availability: 'available' }] }) } return value }
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (path.endsWith('/review-artifacts')) { writes.push(JSON.parse(init!.body as string)); if (writes.length === 1) throw new Error('材料响应丢失'); published = true; return response(latest()) }
    if (path.endsWith('/dispositions')) { expect(JSON.parse(init!.body as string).action).toBe('reopen'); revision = 4; return response(latest()) }
    if (path.includes('/issues')) return response(issuePage([record], revision))
    if (path.includes('/batches/bMonthly')) return response(latest())
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByRole('heading', { name: '未完成的复核事项' }); reviewer('成果编制人')
  expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '生成中间复核材料' }))
  await screen.findByText('复核材料响应需要核实')
  expect(screen.getByRole('button', { name: '确认并下一项' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '核实原材料请求' }))
  await screen.findByText('固定中间复核包')
  expect(writes).toHaveLength(2); expect(writes[1]).toEqual(writes[0])
  expect(Object.keys(writes[0]).sort()).toEqual(['actor', 'expected_revision', 'kind', 'request_id'])
  expect(writes[0]).toMatchObject({ expected_revision: 3, kind: 'intermediate', actor: { display_name: '成果编制人', source: 'local' } })
  expect(screen.getByRole('link', { name: '下载 results.xlsx' })).toHaveAttribute('href', '/api/platform/v1/workspaces/wMonthly/artifacts/bundleReviewOld/files/results.xlsx')
  record.disposition = 'confirmed'
  await choose('复核状态筛选', '已确认')
  const reviewPane = within(document.querySelector('.batch-review-item') as HTMLElement)
  await waitFor(() => expect(reviewPane.getByRole('button', { name: /重\s*开/ })).toBeEnabled())
  fireEvent.click(reviewPane.getByRole('button', { name: /重\s*开/ }))
  await screen.findByText('复核成果已过期')
  expect(screen.getByRole('link', { name: '下载 results.xlsx' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '下载 差异.csv' })).toBeInTheDocument()
}, 20000)

test('final publication is gated by full index and technical pending operations, then sends the observed revision', async () => {
  let value = batch([], 12); value.coverage.index_complete = false
  let posted: unknown
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (path.endsWith('/review-artifacts')) { posted = JSON.parse(init!.body as string); value.delivery = { bundle_id: 'bundleFinal', revision: 12, kind: 'final', status: 'current' }; return response(value) }
    if (path.includes('/issues')) return response(issuePage([], 12))
    if (path.includes('/batches/bMonthly')) return response(value)
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly'); await screen.findByText('执行已结束'); reviewer('正式编制人')
  expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeDisabled()
  value = { ...value, coverage: { ...value.coverage, index_complete: true }, summary: { ...value.summary, pending_operations: 1 } }
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  await waitFor(() => expect(screen.getByText(/全量索引 已完成/)).toBeInTheDocument())
  expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeDisabled()
  value = { ...value, summary: { ...value.summary, pending_operations: 0 } }
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '生成正式复核成果' }))
  await waitFor(() => expect(posted).toMatchObject({ expected_revision: 12, kind: 'final', actor: { display_name: '正式编制人', source: 'local' } }))
  expect(await screen.findByRole('link', { name: '查看最近复核包' })).toHaveAttribute('href', '/artifacts?workspace=wMonthly&bundle=bundleFinal')
}, 20000)

test('review bundle detail preserves old download identities, exact input versions, and ready predecessor/successor links', async () => {
  const bundle = reviewBundle({ predecessor_bundle_id: 'bundleReviewFirst' })
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    const common = shared(path); if (common) return common
    if (path.endsWith('/artifacts/bundleReviewOld')) return response(bundle)
    throw new Error(path)
  }))
  mount('/artifacts?workspace=wMonthly&bundle=bundleReviewOld')
  expect(await screen.findByText('此复核包已过期')).toBeInTheDocument()
  expect(screen.getByText('固定平台版本 3', { exact: false })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '查看前版复核包' })).toHaveAttribute('href', '/artifacts?workspace=wMonthly&bundle=bundleReviewFirst')
  expect(screen.getByRole('link', { name: '查看后继复核包 1' })).toHaveAttribute('href', '/artifacts?workspace=wMonthly&bundle=bundleReviewNew')
  expect(screen.getByRole('link', { name: '返回处理批次' })).toHaveAttribute('href', '/batches?workspace=wMonthly&batch=bMonthly')
  expect(screen.getAllByRole('link', { name: '下载' })[0]).toHaveAttribute('href', '/api/platform/v1/workspaces/wMonthly/artifacts/bundleReviewOld/files/results.csv')
  expect(screen.getByText(`版本 1 · SHA256 ${'b'.repeat(64)}`)).toBeInTheDocument()
  expect(screen.getByText(`artifact0 · SHA256 ${'0'.repeat(64)}`)).toBeInTheDocument()
  expect(screen.queryByText('版本 undefined')).not.toBeInTheDocument()
}, 20000)

test('a read verified durable unknown operation allows explicit intermediate materials while final and new dispositions stay blocked', async () => {
  const record = issue('iDurable', '持久待核实事项')
  const value = batch([record], 3)
  value.summary.pending_operations = 1
  value.summary.pending_operation = { operation_id: 'opDurable', request_id: 'rDurable', state: 'needs_verification' }
  const writes: { path: string; body: unknown }[] = []
  let releaseOperation!: (response: unknown) => void
  const operationRead = new Promise(resolve => { releaseOperation = resolve })
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (init?.method === 'POST') writes.push({ path, body: JSON.parse(init.body as string) })
    if (path.endsWith('/operations/opDurable')) return operationRead
    if (path.endsWith('/review-artifacts')) { value.delivery = { bundle_id: 'bundleDurable', revision: 3, kind: 'intermediate', status: 'current' }; return response(value) }
    if (path.includes('/issues')) return response(issuePage([record], 3))
    if (path.includes('/batches/bMonthly')) return response(value)
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByText('原操作待核实')
  expect(screen.getByRole('button', { name: '生成中间复核材料' })).toBeDisabled()
  expect(screen.getByRole('textbox', { name: '本机复核人' })).toBeDisabled()
  releaseOperation(response({ schema_version: '1.0', operation_id: 'opDurable', batch_id: 'bMonthly', request_id: 'rDurable', payload_hash: 'f'.repeat(64), expected_revision: 3, actor: { id: 'uOriginal', display_name: '原处置人', source: 'local' }, state: 'needs_verification', receipt: {}, created_at: '2026-10-03T01:00:00Z', updated_at: '2026-10-03T01:00:00Z' }))
  await waitFor(() => expect(screen.getByRole('textbox', { name: '本机复核人' })).toBeEnabled())
  reviewer('中间材料编制人')
  await waitFor(() => expect(screen.getByRole('button', { name: '生成中间复核材料' })).toBeEnabled())
  expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '确认并下一项' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '生成中间复核材料' }))
  await screen.findByRole('link', { name: '查看最近复核包' })
  expect(writes).toHaveLength(1)
  expect(writes[0].path).toBe('/api/platform/v1/batches/bMonthly/review-artifacts')
  expect(writes[0].body).toMatchObject({ expected_revision: 3, kind: 'intermediate', actor: { display_name: '中间材料编制人', source: 'local' } })
  expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '确认并下一项' })).toBeDisabled()
}, 20000)

test('409 publication generation in progress retains the fixed snapshot request across refresh and replays the same payload', async () => {
  const value = batch([], 15)
  const writes: unknown[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (path.endsWith('/review-artifacts')) {
      writes.push(JSON.parse(init!.body as string))
      if (writes.length === 1) return failure(409, '复核成果生成未完成；保留原请求和旧成果，可重试同一请求')
      value.delivery = { bundle_id: 'bundleRetry', revision: 15, kind: 'intermediate', status: 'current' }; return response(value)
    }
    if (path.includes('/issues')) return response(issuePage([], 15))
    if (path.includes('/batches/bMonthly')) return response(value)
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly'); await screen.findByText('执行已结束'); reviewer('固定快照编制人')
  fireEvent.click(screen.getByRole('button', { name: '生成中间复核材料' }))
  await screen.findByText('复核材料响应需要核实')
  const original = JSON.parse(localStorage.getItem('clear-batch-review:bMonthly')!)
  expect(original.status).toBe('uncertain')
  fireEvent.click(screen.getByRole('button', { name: '刷新核对，保留材料请求' }))
  await waitFor(() => expect(JSON.parse(localStorage.getItem('clear-batch-review:bMonthly')!)).toEqual(original))
  expect(screen.getByRole('button', { name: '生成正式复核成果' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '生成中间复核材料' })).toBeDisabled()
  expect(writes).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '核实原材料请求' }))
  await screen.findByRole('link', { name: '查看最近复核包' })
  expect(writes).toHaveLength(2); expect(writes[1]).toEqual(writes[0])
  expect(writes[1]).toMatchObject({ expected_revision: 15, kind: 'intermediate', actor: { display_name: '固定快照编制人', source: 'local' } })
  expect(localStorage.getItem('clear-batch-review:bMonthly')).toBeNull()
}, 20000)
