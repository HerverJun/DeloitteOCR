import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { routes, theme } from '../src/router'
import { type Batch, type Issue, type Template } from '../src/features/batches/api'

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
function batch(issues: Issue[] = []): Batch { return { batch_id: 'bMonthly', workspace_id: 'wMonthly', template, template_id: template.template_id, template_version: 1,
  status: 'succeeded', stage: 'done', summary: { total: 2, processed: 2, succeeded: 2, failed: 0, pending: issues.filter(item => item.disposition === 'pending').length, confirmed: issues.filter(item => item.disposition === 'confirmed').length, deferred: 0, coverage: { same: 1, different: 1, only_left: 0, only_right: 0, ambiguous_left: 0, ambiguous_right: 0 }, execution_complete: true, review_complete: issues.length === 0 }, issues, receipts: {}, artifact_state: 'collected',
  artifacts: [{ bundle_id: 'bundleMonthly', title: '固定比对成果', files: [{ name: '差异.csv', availability: 'available' }] }], capabilities: [], created_at: '2026-10-02T02:00:00Z', updated_at: '2026-10-02T02:00:00Z' } }
const response = (value: unknown) => ({ ok: true, json: async () => value })
function shared(input: string) {
  if (input.includes('/workspaces?')) return response({ items: [{ workspace_id: 'wMonthly', name: '月度空间' }], next_cursor: null, snapshot_revision: 1 })
  if (input.includes('/batch-templates?')) return response({ items: [template] })
  if (input.includes('/assets?')) return response({ items: inputs, next_offset: null })
  if (input.endsWith('/data/sessions')) return response({ items: [] })
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
    if (path.includes('/batches/bMonthly/issues')) return response({ items: [issue('iDifference', '金额差异')], total: 1 })
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
    if (path.includes('/batches/bMonthly/issues')) return response({ items: [], total: 0 })
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
  const saved: { item_ids: string[]; action: string; note: string }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path)
    if (common) return common
    if (path.endsWith('/dispositions')) {
      const payload = JSON.parse(init!.body as string)
      saved.push(payload)
      records.filter(item => payload.item_ids.includes(item.item_id)).forEach(item => { item.disposition = payload.action === 'defer' ? 'deferred' : 'confirmed'; item.note = payload.note })
      return response(batch(records))
    }
    if (path.includes('/issues')) return response({ items: records.filter(item => item.disposition === 'pending'), total: records.filter(item => item.disposition === 'pending').length })
    if (path.includes('/batches/bMonthly')) return response(batch(records))
    throw new Error(path)
  }))
  mount('/batches?workspace=wMonthly&batch=bMonthly')
  await screen.findByRole('heading', { name: '差异一' })
  fireEvent.change(screen.getByRole('textbox', { name: '复核说明' }), { target: { value: '已核对原账' } })
  fireEvent.click(screen.getByRole('button', { name: '确认并下一项' }))
  expect(await screen.findByRole('heading', { name: '差异二' })).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: '复核状态筛选' })).toBeInTheDocument()
  expect(saved[0]).toEqual({ item_ids: ['iFirst'], action: 'confirm', note: '已核对原账' })
  fireEvent.click(screen.getAllByRole('checkbox')[1])
  expect(screen.getByText('本页已明确选中 1 项')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '确认选中 1 项' }))
  await waitFor(() => expect(saved).toHaveLength(2))
  expect(saved[1].item_ids).toEqual(['iSecond'])
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
