import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { beforeAll, afterEach, test, expect, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { MemoryRouter } from 'react-router-dom'
import { AgentPreferences } from '../src/features/agent/AgentPreferences'
import { ArtifactFreshness } from '../src/features/artifacts/ArtifactFreshness'
import { ScreenDiagnostics } from '../src/features/ocr/ScreenDiagnostics'
import { reliableDiagnosticBox, type Diagnostic } from '../src/features/ocr/screenDiagnosticWorkflow'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal('crypto', { randomUUID })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function mount(child: React.ReactNode) { return render(<ConfigProvider><QueryClientProvider client={new QueryClient({ defaultOptions:{ queries:{retry:false} } })}><MemoryRouter>{child}</MemoryRouter></QueryClientProvider></ConfigProvider>) }
const ok = (value: unknown) => ({ok:true,json:async()=>value})

test('explicit default save and revoke retain the existing task snapshot and provenance', async () => {
  const writes: any[] = []
  let revision = 1, entries: any = {value_mode:{value:'text',business:'screen_excel',revision:1,source:{quote:'以后保留原文',actor:'user'}}}
  vi.stubGlobal('fetch',vi.fn(async (path:string,init?:RequestInit) => {
    if(path.includes('/snapshots')) return ok({snapshots:[{run_id:'frozen-run',business:'screen_excel',profile_revision:1,project_revision:0,values:{value_mode:'text'},overrides:{},sources:entries,sha256:'fixed-snapshot'}]})
    if(init?.method==='POST'){const body=JSON.parse(String(init.body));writes.push(body);revision++;if(body.revoke_keys.length)entries={};else entries={value_mode:{value:body.values.value_mode,business:'screen_excel',revision,source:body.source}};return ok({revision})}
    return ok({profile:{workspace_id:'wtest',business:'screen_excel',revision,entries},project_profile:{revision:0,entries:{}},effective_sources:entries,effective_values:{value_mode:'text'}})
  }))
  mount(<AgentPreferences open close={()=>{}} workspace="wtest" sessionId="sessionA" />)
  await screen.findByText('来源：以后保留原文', {exact:false})
  fireEvent.mouseDown(screen.getByLabelText('默认原值格式'))
  fireEvent.click(await screen.findByText('仅确认并明确选列后使用数值'))
  fireEvent.click(screen.getByRole('button',{name:'保存后续任务默认'}))
  await waitFor(()=>expect(writes).toHaveLength(1))
  expect(writes[0]).toMatchObject({business:'screen_excel',expected_revision:1,values:{value_mode:'confirmed_numeric'},source:{mode:'user_form'}})
  expect(writes[0]).not.toHaveProperty('numeric_columns')
  await screen.findByText('当前默认 · 版本 2')
  expect(screen.getByText('原值格式：保留原文文本')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'撤回原值格式'}))
  await waitFor(()=>expect(writes).toHaveLength(2))
  expect(writes[1]).toMatchObject({expected_revision:2,values:{},revoke_keys:['value_mode']})
  await screen.findByText('当前默认 · 版本 3')
  expect(screen.getByText('固定身份与来源')).toBeInTheDocument()
},30000)

test('historical screen export keeps a version label and links to its fixed batch scope', async () => {
  vi.stubGlobal('fetch',vi.fn(async()=>ok({kind:'screen_batch',batch_id:'batchA',state:'historical',current:false,snapshot_id:'old',current_snapshot_id:'new',export_options:{include_pending_review:true}})))
  mount(<ArtifactFreshness workspaceId="wtest" bundleId="bundleA" inputSetId="setA" projectId="projectA" handoffId="handoffA" />)
  await screen.findByText('历史导出版本')
  const link=screen.getByRole('link',{name:'返回当前批次重新导出'})
  expect(link.getAttribute('href')).toContain('screen_batch=batchA')
  expect(link.getAttribute('href')).toContain('set=setA')
  expect(screen.getByText('这份成果保留历史版本，可继续下载；请回批次核对当前范围和数值列，再重新导出。')).toBeInTheDocument()
})

test('diagnostic geometry rejects index-only boxes and exposes budget/capability reasons', async () => {
  const item: Diagnostic={id:'i1',code:'difference',severity:'high',reason:'关键值不一致',location:{level:'cell',table:0,row:1,column:1,box:[10,10,50,30],basis:'indexes_only'},evidence:[{current:'01.10',alternative:'01.20'}],applicable_actions:[{action:'precision_subbatch',label:'提高这张精度',enabled:false,reason:'remaining_budget_insufficient'},{action:'local_reread',label:'局部复识',enabled:false,reason:'capability_not_verified'}]}
  expect(reliableDiagnosticBox(item)).toBeNull()
  const reliable={...item,location:{...item.location,basis:'observed_full_cell_geometry',range_semantics:'full_cell'}}
  expect(reliableDiagnosticBox(reliable)).toEqual([10,10,50,30])
  mount(<ScreenDiagnostics data={{items:[item],summary:{issues:1,groups:1,cell:0,table:0,image:1},budget:{remaining_seconds:0}}} busy={false} select={()=>{}} action={()=>{}} />)
  expect(screen.getAllByText('全图 · 无可靠单格定位').length).toBeGreaterThan(0)
  expect(screen.getByRole('button',{name:'提高这张精度'})).toBeDisabled()
  expect(screen.getByText('（剩余预算不足）')).toBeInTheDocument()
  expect(screen.queryByText(/（可靠单格）/)).not.toBeInTheDocument()
})
