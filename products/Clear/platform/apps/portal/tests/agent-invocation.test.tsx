import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { EventContent } from '../src/features/agent/AgentWorkbench'
import { ReferenceLinks } from '../src/features/agent/TaskPanel'
import { ManualReturn } from '../src/features/agent/ManualReturn'
import { FindingsWorkspace } from '../src/features/agent/FindingsWorkspace'
import { referencePath } from '../src/features/agent/referencePath'
import type { AgentRef } from '../src/features/agent/api'

vi.mock('../src/App', () => ({ useWorkspaceId: () => 'wtest' }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })
const ref: AgentRef = { kind: 'result', application: 'ocr', workspace_id: 'wtest', object_id: 'rtest', parent_id: 'handoff', revision: 2, sources: [], coverage: {} }
function mount(element: React.ReactNode, url = '/?workspace=wtest&task_return=task') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const router = createMemoryRouter([{ path: '*', element }], { initialEntries: [url] })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  return router
}

test('message, delivery and task references keep workspace and return task in rendered links', () => {
  mount(<><EventContent event={{ seq: 1, kind: 'message', run_id: null, created: '', payload: { role: 'assistant', content: '结果', references: [ref] } }} taskId="task" reply={() => {}} answered={false} answerable={false} />
    <EventContent event={{ seq: 2, kind: 'delivery', run_id: null, created: '', payload: { delivery: { result_refs: [ref], artifact_refs: [{ ...ref, kind: 'artifact' }] } } }} taskId="task" reply={() => {}} answered={false} answerable={false} />
    <ReferenceLinks refs={[ref]} taskId="task" /></>)
  const links = screen.getAllByRole('link')
  expect(links).toHaveLength(4)
  for (const link of links) {
    const url = new URL(link.getAttribute('href')!, 'http://local')
    expect(url.searchParams.get('workspace')).toBe('wtest')
    expect(url.searchParams.get('task_return')).toBe('task')
  }
  expect(links[2].getAttribute('href')).toMatch(/^\/artifacts\?/)
})

test('object kind determines artifact asset set and job routes for every application', () => {
  for (const application of ['platform', 'data', 'ocr', 'classification', 'forensics'] as const) {
    expect(referencePath({ ...ref, application, kind: 'artifact' }, 'task')).toMatch(/^\/artifacts\?/)
    expect(referencePath({ ...ref, application, kind: 'asset_set' }, 'task')).toMatch(/^\/assets\?/)
    expect(referencePath({ ...ref, application, kind: 'job' }, 'task')).toMatch(/^\/jobs\?/)
  }
  expect(referencePath({ ...ref, application: 'data', kind: 'table' }, 'task')).toContain('session=handoff')
  expect(referencePath({ ...ref, kind: 'evidence', application: 'platform' }, 'task')).toContain('session=task')
  expect(referencePath({ ...ref, kind: 'job', application: 'platform', coverage: { kind: 'batch' } }, 'task')).toContain('/batches?')
  const native = referencePath({ ...ref, coverage: { origin: 'native_binding' } }, 'task')
  expect(native).toContain('native_binding=handoff'); expect(native).not.toContain('handoff=')
})

test('verified audit draft references open their owning task and exact draft revision while retaining return scope', () => {
  const path = referencePath({ ...ref, kind: 'result', application: 'platform', object_id: 'draftExisting', parent_id: 'owningTask', revision: 7,
    sha256: 'a'.repeat(64), coverage: { kind: 'audit_draft' } }, 'returnTask')
  const url = new URL(path, 'http://platform')
  expect(url.pathname).toBe('/findings')
  expect(Object.fromEntries(url.searchParams)).toEqual({ workspace: 'wtest', task_return: 'returnTask', session: 'owningTask', draft: 'draftExisting', revision: '7', sha256: 'a'.repeat(64) })
})

test('fixed draft deep links fetch the exact hashed historical version read-only and leave the current draft editable', async () => {
  const urls: string[] = [], writes: string[] = [], hash = 'a'.repeat(64)
  const current = { draft_id: 'draftExisting', task_id: 'owningTask', revision: 8, title: '当前第八版', facts: '当前事实', interpretation: '', rule: '', missing_information: '', recommendations: '', evidence_ids: [], status: 'draft', author: { kind: 'human' }, outdated: false }
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    urls.push(url); if (init?.method) writes.push(url)
    return { ok: true, json: async () => url.includes('/revisions/3?') ? { ...current, revision: 3, title: '交付时第三版', facts: '第三版完整事实', content_sha256: hash, source_assets: ['assetOriginal'] }
      : url.endsWith('/findings') ? { items: [current], total: 1 } : url.endsWith('/task') ? { id: 'owningTask', workspace_id: 'wtest', selected_assets: [], result_refs: [], artifact_refs: [], initial_refs: [] } : { items: [] } }
  }))
  const router = mount(<FindingsWorkspace />, `/findings?workspace=wtest&session=owningTask&draft=draftExisting&revision=3&sha256=${hash}`)
  const reader = await screen.findByLabelText('固定草稿只读内容')
  expect(reader).toHaveTextContent('交付时第三版'); expect(reader).toHaveTextContent('第三版完整事实')
  expect(within(reader).queryByRole('textbox')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存草稿' })).not.toBeInTheDocument()
  expect(urls).toContain(`/api/platform/v1/agent/sessions/owningTask/findings/draftExisting/revisions/3?sha256=${hash}`)
  fireEvent.click(screen.getByRole('button', { name: '返回当前草稿列表' }))
  await waitFor(() => expect(router.state.location.search).toBe('?workspace=wtest&session=owningTask'))
  fireEvent.click(await screen.findByRole('button', { name: '当前第八版' }))
  expect(await screen.findByLabelText('发现标题')).toHaveValue('当前第八版')
  expect(screen.getByLabelText('观察事实')).toHaveValue('当前事实')
  expect(writes).toHaveLength(0)
}, 15000)

test('a draft revision response with a different content hash is rejected without falling back to current content', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => url.includes('/revisions/')
    ? { draft_id: 'draftExisting', task_id: 'owningTask', revision: 3, content_sha256: 'b'.repeat(64), title: '错误版本', source_assets: [], evidence_ids: [] }
    : url.endsWith('/task') ? { id: 'owningTask', result_refs: [], artifact_refs: [], initial_refs: [] } : { items: [], total: 0 } })))
  mount(<FindingsWorkspace />, `/findings?workspace=wtest&session=owningTask&draft=draftExisting&revision=3&sha256=${'a'.repeat(64)}`)
  expect(await screen.findByText('读取的草稿与固定版本链接不一致，请重新核实交付引用。')).toBeInTheDocument()
  expect(screen.queryByLabelText('固定草稿只读内容')).not.toBeInTheDocument()
  expect(screen.queryByText('错误版本')).not.toBeInTheDocument()
}, 15000)

test('manual result selection survives paging, submits explicit scope and restores that scope', async () => {
  const selectedAssets = Array.from({ length: 99 }, (_, i) => `old${i}`)
  const task = { id: 'task', workspace_id: 'wtest', title: '审计任务', manual: true, revision: 4, selected_assets: selectedAssets }
  let sent: Record<string, unknown> | undefined
  const first = { title: '后续PDF页', reference: { ...ref, object_id: 'page73' }, required_assets: [{ asset_id: 'new', name: '新增资料' }, { asset_id: 'old0', name: '重复资料' }] }
  const second = { title: '后续作业', reference: { ...ref, object_id: 'olderjob' }, required_assets: [{ asset_id: 'new', name: '新增资料' }] }
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (input.includes('/manual-return')) { sent = JSON.parse(String(init?.body)); return { ok: true, json: async () => ({ revision: 5, manual: false }) } }
    if (input.includes('/manual-results')) return { ok: true, json: async () => input.includes('offset=50') ? { items: [second], next_offset: null, unavailable: [] } : { items: [first], next_offset: 50, unavailable: [] } }
    if (input.includes('/native-results')) return { ok: true, json: async () => ({ links: [], items: [], next_offset: null }) }
    return { ok: true, json: async () => task }
  }))
  const router = mount(<ManualReturn />)
  fireEvent.click(await screen.findByRole('button', { name: '带回结果' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /后续PDF页/ }))
  fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /后续作业/ }))
  expect(screen.getByText('已选 2 项，最多 50 项')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '上一页' }))
  expect(await screen.findByRole('checkbox', { name: /后续PDF页/ })).toBeChecked()
  const submit = screen.getByRole('button', { name: '返回 Agent' })
  expect(submit).toBeEnabled()
  fireEvent.click(submit)
  await waitFor(() => expect(sent).toBeDefined())
  expect(sent!.include_asset_ids).toEqual(['new', 'old0'])
  expect((sent!.references as AgentRef[]).map(item => item.object_id)).toEqual(['page73', 'olderjob'])
  await waitFor(() => expect(router.state.location.search).toContain('continue='))
  const stored = JSON.parse(localStorage.getItem('clear-audit-draft:wtest:task')!)
  expect(stored.assets).toHaveLength(100)
  expect(stored.assets).toContain('new')
})

test('fixed evidence links open saved observations with a source-update warning', async () => {
  const fixed = { evidence_id: 'saved', summary: '历史分类证据', reference: ref, sources: [], observation: { items: [{ human_label: 'old' }] }, location_url: '/apps/classification/workflow?workspace=wtest&task_return=task', stale: true }
  vi.stubGlobal('fetch', vi.fn(async (input: string) => ({ ok: true, json: async () => input.includes('/evidence/') ? fixed : input.endsWith('/task') ? { title: '任务', result_refs: [], artifact_refs: [], initial_refs: [] } : { items: [] } })))
  mount(<FindingsWorkspace />, '/findings?workspace=wtest&session=task&evidence=saved&task_return=task')
  expect(await screen.findByText('结果已有更新，以下为保存时的固定快照')).toBeInTheDocument()
  expect(screen.getByText('历史分类证据')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /打开工具中的结果版本/ })).toHaveAttribute('href', fixed.location_url)
})

test('native imports are attached before the user explicitly includes new source assets', async () => {
  const task = { id: 'task', workspace_id: 'wtest', title: '审计任务', manual: true, revision: 4, selected_assets: ['selected'] }
  const links = [{ application: 'ocr', link_id: 'native_link', project_id: 'native_project' }]
  const candidate = { title: '接回的OCR结果', reference: { ...ref, parent_id: 'binding', coverage: { origin: 'native_binding' } },
    required_assets: [{ asset_id: 'imported', name: '工具内导入.png' }] }
  let attached = false, attachment: Record<string, unknown> | undefined, returned: Record<string, unknown> | undefined
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (input.includes('/native-results/attach')) {
      attachment = JSON.parse(String(init?.body)); attached = true
      return { ok: true, json: async () => ({ status: 'success', data: { binding_id: 'binding', required_assets: ['imported'] } }) }
    }
    if (input.includes('/manual-return')) {
      returned = JSON.parse(String(init?.body))
      return { ok: true, json: async () => ({ revision: 5, manual: false }) }
    }
    if (input.includes('/manual-results')) return { ok: true, json: async () => ({ items: attached ? [candidate] : [], next_offset: null, unavailable: [] }) }
    if (input.includes('/native-results')) return { ok: true, json: async () => ({ links, next_offset: null,
      items: input.includes('link_id=') ? [{ kind: 'result', object_id: 'native_result', title: '独立OCR结果' }] : [] }) }
    return { ok: true, json: async () => task }
  }))
  const router = mount(<ManualReturn />)
  fireEvent.click(await screen.findByRole('button', { name: '带回结果' }))
  fireEvent.click(screen.getByText('更多来源'))
  const nativePicker = await screen.findByRole('combobox', { name: '选择原生项目' })
  fireEvent.mouseDown(nativePicker)
  fireEvent.click(await screen.findByText('OCR · native_project', { selector: '.ant-select-item-option-content' }))
  // Selecting a native project starts a second catalog query after the overlay commits.
  await waitFor(() => expect(nativePicker.closest('.ant-select')).toHaveTextContent('OCR · native_project'), { timeout: 5000 })
  fireEvent.click(await screen.findByRole('button', { name: '核实原件并接回' }, { timeout: 5000 }))
  const selected = await screen.findByRole('checkbox', { name: /接回的OCR结果/ })
  expect(selected).not.toBeChecked()
  expect(returned).toBeUndefined()
  expect(attachment).toMatchObject({ application: 'ocr', link_id: 'native_link', object_id: 'native_result' })
  fireEvent.click(selected)
  expect(screen.getByText('同时加入任务资料范围')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '返回 Agent' }))
  await waitFor(() => expect(router.state.location.search).toContain('continue='))
  expect(returned!.include_asset_ids).toEqual(['imported'])
  expect((returned!.references as AgentRef[])[0].coverage.origin).toBe('native_binding')
  expect(JSON.parse(localStorage.getItem('clear-audit-draft:wtest:task')!).assets).toEqual(['selected', 'imported'])
}, 15000)

test('manual results default to task scope, expanding other results resets paging and cancel keeps manual control', async () => {
  const urls: string[] = [], writes: string[] = []
  const task = { id: 'task', workspace_id: 'wtest', title: '任务', manual: true, revision: 4, selected_assets: ['selected'] }
  const candidate = { title: '当前任务结果', reference: ref, required_assets: [], related_to_task: true }
  const other = { title: '项目其它结果', reference: { ...ref, object_id: 'other' }, required_assets: [{ asset_id: 'extra', name: '新增资料' }], related_to_task: false }
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    urls.push(input); if (init?.method === 'POST') writes.push(input)
    if (input.includes('/manual-results')) return { ok: true, json: async () => ({ items: input.includes('related_only=false') ? [candidate, other] : input.includes('offset=50') ? [] : [candidate], next_offset: input.includes('offset=50') || input.includes('related_only=false') ? null : 50, unavailable: [] }) }
    if (input.includes('/native-results')) return { ok: true, json: async () => ({ links: [], items: [], next_offset: null }) }
    return { ok: true, json: async () => task }
  }))
  mount(<ManualReturn />)
  fireEvent.click(await screen.findByRole('button', { name: '带回结果' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /当前任务结果/ }))
  expect(urls.some(url => url.includes('related_only=true'))).toBe(true)
  expect(urls.some(url => url.includes('/native-results'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  await waitFor(() => expect(urls.some(url => url.includes('offset=50'))).toBe(true))
  fireEvent.click(screen.getByText('更多来源'))
  fireEvent.click(await screen.findByRole('checkbox', { name: '显示项目内其它结果' }))
  expect(await screen.findByRole('checkbox', { name: /当前任务结果/ })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: /项目其它结果/ })).not.toBeChecked()
  expect(urls.some(url => url.includes('related_only=false') && url.includes('offset=0'))).toBe(true)
  expect(urls.some(url => url.includes('related_only=false') && url.includes('offset=50'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '继续手动处理' }))
  expect(writes).toHaveLength(0)
  expect(screen.getByText('来自 Agent · 任务')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '带回结果' })).toBeEnabled()
})

test('a lost manual return reply replays the same fixed request after reload and server manual state changes', async () => {
  let manual = true; const commands: Record<string, unknown>[] = []
  const task = () => ({ id: 'task', workspace_id: 'wtest', title: '任务', manual, revision: manual ? 4 : 5, selected_assets: ['selected'] })
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (input.includes('/manual-return')) {
      commands.push(JSON.parse(String(init?.body))); manual = false
      if (commands.length === 1) throw new Error('人工带回回包未知')
      return { ok: true, json: async () => task() }
    }
    if (input.includes('/manual-results')) return { ok: true, json: async () => ({ items: [{ title: '固定结果', reference: ref, required_assets: [] }], next_offset: null, unavailable: [] }) }
    return { ok: true, json: async () => task() }
  }))
  const first = mount(<ManualReturn />)
  fireEvent.click(await screen.findByRole('button', { name: '带回结果' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /固定结果/ }))
  fireEvent.click(screen.getByRole('button', { name: '返回 Agent' }))
  expect(await screen.findByText('人工带回回包未知')).toBeInTheDocument()
  expect(sessionStorage.getItem('clear-manual-return:task')).not.toBeNull()
  await first.navigate('/other?workspace=wtest&task_return=task')
  cleanup()
  const second = mount(<ManualReturn />)
  const retry = await screen.findByRole('button', { name: '核实原带回' })
  await waitFor(() => expect(retry).toBeEnabled()); fireEvent.click(retry)
  await waitFor(() => expect(commands).toHaveLength(2))
  expect(commands[1]).toEqual(commands[0])
  expect(commands[1]).toMatchObject({ expected_revision: 4, references: [ref] })
  await waitFor(() => expect(second.state.location.search).toContain('continue='))
  expect(sessionStorage.getItem('clear-manual-return:task')).toBeNull()
})
