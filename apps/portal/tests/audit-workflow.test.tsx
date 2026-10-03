import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { theme } from '../src/router'
import { AuditWorkbench } from '../src/features/audit/AuditWorkbench'
import type { AuditFinding, AuditProcedure, AuditProject, AuditRecord, WorkpaperBundle } from '../src/features/audit/api'

beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  localStorage.clear()
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

const time = '2026-10-03T02:00:00Z'
const actor = { id: 'uAudit', display_name: '王审计', source: 'local' as const }
const hash = 'a'.repeat(64)
function project(revision = 1): AuditProject { return { schema_version: '1.0', audit_version: '1.0', project_id: 'pAudit', workspace_id: 'wAudit', revision,
  content_revision: 1, project_context_revision: 1, project: { title: '收入审计', audit_object: '销售公司', period_start: '2026-01-01', period_end: '2026-09-30', objective: '核查收入完整性' },
  procedure_count: 1, finding_count: 0, records: [], records_current: false, created_at: time, updated_at: time } }
function procedure(): AuditProcedure { return { schema_version: '1.0', audit_version: '1.0', procedure_id: 'sAudit', project_id: 'pAudit', project_revision: 1,
  content_revision: 1, project_context_revision: 1, procedure: { title: '收入对账', basis: '总账明细', scope: '本期全部交易', note: '', conclusion: '' },
  batches: [{ batch_id: 'bAudit', batch_revision: 7, template_id: 'tAudit', template_version: 1, config_hash: hash }], records: [], records_current: false, created_at: time, updated_at: time } }
const page = <T,>(items: T[], total = items.length, next_cursor: string | null = null) => ({ schema_version: '1.0', audit_version: '1.0', project_id: 'pAudit', workspace_id: 'wAudit', project_revision: 1, items, total, snapshot_token: 'snapshotAudit', next_cursor })
const response = (value: unknown) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(value)) })
const failure = (status: number, detail: string) => ({ ok: false, status, json: async () => ({ detail }) })
const batch = { batch_id: 'bAudit', workspace_id: 'wAudit', revision: 7,
  template: { template_id: 'tAudit', version: 1, name: '月度对账', scenario: 'data_compare', config_hash: hash },
  artifacts: [], summary: {}, status: 'succeeded' }
function mount(url = '/audit?workspace=wAudit&project=pAudit&procedure=sAudit') {
  const router = createMemoryRouter([{ path: '/audit', element: <AuditWorkbench /> }], { initialEntries: [url] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const view = render(<ConfigProvider theme={theme}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, view }
}
async function choose(label: string, option: string) {
  fireEvent.mouseDown(await screen.findByRole('combobox', { name: label }))
  fireEvent.click((await screen.findAllByText(option, { selector: '.ant-select-item-option-content' })).at(-1)!)
}
function shared(path: string) {
  if (path.includes('/forensics?')) return response({ schema_version: '1.0', items: [], total: 0, next_offset: null })
  if (path.includes('/workspaces?')) return response({ items: [{ workspace_id: 'wAudit', name: '审计空间' }], next_cursor: null, snapshot_revision: 1 })
  if (path.includes('/batches?')) return response({ items: [batch] })
  return null
}
const file = (name: string) => ({ name, artifact_id: name.endsWith('.md') ? 'aMarkdown' : 'aJson', revision: 1, media_type: 'text/plain', size_bytes: 300, availability: 'available' as const, blob_hash: hash })
const review = { bundle_id: 'rReview', workspace_id: 'wAudit', kind: 'batch_review', batch_id: 'bAudit', batch_revision: 7,
  delivery_kind: 'final', status: 'current', title: '月度对账最终复核', created_at: time, files: [file('results.xlsx')] }
function workpaper(revision: number): WorkpaperBundle { return { schema_version: '1.0', audit_version: '1.0', workpaper_id: 'wPaper', bundle_id: 'wPaper',
  workspace_id: 'wAudit', project_id: 'pAudit', project_revision: revision, kind: 'audit_workpaper', delivery_kind: 'final', status: 'current',
  request_id: 'rWorkpaper', payload_hash: hash, snapshot_hash: hash, actor, title: '收入审计底稿', created_at: time,
  predecessor_workpaper_id: null, successor_workpaper_ids: [], successor_count: 0, files: [file('workpaper.md'), file('workpaper.json')],
  review_refs: [{ bundle_id: 'rReview', batch_id: 'bAudit', batch_revision: 7, snapshot_hash: hash, results_xlsx: file('results.xlsx') }], unresolved_count: 0, unresolved_reasons: [] } }

test('business flow binds an actual batch, selects a later issue page, saves a resolved investigation, records reviews and downloads fixed workpapers', async () => {
  localStorage.setItem('clear-local-reviewer', JSON.stringify(actor))
  let currentProject = project()
  let currentProcedure = procedure()
  let currentFinding: AuditFinding | null = null
  let paper: WorkpaperBundle | null = null
  const writes: { path: string; body: Record<string, any> }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (init?.method === 'POST') {
      const body = JSON.parse(init.body as string)
      writes.push({ path, body })
      expect(body.expected_revision).toBe(currentProject.revision)
      expect(body.actor).toEqual(actor)
      currentProject = { ...currentProject, revision: currentProject.revision + 1 }
      if (path.endsWith('/workpapers')) { paper = workpaper(currentProject.revision); return response(paper) }
      let entity_kind = 'finding', entity_id = 'fAudit'
      if (path.endsWith('/procedures/sAudit')) { currentProcedure = { ...currentProcedure, procedure: body.procedure, batches: body.batches.map((item: object) => ({ ...item, template_id: 'tAudit', template_version: 1, config_hash: hash })) }; entity_kind = 'procedure'; entity_id = 'sAudit' }
      if (path.endsWith('/findings')) {
        currentFinding = { schema_version: '1.0', audit_version: '1.0', project_id: 'pAudit', procedure_id: 'sAudit', finding_id: 'fAudit', project_revision: currentProject.revision,
          content_revision: 1, project_context_revision: 1, finding: body.finding, records: [], records_current: false, created_at: time, updated_at: time,
          evidence_refs: body.evidence.map((item: object) => ({ ...item, evidence_id: 'eAudit', project_id: 'pAudit', workspace_id: 'wAudit', scenario: 'data_compare', issue_type: 'result', snapshot_hash: hash, captured_at: time, sources: [], source_count: 0 })) }
        currentProject.finding_count = 1
      }
      if (path.endsWith('/records')) {
        const owner = body.entity_kind === 'project' ? currentProject : body.entity_kind === 'procedure' ? currentProcedure : currentFinding!
        const record: AuditRecord = { ...body, record_id: 'rRecord' + writes.length, project_revision: currentProject.revision, created_at: time }
        owner.records = [...owner.records.filter(item => item.role !== body.role), record]
        owner.records_current = owner.records.length === 2
        entity_kind = 'record'; entity_id = record.record_id
      }
      return response({ schema_version: '1.0', audit_version: '1.0', request_id: body.request_id, project_id: 'pAudit', project_revision: currentProject.revision,
        entity_kind, entity_id, actor, payload_hash: hash, created_at: time })
    }
    if (path.includes('/artifacts?')) return response({ items: paper ? [review, paper] : [review], next_offset: null })
    if (path.endsWith('/artifacts/rReview')) return response(review)
    if (path.endsWith('/artifacts/wPaper')) return response(paper)
    if (path.includes('/batches/bAudit/issues')) {
      const later = new URL(path, 'http://local').searchParams.get('offset') === '20'
      return response({ items: [{ item_id: later ? 'iLater' : 'iFirst', title: later ? '第二页收入差异' : '第一页差异', detail: '金额需要解释', disposition: 'pending' }], revision: 7, total: 21 })
    }
    if (path.includes('/audit-projects/pAudit/procedures/sAudit')) return response(currentProcedure)
    if (path.includes('/audit-projects/pAudit/procedures?')) return response(page([currentProcedure]))
    if (path.includes('/audit-projects/pAudit/findings/fAudit')) return response(currentFinding)
    if (path.includes('/audit-projects/pAudit/findings?')) return response(page(currentFinding ? [currentFinding] : []))
    if (path.endsWith('/audit-projects/pAudit')) return response(currentProject)
    if (path.includes('/audit-projects?')) return response(page([currentProject]))
    throw new Error(path)
  }))
  mount()
  await screen.findByRole('textbox', { name: '程序名称' })
  fireEvent.change(screen.getByRole('textbox', { name: '程序说明' }), { target: { value: '抽核全量销售收入' } })
  fireEvent.click(screen.getByRole('button', { name: '保存审计程序' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0].body.batches).toEqual([{ batch_id: 'bAudit', batch_revision: 7 }])
  await waitFor(() => expect(screen.getByRole('button', { name: '从程序生成调查事项' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '从程序生成调查事项' }))
  await screen.findByRole('textbox', { name: '调查事项名称' })
  fireEvent.change(screen.getByRole('textbox', { name: '调查事项名称' }), { target: { value: '收入跨期调查' } })
  fireEvent.change(screen.getByRole('textbox', { name: '调查解释' }), { target: { value: '已核对发票与账簿' } })
  fireEvent.change(screen.getByRole('textbox', { name: '调查结论' }), { target: { value: '属于期间归属调整，已完成更正' } })
  await choose('调查状态', '已解决')
  const nextPage = document.querySelector('.ant-pagination-next')!
  fireEvent.click(nextPage)
  await screen.findByText('第二页收入差异')
  const row = screen.getByText('第二页收入差异').closest('tr')!
  fireEvent.click(within(row).getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '生成调查事项' }))
  await waitFor(() => expect(writes).toHaveLength(2))
  expect(writes[1].body.evidence).toEqual([{ batch_id: 'bAudit', item_id: 'iLater', batch_revision: 7 }])
  expect(writes[1].body.finding).toMatchObject({ status: 'resolved', explanation: '已核对发票与账簿', conclusion: '属于期间归属调整，已完成更正' })
  await screen.findByText('调查解释与结论')
  for (const tab of ['调查事项', '审计程序', '项目目标']) {
    fireEvent.click(screen.getByRole('tab', { name: tab }))
    const panel = await screen.findByRole('tabpanel', { name: tab })
    await waitFor(() => expect(within(panel as HTMLElement).getByRole('button', { name: '登记编制' })).toBeEnabled())
    fireEvent.click(within(panel as HTMLElement).getByRole('button', { name: '登记编制' }))
    await waitFor(() => expect(within(panel as HTMLElement).getByRole('button', { name: '登记复核' })).toBeEnabled())
    fireEvent.click(within(panel as HTMLElement).getByRole('button', { name: '登记复核' }))
    await waitFor(() => expect(within(panel as HTMLElement).getByText('记录均为当前版本')).toBeInTheDocument())
  }
  fireEvent.click(screen.getByRole('tab', { name: '审计底稿' }))
  await choose('底稿采用复核成果', '月度对账最终复核 · 批次 bAudit 版本 7')
  fireEvent.click(screen.getByRole('button', { name: '生成最终审计底稿' }))
  expect(await screen.findByRole('link', { name: '下载 workpaper.md' })).toHaveAttribute('href', '/api/platform/v1/workspaces/wAudit/artifacts/wPaper/files/workpaper.md')
  expect(screen.getByRole('link', { name: '下载 workpaper.json' })).toHaveAttribute('href', '/api/platform/v1/workspaces/wAudit/artifacts/wPaper/files/workpaper.json')
  expect(writes.at(-1)?.body.review_bundle_ids).toEqual(['rReview'])
  expect(writes.filter(item => item.path.endsWith('/records')).map(item => [item.body.entity_kind, item.body.role])).toEqual([
    ['finding', 'prepared'], ['finding', 'reviewed'], ['procedure', 'prepared'], ['procedure', 'reviewed'], ['project', 'prepared'], ['project', 'reviewed'],
  ])
  expect(localStorage.getItem('clear-audit-request:wAudit')).toBeNull()
}, 60000)

test('a lost project-create response survives reload and replays the entire original request with its fixed actor', async () => {
  let lost = true
  const writes: Record<string, unknown>[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (path.endsWith('/audit-projects') && init?.method === 'POST') {
      const body = JSON.parse(init.body as string); writes.push(body)
      if (lost) throw new Error('连接中断')
      return response({ request_id: body.request_id, project_id: 'pAudit', project_revision: 1, entity_kind: 'project', entity_id: 'pAudit', actor: body.actor, payload_hash: hash, created_at: time })
    }
    if (path.endsWith('/audit-projects/pAudit')) return response(project())
    if (path.includes('/procedures?')) return response(page([procedure()]))
    if (path.includes('/findings?')) return response(page([]))
    if (path.includes('/audit-projects?')) return response(page([]))
    throw new Error(path)
  }))
  const first = mount('/audit?workspace=wAudit')
  fireEvent.change(await screen.findByRole('textbox', { name: '审计本机操作人' }), { target: { value: '李编制' } })
  fireEvent.click(screen.getByRole('button', { name: '新建项目' }))
  fireEvent.change(screen.getByRole('textbox', { name: '项目名称' }), { target: { value: '采购审计' } })
  fireEvent.change(screen.getByRole('textbox', { name: '审计对象' }), { target: { value: '采购部' } })
  fireEvent.change(screen.getByLabelText('期间开始'), { target: { value: '2026-01-01' } })
  fireEvent.change(screen.getByLabelText('期间结束'), { target: { value: '2026-09-30' } })
  fireEvent.click(screen.getByRole('button', { name: '创建审计项目' }))
  await screen.findByText('原请求响应需要核实')
  expect(writes).toHaveLength(1)
  first.view.unmount(); lost = false
  mount('/audit?workspace=wAudit')
  await screen.findByText('原请求响应需要核实')
  fireEvent.click(screen.getByRole('button', { name: '核实原请求并继续' }))
  await screen.findByText('所见项目版本 1 · 1 个程序 · 0 个调查事项')
  expect(writes).toHaveLength(2)
  expect(writes[1]).toEqual(writes[0])
  expect(writes[0].actor).toMatchObject({ display_name: '李编制', source: 'local' })
  expect(localStorage.getItem('clear-audit-request:wAudit')).toBeNull()
}, 20000)

test('an interrupted workpaper publication resumes with the original payload after request status confirms publishing', async () => {
  localStorage.setItem('clear-local-reviewer', JSON.stringify(actor))
  const payload = { schema_version: '1.0', audit_version: '1.0', request_id: 'rWorkpaper', actor, expected_revision: 1, kind: 'final', review_bundle_ids: ['rReview'] }
  localStorage.setItem('clear-audit-request:wAudit', JSON.stringify({ workspace_id: 'wAudit', project_id: 'pAudit',
    path: '/audit-projects/pAudit/workpapers', payload, label: '生成最终审计底稿', status: 'conflict' }))
  const submitted: unknown[] = []
  let ready = false
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (path.endsWith('/requests/rWorkpaper')) return response({ schema_version: '1.0', audit_version: '1.0', request_id: 'rWorkpaper', project_id: 'pAudit', payload_hash: hash, state: 'publishing', receipt: null })
    if (path.endsWith('/workpapers') && init?.method === 'POST') { submitted.push(JSON.parse(init.body as string)); ready = true; return response(workpaper(1)) }
    if (path.endsWith('/audit-projects/pAudit')) return response(project())
    if (path.includes('/procedures?')) return response(page([procedure()]))
    if (path.includes('/findings?')) return response(page([]))
    if (path.includes('/audit-projects?')) return response(page([project()]))
    if (path.includes('/artifacts?')) return response({ items: ready ? [review, workpaper(1)] : [review], next_offset: null })
    if (path.endsWith('/artifacts/rReview')) return response(review)
    if (path.endsWith('/artifacts/wPaper')) return response(workpaper(1))
    throw new Error(path)
  }))
  mount('/audit?workspace=wAudit&project=pAudit')
  fireEvent.click(await screen.findByRole('button', { name: '核实原请求并继续' }))
  await screen.findByRole('link', { name: '下载 workpaper.md' })
  expect(submitted).toEqual([payload])
  expect(localStorage.getItem('clear-audit-request:wAudit')).toBeNull()
}, 20000)

test('a version conflict keeps explanation and the fixed request until status is verified, then explicitly refreshes the project revision', async () => {
  localStorage.setItem('clear-local-reviewer', JSON.stringify(actor))
  let revision = 1
  const saved: Record<string, unknown>[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const common = shared(path); if (common) return common
    if (init?.method === 'POST') { saved.push(JSON.parse(init.body as string)); revision = 2; return failure(409, '项目版本已变化') }
    if (path.includes('/requests/')) return failure(404, '请求未提交')
    if (path.endsWith('/audit-projects/pAudit')) return response(project(revision))
    if (path.includes('/procedures?')) return response(page([procedure()]))
    if (path.includes('/findings?')) return response(page([]))
    throw new Error(path)
  }))
  mount('/audit?workspace=wAudit&project=pAudit')
  const objective = await screen.findByRole('textbox', { name: '审计目标' })
  fireEvent.change(objective, { target: { value: '补充核查全部银行流水' } })
  fireEvent.click(screen.getByRole('button', { name: '保存项目目标' }))
  await screen.findByText('所见版本或发布条件冲突，原请求与草稿已保留')
  expect(objective).toHaveValue('补充核查全部银行流水')
  expect(saved[0].expected_revision).toBe(1)
  fireEvent.click(screen.getByRole('button', { name: '核实并刷新所见版本' }))
  await screen.findByText('原请求未提交；已刷新所见版本并保留草稿，请核对后再次保存。')
  expect(objective).toHaveValue('补充核查全部银行流水')
  expect(await screen.findByText('所见项目版本 2 · 1 个程序 · 0 个调查事项')).toBeInTheDocument()
  expect(localStorage.getItem('clear-audit-request:wAudit')).toBeNull()
  expect(saved).toHaveLength(1)
}, 20000)
