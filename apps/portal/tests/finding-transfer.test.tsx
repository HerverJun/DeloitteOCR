import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FindingTransfer } from '../src/features/agent/FindingTransfer'
import { FormalDraftOrigin, FormalDirectEvidence } from '../src/features/audit/FormalDraftOrigin'
import type { Finding } from '../src/features/agent/FindingsWorkspace'
import type { AuditDirectEvidenceRef, AuditFindingOrigin } from '../../../packages/contracts/types.generated'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })
const source = { task_id: 'task', draft_id: 'draft', revision: 1, snapshot_hash: 'a'.repeat(64), scope_revision: 2, project_context_revision: 3 }
const draft: Finding = { draft_id: 'draft', task_id: 'task', revision: 1, title: '台账差异', facts: '全部观察', interpretation: '原判断', missing_information: '缺凭证', recommendations: '补资料', rule: '原口径', evidence_ids: ['evidence'], status: 'draft', outdated: false, outdated_reasons: [], author: { kind: 'agent', model: 'local' }, transfer_source: source }
const ok = (data: unknown) => ({ ok: true, json: async () => data })
function mount(element: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={client}>{element}</QueryClientProvider></MemoryRouter>)
}
async function choose(label: string, text: string) {
  fireEvent.mouseDown(await screen.findByRole('combobox', { name: label }))
  fireEvent.click(await screen.findByText(text, { selector: '.ant-select-item-option-content' }))
}

test('transfer fixes source and actor, reads all archive pages, and recovers the committed receipt after reload', async () => {
  const writes: Record<string, unknown>[] = [], urls: string[] = []
  const actor = { id: 'originalOperator', display_name: '李审计', source: 'local' }
  localStorage.setItem('clear-local-reviewer', JSON.stringify(actor))
  const receipt = { schema_version: '1.0', request_id: 'committed', entity_kind: 'finding', entity_id: 'formal-finding', project_id: 'second', project_revision: 5, actor }
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    urls.push(url)
    if (init?.method === 'POST') { writes.push(JSON.parse(String(init.body))); throw new Error('转档回包未知') }
    if (url.includes('/requests/')) return ok({ state: 'committed', receipt: { ...receipt, request_id: writes[0].request_id } })
    if (url.includes('/procedures?')) return ok({ items: [{ procedure_id: 'procedure', project_id: 'second', procedure: { title: '核对程序' } }], next_cursor: null, snapshot_token: 'proc' })
    if (url.includes('cursor=next')) return ok({ items: [{ project_id: 'second', workspace_id: 'workspace', revision: 4, project: { title: '第二档案' } }], next_cursor: null, snapshot_token: 'archive' })
    return ok({ items: [{ project_id: 'first', workspace_id: 'workspace', revision: 2, project: { title: '第一档案' } }], next_cursor: 'next', snapshot_token: 'archive' })
  }))
  const view = mount(<FindingTransfer workspace="workspace" finding={draft} close={() => {}} done={() => {}} />)
  await choose('目标正式档案', '第二档案'); await choose('目标审计程序', '核对程序')
  expect(writes).toHaveLength(0)
  expect(urls.some(url => url.includes('cursor=next') && url.includes('snapshot_token=archive'))).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '确认转档' }))
  expect(await screen.findByText('转档回包未知')).toBeInTheDocument()
  expect(writes[0]).toMatchObject({ expected_revision: 4, procedure_id: 'procedure', source_draft: source, actor })
  expect(writes[0]).not.toHaveProperty('finding'); expect(writes[0]).not.toHaveProperty('evidence')
  expect(writes[0]).not.toHaveProperty('role')
  const fixed = JSON.parse(localStorage.getItem('clear-audit-request:workspace')!)
  view.unmount()
  mount(<FindingTransfer workspace="workspace" finding={{ ...draft, transfer_source: { ...source, revision: 9 } }} close={() => {}} done={() => {}} />)
  expect(screen.getByText(/源草稿 draft · v1/)).toBeInTheDocument()
  expect(screen.getByRole('textbox', { name: '本机转档操作人' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '核实原转档' }))
  expect(await screen.findByText('已转入正式档案，源草稿仍保留')).toBeInTheDocument()
  expect(writes).toHaveLength(1)
  expect(urls.some(url => url.endsWith('/requests/' + fixed.payload.request_id))).toBe(true)
  expect(localStorage.getItem('clear-audit-request:workspace')).toBeNull()
  expect(screen.getByRole('link', { name: '查看正式发现与固定证据' })).toHaveAttribute('href', '/audit?workspace=workspace&project=second&procedure=procedure&finding=formal-finding')
})

test('formal origin keeps full section text and direct evidence is read on demand without a write', async () => {
  const actor = { id: 'operator', display_name: '操作人', source: 'local' as const }
  const full = '完整事实'.repeat(2999) + '尾部保留'
  const origin: AuditFindingOrigin = { source, content: { facts: full, interpretation: '完整解释', rule: '完整口径', missing_information: '完整待补', recommendations: '完整建议' }, author: { kind: 'agent', model: '原模型' }, transfer_actor: actor, transfer_kind: 'human', transferred_at: '2026-10-05T00:00:00Z' }
  const evidence: AuditDirectEvidenceRef = { task_id: 'task', source_evidence_id: 'source-evidence', snapshot_hash: source.snapshot_hash, evidence_id: 'formal-evidence', project_id: 'archive', workspace_id: 'workspace', captured_at: '2026-10-05T00:00:00Z', scenario: 'direct_tool', sources: [], source_assets: [], source_count: 0 }
  const fetcher = vi.fn(async () => ok({ summary: '固定OCR来源', observation: { snapshot: { edited: { text: '固定识别文字' } } }, location_url: '/apps/ocr/workflow?workspace=workspace&task_return=task&result=saved', stale: true }))
  vi.stubGlobal('fetch', fetcher)
  mount(<><FormalDraftOrigin origin={origin} /><FormalDirectEvidence evidence={evidence} /></>)
  expect(screen.getByText(full)).toBeInTheDocument()
  for (const text of ['完整解释', '完整口径', '完整待补', '完整建议']) expect(screen.getByText(text)).toBeInTheDocument()
  expect(fetcher).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '读取固定证据' }))
  expect(await screen.findByText('固定识别文字')).toBeInTheDocument()
  expect(screen.getByText('原结果已有更新，以下为转档时的固定证据')).toBeInTheDocument()
  expect(fetcher.mock.calls[0]).toEqual(['/api/platform/v1/audit-projects/archive/evidence/formal-evidence', expect.anything()])
  expect(screen.getByRole('link', { name: '打开工具中的结果版本' })).toHaveAttribute('href', expect.stringContaining('task_return=task&result=saved'))
})
