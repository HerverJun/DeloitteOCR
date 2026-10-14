import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { FirstDataSetup } from '../src/features/batches/FirstDataSetup'
import { TemplateSummary } from '../src/features/batches/TemplateSummary'
import type { Template } from '../src/features/batches/api'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => { localStorage.clear(); const computed = window.getComputedStyle.bind(window); vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element)) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = (value: unknown) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(value)) })
function mount(element: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<ConfigProvider><QueryClientProvider client={client}><MemoryRouter>{element}</MemoryRouter></QueryClientProvider></ConfigProvider>)
}
async function choose(label: string, option: string) {
  fireEvent.mouseDown(await screen.findByRole('combobox', { name: label }))
  fireEvent.click((await screen.findAllByText(option, { selector: '.ant-select-item-option-content' })).at(-1)!)
}
const template: Template = { template_id: 'tIncome', version: 3, name: '收入核对', scenario: 'data_compare', config_hash: 'a'.repeat(64), config: {
  roles: { left: { formats: ['csv'], columns: [{ header: '编号', kinds: ['text'] }, { header: '收入', kinds: ['decimal'] }], steps: [{ operator: 'trim', parameters: { column: '编号' } }, { operator: 'cast', parameters: { column: '收入', target: 'decimal' } }] },
    right: { formats: ['xlsx'], columns: [{ header: '收入金额', kinds: ['decimal'] }, { header: '业务编号', kinds: ['text'] }], steps: [] } },
  comparison: { key_pairs: [['编号', '业务编号']], compare_pairs: [['收入', '收入金额']], amount_pairs: [['收入', '收入金额']], amount_tolerance: '0.05', type_policy: 'strict', null_policy: 'never_match', trim_text: true, casefold_text: false },
} }

test('first setup imports both roles, uses explicit actual column IDs despite reversed order, checks sample results and saves after confirmation', async () => {
  const onSaved = vi.fn()
  let current: any = { id: 'sSample', name: '首配样本', version: 1, jobs: [], state: { tables: {}, result_id: null } }
  const operations: any[] = []
  const left = { revision: 'vLeft', name: '左账.csv', rows: 2, upload_id: 'uLeft', format: 'csv', sheets: [], options: {}, history: [], columns: [{ column_id: 'c1', header: '编号' }, { column_id: 'c2', header: '收入' }] }
  const right = { revision: 'vRight', name: '右账.csv', rows: 2, upload_id: 'uRight', format: 'csv', sheets: [], options: {}, history: [], columns: [{ column_id: 'c1', header: '收入金额' }, { column_id: 'c2', header: '业务编号' }] }
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/assets?')) return response({ items: [{ asset_id: 'aLeft', original_name: '左账.csv', revision: 1 }, { asset_id: 'aRight', original_name: '右账.csv', revision: 1 }], next_offset: null })
    if (path.endsWith('/assets/sets')) return response({ asset_set_id: 'sAssets', revision: 1, items: [{ item_id: 'mLeft', asset_id: 'aLeft' }, { item_id: 'mRight', asset_id: 'aRight' }] })
    if (path.endsWith('/data/sessions')) return response(current)
    if (path.endsWith('/asset-uploads')) return response({ id: JSON.parse(init!.body as string).item_id === 'mLeft' ? 'uLeft' : 'uRight' })
    if (path.endsWith('/actions')) {
      const body = JSON.parse(init!.body as string); operations.push(body); current.version++
      if (body.action === 'import') current.state.tables[body.payload.side] = body.payload.side === 'left' ? left : right
      if (body.action === 'step') current.state.tables.left = { ...left, revision: 'vClean' }
      if (body.action === 'compare') { current.state.result_id = 'rSample'; current.state.comparison = { left_revision: current.state.tables.left.revision, right_revision: right.revision, spec: body.payload.spec } }
      return response({ id: 'j' + operations.length, action: body.action, status: 'succeeded' })
    }
    if (path.includes('/tables/')) return response({ rows: [{ row_id: 'rOriginal', source_row: 2, cells: [{ kind: 'text', value: '9223372036854775807', raw: '9223372036854775807' }, { kind: 'decimal', value: '01.10', raw: '01.10' }] }] })
    if (path.includes('/results/')) return response({ total: 1, rows: [{ ordinal: 1, category: 'different', left_source_row: 2, right_source_row: 2, L_c1_value: '9223372036854775807', L_c2_value: '01.10', R_c1_value: '1.20', R_c2_value: '9223372036854775807' }], metadata: { coverage: { same_pairs: 0, different_pairs: 1, only_left: 0, only_right: 0 } } })
    if (path.endsWith('/from-data-session')) { expect(JSON.parse(init!.body as string)).toMatchObject({ name: '收入核对', session_id: 'sSample' }); return response(template) }
    if (path.endsWith('/data/sessions/sSample')) return response(current)
    throw new Error(path)
  }))
  mount(<FirstDataSetup workspaceId="wSample" name="收入核对" onSaved={onSaved} />)
  await choose('首配左表资料', '左账.csv · 版本 1'); await choose('首配右表资料', '右账.csv · 版本 1')
  fireEvent.click(screen.getByRole('button', { name: '连续导入左右样本' }))
  await screen.findByText('明确匹配键与左右列映射')
  expect(screen.getByRole('button', { name: '执行样本规则核对' })).toBeDisabled()
  expect(screen.getByRole('combobox', { name: '首配右侧映射c1' })).toHaveValue('')
  await choose('首配右侧映射c1', '业务编号'); await choose('首配列用途c1', '业务匹配键')
  await choose('首配右侧映射c2', '收入金额'); await choose('首配列用途c2', '比对值')
  await choose('首配清洗列', '收入'); await choose('首配清洗操作', '转换为十进制金额')
  fireEvent.click(screen.getByRole('button', { name: '应用首配清洗步骤' }))
  await waitFor(() => expect(operations).toHaveLength(3))
  await waitFor(() => expect(screen.getByRole('button', { name: '执行样本规则核对' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '执行样本规则核对' }))
  await screen.findByText('小样本核对结果')
  expect(operations[3].payload.spec.key_pairs).toEqual([['c1', 'c2']])
  expect(operations[3].payload.spec.compare_pairs).toEqual([['c2', 'c1']])
  expect(screen.getByRole('button', { name: '确认并保存模板' })).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox', { name: '已核对样本结果、匹配键、列映射及清洗政策' }))
  fireEvent.click(screen.getByRole('button', { name: '确认并保存模板' }))
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(template))
  expect(operations[2].payload).toEqual({ side: 'left', operator: 'cast', parameters: { column: 'c2', target: 'decimal' } })
}, 45000)

test('template summary exposes actual rules and fixed identity, including native engine selection without inventing a version', () => {
  const view = mount(<TemplateSummary template={template} />)
  expect(screen.getByText(/模板 tIncome · 版本 3/)).toHaveTextContent('配置 SHA256 ' + 'a'.repeat(64))
  expect(screen.getByText(/去除首尾空白 · 编号/)).toHaveTextContent('类型转换 · 收入 · 十进制')
  expect(screen.getByLabelText('可读模板摘要')).toHaveTextContent('收入 ↔ 收入金额 · 容差 0.05')
  expect(screen.getByLabelText('可读模板摘要')).toHaveTextContent('类型政策：严格区分类型')
  expect(screen.getByLabelText('可读模板摘要')).toHaveTextContent('空值政策：空值不匹配')
  view.unmount()
  mount(<TemplateSummary template={{ ...template, scenario: 'ocr', config: { link_id: 'lOcr', engines: ['paddle', 'tesseract'] } }} />)
  expect(screen.getByLabelText('可读模板摘要')).toHaveTextContent('识别引擎：paddle、tesseract')
  expect(screen.getByLabelText('可读模板摘要')).toHaveTextContent('引擎版本：当前模板未记录引擎版本')
})
