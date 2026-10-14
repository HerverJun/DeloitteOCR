import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { act, cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ConfigProvider } from 'antd'
import type { Asset, EvidenceValue, ItemEvidence, SourceLocator } from '../../../packages/contracts/types.generated'
import { ItemEvidenceView } from '../src/features/todos/ItemEvidenceView'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

const source: SourceLocator = { schema_version: '1.0', kind: 'region', asset_id: 'assetA', asset_revision: 3,
  blob_hash: 'a'.repeat(64), page_number: 2, coordinate_space: 'image_pixels', box: [12, 20, 32, 48], precision: 'exact' }
const asset: Asset = { schema_version: '1.0', asset_id: 'assetA', workspace_id: 'workspaceA', source_id: 'sourceA',
  original_name: '扫描件.pdf', media_type: 'image/png', storage_mode: 'managed', blob_hash: source.blob_hash,
  size_bytes: 100, revision: 3, created_at: '2026-10-03T03:00:00Z' }
function value(role: EvidenceValue['role'], kind: EvidenceValue['kind'], literal: EvidenceValue['value'], raw = literal): EvidenceValue {
  return { role, kind, raw, value: literal, source: null }
}
function evidence(changes: Partial<ItemEvidence> = {}): ItemEvidence {
  return { schema_version: '1.0', audit_version: '1.0', workspace_id: 'workspaceA', batch_id: 'batchA', item_id: 'itemA',
    batch_revision: 7, scenario: 'data_compare', issue_type: 'result', snapshot_hash: 'b'.repeat(64),
    template_id: 'templateA', template_version: 4, config_hash: 'c'.repeat(64), result_id: 'resultA',
    native_version_id: null, native_revision: null, adoption_operation_id: null,
    rule_summary: '按已保存规则核对原件。', rule_hash: 'd'.repeat(64), fields: [], sources: [], source_count: 0,
    native_context_available: false, ...changes }
}
function mount(item: ItemEvidence) { return render(<ConfigProvider><ItemEvidenceView evidence={item} /></ConfigProvider>) }
const response = (body: unknown) => ({ ok: true, json: async () => body })

test('literal identifiers, Decimal strings, null and unknown formulas remain readable and tolerance comes only from the server', () => {
  const longId = '000922337203685477580712345678901234567890'
  const leftAmount = '1000000000000000000000000.000001'
  const rightAmount = '-1000000000000000000000000.000001'
  const item = evidence({ rule_summary: JSON.stringify({ key_pairs: [['编号', '业务编号']], compare_pairs: [['收入', '金额']],
    amount_pairs: [['收入', '金额']], amount_tolerance: '0.000000000000001', type_policy: 'strict', null_policy: 'never_match', trim_text: true, casefold_text: false }), fields: [
    { name: '业务编号', values: [value('left', 'text', longId), value('right', 'text', '001')], rule_kind: 'exact', outcome: 'different' },
    { name: '收入 / 金额', values: [value('left', 'decimal', leftAmount, '001.1000'), value('right', 'decimal', rightAmount)],
      rule_kind: 'amount', outcome: 'within_tolerance', delta: '0.009900000000001', tolerance: '0.000000000000001', within_tolerance: true },
    { name: '空值与公式', values: [value('left', 'null', null), value('right', 'unknown_formula', null, '=SUM(A1:A3)')], rule_kind: 'exact', outcome: 'unknown' },
    { name: '审批布尔值', values: [value('left', 'boolean', true)], rule_kind: 'exact', outcome: 'equal' },
  ] })
  mount(item)
  expect(screen.getByText(longId)).toHaveTextContent(longId)
  expect(screen.getByText('001')).toHaveTextContent('001')
  expect(screen.getByText(leftAmount)).toHaveTextContent(leftAmount)
  expect(screen.getByText(rightAmount)).toHaveTextContent(rightAmount)
  expect(screen.getByText('原始字面值：001.1000')).toBeInTheDocument()
  expect(screen.getByText('空值（null）')).toBeInTheDocument()
  expect(screen.getByText('原公式：=SUM(A1:A3)')).toBeInTheDocument()
  expect(screen.getByText('true')).toBeInTheDocument()
  expect(screen.getByText('服务端差额：0.009900000000001')).toBeInTheDocument()
  expect(screen.getByText('服务端容差判断：在容差内')).toBeInTheDocument()
  expect(screen.getByText('固定金额容差：0.000000000000001')).toBeInTheDocument()
  expect(screen.getByText('固定匹配键：编号 ↔ 业务编号')).toBeInTheDocument()
  expect(screen.getByText('类型政策：严格区分类型')).toBeInTheDocument()
  const details = screen.getByText('技术详情 · 固定证据身份与完整数据').closest('details')!
  expect(details).not.toHaveAttribute('open')
  expect(details.querySelector('pre')).toHaveTextContent('"snapshot_hash"')
})

test.each(['ocr', 'classification'] as const)('original and adopted %s values and actual revisions are separate from rule JSON', scenario => {
  const original = scenario === 'ocr' ? '原始 OCR：收入 01.10' : 'categoryOriginalA'
  const adopted = scenario === 'ocr' ? '人工已采用：收入 01.20' : 'categoryAdoptedB'
  mount(evidence({ scenario, native_version_id: 'nativeVersionOriginal', native_revision: 2,
    adoption_operation_id: 'operationAdoptB', rule_summary: JSON.stringify({ model_id: 'modelA', device: 'cpu', engines: ['ppocr'], link_id: 'linkA' }),
    fields: [{ name: scenario === 'ocr' ? '识别原文 / 已采用文字' : '模型建议类别 ID / 已采用人工类别 ID',
      values: [value('original', 'text', original), value('adopted', 'text', adopted)], rule_kind: 'native_revision', outcome: 'adopted' },
      { name: '原生修订版本', values: [value('original', 'integer', '0'), value('adopted', 'integer', '2')], rule_kind: 'native_revision', outcome: 'adopted' }] }))
  expect(screen.getByText(original)).toBeInTheDocument()
  expect(screen.getByText(adopted)).toBeInTheDocument()
  expect(screen.getAllByText('原始值')).toHaveLength(2)
  expect(screen.getAllByText('已采用值')).toHaveLength(2)
  expect(screen.getByText(/固定模板 templateA/)).toHaveTextContent('模板版本 4 · 批次复核版本 7')
  expect(screen.getByText(/原件版本：nativeVersionOriginal/)).toHaveTextContent('已采用原生修订：2。 采用操作：operationAdoptB')
  expect(screen.getByText('固定识别引擎：ppocr')).toBeInTheDocument()
})

test('an image appears only after identity and actual media type match, and supports a large preview', async () => {
  let resolve!: (value: unknown) => void
  const fetch = vi.fn(() => new Promise(done => { resolve = done }))
  vi.stubGlobal('fetch', fetch)
  const view = mount(evidence({ sources: [source], source_count: 1 }))
  expect(view.container.querySelector('img')).toBeNull()
  expect(screen.getByText('正在核对原件版本、内容身份和类型…')).toBeInTheDocument()
  expect(fetch.mock.calls[0][0]).toBe('/api/platform/v1/workspaces/workspaceA/assets/assetA')
  await act(async () => resolve(response(asset)))
  const image = await screen.findByAltText('证据原图：扫描件.pdf')
  expect(image).toHaveAttribute('src', '/api/platform/v1/workspaces/workspaceA/assets/assetA/content?revision=3')
  expect(screen.getByRole('link', { name: '打开原图文件' })).toHaveAttribute('href', '/api/platform/v1/workspaces/workspaceA/assets/assetA/content?revision=3')
  expect(screen.getByText(/页 2 · 区域 \[12, 20, 32, 48\] 像素/)).toBeInTheDocument()
  fireEvent.load(image)
  fireEvent.click(image)
  await screen.findByRole('dialog')
  expect(screen.getAllByAltText('证据原图：扫描件.pdf').length).toBeGreaterThanOrEqual(1)
})

test.each([{ revision: 4 }, { blob_hash: 'f'.repeat(64) }, { workspace_id: 'workspaceB' }, { media_type: 'not-a-mime' }])('source mismatch %j blocks the image and original-file link', async change => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ ...asset, ...change })))
  const view = mount(evidence({ sources: [source], source_count: 1 }))
  await screen.findByText('资料身份、版本或内容已变化，不能将当前原件显示为此证据来源。')
  expect(view.container.querySelector('img')).toBeNull()
  expect(screen.queryByRole('link', { name: '打开原图文件' })).toBeNull()
  expect(screen.queryByRole('link', { name: '查看原件文件' })).toBeNull()
})

test('actual PDF metadata never becomes an image because a filename ends with png', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ ...asset, original_name: '看似图片.png', media_type: 'application/pdf' })))
  const view = mount(evidence({ sources: [{ ...source, kind: 'cell', sheet_name: '收入表', row_number: 18, column_number: 4 }], source_count: 1 }))
  const link = await screen.findByRole('link', { name: '查看原 PDF 文档' })
  expect(link).toHaveAttribute('href', '/api/platform/v1/workspaces/workspaceA/assets/assetA/content?revision=3')
  expect(view.container.querySelector('img')).toBeNull()
  expect(screen.getByText(/工作表「收入表」 · 行 18 · 列 4/)).toBeInTheDocument()
})

test('bounded sources retain the server total and cell location without fetching hidden sources', async () => {
  const sources = Array.from({ length: 10 }, (_, index): SourceLocator => ({ ...source, kind: 'cell',
    asset_id: 'asset' + index, sheet_name: '月账', row_number: index + 2, column_number: 3 }))
  const fetch = vi.fn(async (url: string) => response({ ...asset, asset_id: url.split('/').at(-1), media_type: 'text/csv' }))
  vi.stubGlobal('fetch', fetch)
  mount(evidence({ sources, source_count: 23 }))
  expect(screen.getByText('来源共 23 处，本页显示 10 处。')).toBeInTheDocument()
  await waitFor(() => expect(screen.getAllByRole('link', { name: '查看原件文件' })).toHaveLength(10))
  expect(fetch).toHaveBeenCalledTimes(10)
  expect(within(screen.getByRole('region', { name: '来源 1' })).getByText(/工作表「月账」 · 行 2 · 列 3/)).toBeInTheDocument()
})
