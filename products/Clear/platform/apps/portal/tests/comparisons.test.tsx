// @vitest-environment-options {"url":"http://127.0.0.1:8765/"}
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { ComparisonPanel } from '../src/features/comparisons/ComparisonWorkbench'
import { ComparisonResults } from '../src/features/comparisons/ComparisonResults'
import { ProviderSelect } from '../src/features/comparisons/Configuration'
import { comparisonsApi, defaultLayout, defaultProfile, inputError, moveAsset, profileError, type ComparisonRun, type ProviderConnection } from '../src/features/comparisons/api'
import type { Asset } from '../src/features/batches/api'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
let client: QueryClient
afterEach(() => { cleanup(); client?.clear(); localStorage.clear(); vi.unstubAllGlobals() })
const asset = (name: string, id = name): Asset => ({ asset_id: id, revision: 1, original_name: name, media_type: '', blob_hash: 'a'.repeat(64) })
const connection: ProviderConnection = { id: 'provider', version: 2, name: '测试连接', model: 'vision-model', protocol: 'openai_chat_completions', base_url: 'https://api.example.test/v1',
  context_window: 32768, max_output_tokens: 8192, has_key: true, supports_vision: true, structured_output: 'json_schema' }
const run: ComparisonRun = { comparison_id: 'runA', title: '项目进度对比', status: 'succeeded', stage: 'done', revision: 2, profile: defaultProfile,
  progress: { total: 2, processed: 2, succeeded: 2, failed: 0 }, files: ['report.html', 'comparison.json', 'left.md'],
  left: { layout: defaultLayout, pages: [{ page_number: 1, status: 'succeeded', blocks: [] }] }, right: { pages: [{ page_number: 1, status: 'succeeded', blocks: [] }] },
  result: { items: [{ id: 'itemA', entity: '项目 A', field: 'completion', status: 'different', a_value: '50%', b_value: '70%', explanation: '同一截止日期，完成率不同。',
    evidence: [{ side: 'left', page_number: 1, block_id: 'b1', quote: '完成率 50%' }, { side: 'right', page_number: 1, block_id: 'b2', quote: '完成率 70%' }] }] } }
function mount(node: React.ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>)
}
function setupFetch(extra?: (url: string, init?: RequestInit) => unknown) {
  const calls: { url: string; body?: unknown; method?: string }[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method, body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined })
    const custom = extra?.(url, init)
    if (custom != null) return { ok: true, json: async () => custom }
    if (url.includes('/assets?')) return { ok: true, json: async () => ({ items: [asset('报告.pdf', 'pdf'), asset('汇报.pptx', 'ppt'), asset('page1.png'), asset('page2.png')], next_offset: null }) }
    if (url.endsWith('/connections')) return { ok: true, json: async () => ({ items: [connection] }) }
    if (url.endsWith('/profiles')) return { ok: true, json: async () => ({ items: [defaultProfile] }) }
    if (url.endsWith('/comparisons') || url.includes('/comparisons?')) return { ok: true, json: async () => init?.method === 'POST' ? run : ({ items: [], total: 0 }) }
    if (url.endsWith('/runA')) return { ok: true, json: async () => run }
    throw new Error(`Unexpected request: ${url}`)
  }))
  return calls
}

test('input boundary rejects mixed slides and preserves an explicit image order', () => {
  expect(inputError([asset('x.pdf')], [asset('a.pptx')])).toBe('')
  expect(inputError([asset('x.pdf')], [asset('a.png'), asset('b.jpg')])).toBe('')
  expect(inputError([asset('x.pdf')], [asset('a.pptx'), asset('b.png')])).toMatch('不能混合')
  expect(inputError([asset('x.png')], [asset('b.pptx')])).toMatch('PDF')
  expect(inputError([asset('x.pdf')], Array.from({ length: 101 }, () => asset('a.png')))).toMatch('100')
  const original = ['page1', 'page2', 'page3']
  expect(moveAsset(original, 2, -1)).toEqual(['page1', 'page3', 'page2'])
  expect(original).toEqual(['page1', 'page2', 'page3'])
})

test('profile validation prevents ambiguous duplicate fields and invalid numeric tolerance', () => {
  expect(profileError(defaultProfile)).toBe('')
  expect(profileError({ ...defaultProfile, fields: [defaultProfile.fields[0], defaultProfile.fields[0]] })).toMatch('不能重复')
  expect(profileError({ ...defaultProfile, fields: [{ ...defaultProfile.fields[0], tolerance: -1 }] })).toMatch('容差')
})

test('API freezes asset and independent provider versions and sends precise page rerun scope', async () => {
  const calls = setupFetch(url => url.endsWith('/rerun-pages') ? run : undefined)
  await comparisonsApi.create('project A', { request_id: 'fixed-request', title: '进度', left: { assets: [{ asset_id: 'pdf', asset_revision: 3, blob_hash: 'a'.repeat(64) }], layout: defaultLayout },
    right: { assets: [{ asset_id: 'ppt', asset_revision: 1, blob_hash: 'b'.repeat(64) }], layout: { ...defaultLayout, mode: 'multimodal', provider_id: 'layout', provider_version: 4 } },
    profile_id: 'profile', profile_version: 2, comparison_provider_id: 'compare', comparison_provider_version: 8 })
  await comparisonsApi.rerun('project A', 'runA', 'right', [2], 3, defaultLayout)
  expect(calls[0].url).toContain('/workspaces/project%20A/comparisons')
  expect(calls[0].body).toMatchObject({ request_id: 'fixed-request', comparison_provider_id: 'compare', comparison_provider_version: 8, right: { layout: { provider_id: 'layout', provider_version: 4 } } })
  expect(calls[1].body).toEqual({ side: 'right', pages: [2], expected_revision: 3, layout: defaultLayout })
})

test('workbench defaults to local OCR and explains per-page API data flow independently', async () => {
  setupFetch()
  mount(<ComparisonPanel workspace="workspace" />)
  await screen.findByText('项目进度 · v1')
  expect(screen.getByRole('button', { name: '开始解析与比较' })).toBeDisabled()
  fireEvent.click(screen.getByText('A 侧解析设置 · 本地 OCR'))
  fireEvent.mouseDown(screen.getByLabelText('A 侧解析方式'))
  fireEvent.click(await screen.findByText('多模态 API · 一次一页'))
  expect(screen.getByLabelText('A 侧逐页解析模型')).toBeInTheDocument()
  expect(screen.queryByLabelText('B 侧逐页解析模型')).not.toBeInTheDocument()
  expect(screen.getByText(/每次只发送当前页图片/)).toBeInTheDocument()
  expect(screen.getByLabelText('比较模型（独立于逐页解析模型）')).toBeInTheDocument()
  expect(screen.getByLabelText('失败页使用本地 OCR 补全')).not.toBeChecked()
})

test('comparison explains the next missing input without creating a model request', async () => {
  const calls = setupFetch()
  mount(<ComparisonPanel workspace="workspace" />)
  await screen.findByText('项目进度 · v1')
  expect(screen.getByRole('status')).toHaveTextContent('A 侧请选择一份 PDF。')
  expect(screen.getByLabelText('比较准备情况')).toBeVisible()
  expect(screen.queryByRole('button', { name: '上传未提交资料' })).not.toBeInTheDocument()
  expect(screen.getByText('A 侧解析设置 · 本地 OCR').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByText('调整比较要求与字段').closest('details')).not.toHaveAttribute('open')
  expect(calls.some(call => call.method === 'POST')).toBe(false)
})

test('results expose fixed downloads and evidence review preserves block identity when editing', async () => {
  const calls = setupFetch((url, init) => {
    if (url.endsWith('/pages/left/1/review')) return run
    if (url.includes('/pages/left/1?revision=2') && !init?.method) return { page_number: 1, status: 'succeeded', engine: 'paddlevl', revision: 2,
      blocks: [{ block_id: 'b1', type: 'text', text: '完成率 50%', order: 0, bbox: null }], raw_response: { text: '完成率 50%' } }
  })
  const refresh = vi.fn()
  mount(<ComparisonResults workspace="workspace" run={run} connections={[connection]} refresh={refresh} />)
  expect(screen.getByRole('link', { name: 'HTML 报告' })).toHaveAttribute('href', '/api/platform/v1/workspaces/workspace/comparisons/runA/files/report.html?revision=2')
  expect(screen.getByRole('link', { name: 'A 侧 Markdown' })).toHaveAttribute('href', '/api/platform/v1/workspaces/workspace/comparisons/runA/files/left.md?revision=2')
  expect(screen.getByText('同一截止日期，完成率不同。')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'A 侧 · 第 1 页' }))
  const text = await screen.findByLabelText('块 b1 文字')
  fireEvent.change(text, { target: { value: '完成率 55%' } })
  fireEvent.click(screen.getByRole('button', { name: '保存修订并更新报告' }))
  await waitFor(() => expect(refresh).toHaveBeenCalled())
  expect(calls.find(call => call.url.endsWith('/review'))?.body).toEqual({ expected_revision: 2, blocks: [{ block_id: 'b1', type: 'text', text: '完成率 55%', order: 0, bbox: null }] })
})

test('uncertain creation reuses durable request instead of silently replacing configuration', async () => {
  const saved = { request_id: 'existing-request', title: '原任务', left: { assets: [{ asset_id: 'pdf', asset_revision: 1, blob_hash: 'a'.repeat(64) }], layout: defaultLayout },
    right: { assets: [{ asset_id: 'ppt', asset_revision: 1, blob_hash: 'a'.repeat(64) }], layout: defaultLayout }, profile_id: 'project_progress', profile_version: 1,
    comparison_provider_id: 'provider', comparison_provider_version: 2 }
  localStorage.setItem('clear-comparison-request:workspace', JSON.stringify(saved))
  const calls = setupFetch()
  mount(<ComparisonPanel workspace="workspace" />)
  fireEvent.click(await screen.findByRole('button', { name: '核实原请求并继续' }))
  await waitFor(() => expect(calls.find(call => call.method === 'POST')?.body).toEqual(saved))
  await waitFor(() => expect(localStorage.getItem('clear-comparison-request:workspace')).toBeNull())
})

test('historical report remains pinned and disables mutations against an older revision', async () => {
  setupFetch(url => url.includes('/pages/left/1?revision=2') ? { page_number: 1, status: 'succeeded', engine: 'paddlevl', revision: 2, blocks: [{ block_id: 'b1', type: 'text', text: '完成率 50%' }] } : undefined)
  mount(<ComparisonResults workspace="workspace" run={run} connections={[connection]} refresh={vi.fn()} latestRevision={3} selectRevision={vi.fn()} />)
  expect(screen.getByText('材料来源已有更新')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'A 侧 · 第 1 页' }))
  expect(await screen.findByLabelText('块 b1 文字')).toBeDisabled()
  expect(screen.getByRole('button', { name: '更换方式重跑此页' })).toBeDisabled()
})

test('parsed documents remain downloadable when comparison model fails before creating a report', () => {
  mount(<ComparisonResults workspace="workspace" run={{ ...run, status: 'failed', stage: 'comparing', error: '供应商调用失败', result: null, files: ['left.md', 'left.json', 'right.md', 'right.json'] }} connections={[connection]} refresh={vi.fn()} />)
  expect(screen.getByRole('link', { name: 'A 侧 Markdown' })).toHaveAttribute('href', '/api/platform/v1/workspaces/workspace/comparisons/runA/files/left.md?revision=2')
  expect(screen.queryByRole('link', { name: 'HTML 报告' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '继续未完成部分' })).toBeEnabled()
})

test('new profile omits an existing-version precondition while edits preserve it', async () => {
  const calls = setupFetch((url, init) => url.endsWith('/profiles') && init?.method === 'POST' ? defaultProfile : undefined)
  await comparisonsApi.saveProfile('workspace', { ...defaultProfile, id: 'new_profile' }, true)
  await comparisonsApi.saveProfile('workspace', defaultProfile)
  expect(calls[0].body).not.toHaveProperty('version')
  expect(calls[1].body).toHaveProperty('version', 1)
})

test('page rerun displays its pinned provider version and lets users deliberately select the latest', async () => {
  const change = vi.fn()
  mount(<ProviderSelect label="重跑解析模型" value="provider" version={1} connections={[connection]} onChange={change} vision />)
  expect(screen.getByText('测试连接 · 已锁定历史版本 v1')).toBeInTheDocument()
  fireEvent.mouseDown(screen.getByLabelText('重跑解析模型'))
  fireEvent.click(await screen.findByText('测试连接 · vision-model · v2'))
  expect(change).toHaveBeenCalledWith(connection)
})

test('table review edits cells without letting stale preview text overwrite them', async () => {
  const originalTable = { table_id: 't1', rows: 1, columns: 1, cells: [{ row: 0, column: 0, text: '50%' }] }
  const calls = setupFetch((url, init) => {
    if (url.endsWith('/pages/left/1/review')) return run
    if (url.includes('/pages/left/1?revision=2') && !init?.method) return { page_number: 1, status: 'succeeded', engine: 'multimodal', revision: 2,
      blocks: [{ block_id: 'b1', table_id: 't1', type: 'table', text: '50%', bbox: [.1, .1, .8, .8], source: { coordinate_origin: 'model_estimate' } }], tables: [originalTable] }
  })
  mount(<ComparisonResults workspace="workspace" run={run} connections={[connection]} refresh={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'A 侧 · 第 1 页' }))
  expect(await screen.findByLabelText('块 b1 文字')).toHaveAttribute('readonly')
  expect(screen.getByText(/模型估计坐标/)).toBeInTheDocument()
  const tableEditor = screen.getByLabelText('块 b1 表格')
  fireEvent.change(tableEditor, { target: { value: '{broken' } })
  expect(screen.getByRole('button', { name: '保存修订并更新报告' })).toBeDisabled()
  fireEvent.change(tableEditor, { target: { value: JSON.stringify({ rows: 1, columns: 1, cells: [{ row: 0, column: 0, text: '80%' }] }) } })
  fireEvent.click(screen.getByRole('button', { name: '保存修订并更新报告' }))
  await waitFor(() => expect(calls.find(call => call.url.endsWith('/review'))?.body).toMatchObject({ expected_revision: 2, blocks: [{ block_id: 'b1', table: { cells: [{ text: '80%' }] } }] }))
}, 15000)

test('recompare sends explicit profile and model versions without changing page layout', async () => {
  const calls = setupFetch(url => url.endsWith('/recompare') ? { ...run, revision: 3, status: 'queued' } : undefined)
  const refresh = vi.fn()
  mount(<ComparisonResults workspace="workspace" run={{ ...run, comparison_provider: connection, left: { document: { pages: [] } }, right: { document: { pages: [] } } }} connections={[connection]} refresh={refresh} />)
  fireEvent.click(screen.getByRole('button', { name: '按新配置重新比较' }))
  fireEvent.click(await screen.findByRole('button', { name: '创建新修订并比较' }))
  await waitFor(() => expect(refresh).toHaveBeenCalled())
  expect(calls.find(call => call.url.endsWith('/recompare'))?.body).toEqual({ expected_revision: 2, profile_id: 'project_progress', profile_version: 1, comparison_provider_id: 'provider', comparison_provider_version: 2 })
  expect(calls.some(call => call.url.includes('/rerun-pages'))).toBe(false)
})
