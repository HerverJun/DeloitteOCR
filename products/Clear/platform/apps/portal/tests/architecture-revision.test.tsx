import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { act, cleanup, configure, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AgentMessage } from '../src/features/agent/AgentMessage'
import { AgentWorkbench } from '../src/features/agent/AgentWorkbench'
import { DirectImageTextButton } from '../src/features/agent/DirectImageTextButton'
import { DirectPdfTextButton } from '../src/features/agent/DirectPdfTextButton'
import { ReadableText } from '../src/features/agent/ReadableText'
import { RunStatus } from '../src/features/agent/RunStatus'
import { useAgentEvents } from '../src/features/agent/useAgentEvents'
import { applyConversationEvents, applyJobSnapshot, createProjection, projectConversation, type MessageSegment } from '../src/features/agent/conversationProjection'
import { createWorkbenchView } from '../src/features/agent/workbenchView'
import type { AgentEvent, AgentRef } from '../src/features/agent/api'

vi.mock('../src/App', () => ({ useWorkspaces: () => ({ data: [{ workspace_id: 'w', name: '项目' }] }) }))
beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); FakeSource.instances = [] })
const ref: AgentRef = { kind: 'job', workspace_id: 'w', application: 'ocr', parent_id: 'link', object_id: 'native-original', revision: 1 }
const event = (seq: number, payload: Record<string, unknown>): AgentEvent => ({ seq, kind: 'job_progress', run_id: 'r', payload: { generation: 1, job_ref: ref, ...payload }, created: '2026-10-17T01:00:00Z' })
const response = (value: unknown) => ({ ok: true, json: async () => value })
const jobs = (projection: ReturnType<typeof createProjection>) => projection.items.flatMap(item => item.type === 'activity' ? item.jobs : [])

test('terminal native receipts survive late events and stale snapshots without moving the SSE cursor', () => {
  let projection = projectConversation([event(1, { status: 'running', stage: '识别中' })])
  projection = applyJobSnapshot(projection, { sampled_at: '2026-10-17T01:00:02Z', jobs: [{ job_ref: ref, status: 'succeeded', stage: '识别完成', run_id: 'r', generation: 1, progress_done: 1, progress_total: 1 }] })
  projection = applyConversationEvents(projection, [event(3, { status: 'running', stage: '迟到识别中' }), event(2, { status: 'queued', stage: '排队中' })])
  projection = applyJobSnapshot(projection, { sampled_at: '2026-10-17T01:00:01Z', jobs: [{ job_ref: ref, status: 'running', stage: '过期快照' }] })
  expect(jobs(projection)).toHaveLength(1)
  expect(jobs(projection)[0]).toMatchObject({ status: 'succeeded', stage: '识别完成' })
  expect(projection.events.map(item => item.seq)).toEqual([1, 2, 3])
  expect(createWorkbenchView(undefined, [], projection).finishedJobs).toHaveLength(1)
})

test('a failed authoritative read explicitly removes certainty and active animation', () => {
  const projection = applyJobSnapshot(projectConversation([event(1, { status: 'running', stage: '识别中', progress_done: 1, progress_total: 2 })]))
  const job = createWorkbenchView(undefined, [], projection).currentJobs[0]
  render(<RunStatus activeStatus="waiting_job" submitting={false} subscription={{ status: 'reconnecting', error: '' }} reconnect={() => {}} job={job} />)
  expect(screen.getAllByRole('status')[0]).toHaveTextContent('状态暂不可核实')
  expect(document.querySelector('[data-animated="true"]')).not.toBeInTheDocument()
  expect(jobs(projection)[0].status).toBe('running') // historical receipt remains available
})

test('job observations use the latest run receipt without comparing generations across different runs', () => {
  const first = { ...event(1, { generation: 5, status: 'failed', stage: '历史尝试' }), run_id: 'old-run' }
  const next = { ...event(2, { generation: 1, status: 'succeeded', stage: '已核实完成' }), run_id: 'new-run' }
  const late = { ...event(3, { generation: 1, status: 'running', stage: '迟到运行事件' }), run_id: 'new-run' }
  const projection = projectConversation([first, next, late])
  expect(createWorkbenchView(undefined, [], projection).finishedJobs[0]).toMatchObject({ status: 'succeeded', stage: '已核实完成' })
  expect(createWorkbenchView(undefined, [], projection).currentJobs).toEqual([])
})

class FakeSource {
  static instances: FakeSource[] = []
  readyState = 1; onopen: (() => void) | null = null; onerror: (() => void) | null = null; onmessage = null
  constructor(public url: string) { FakeSource.instances.push(this) }
  close() { this.readyState = 2 }
}
function Subscription({ enabled = true }: { enabled?: boolean }) {
  const { projection } = useAgentEvents('w', 's', enabled)
  return <div>{jobs(projection).map(job => <span key={job.id}>{job.observation === 'unavailable' ? '状态暂不可核实' : job.status}</span>)}</div>
}

test('refresh and disconnected SSE read authoritative jobs without any tool or model write', async () => {
  const requests: { path: string; method?: string }[] = []
  let readable = true
  vi.stubGlobal('EventSource', FakeSource)
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    requests.push({ path, method: init?.method })
    if (path.endsWith('/jobs')) {
      if (!readable) throw new Error('snapshot unreachable')
      return response({ sampled_at: '2026-10-17T01:00:02Z', jobs: [{ job_ref: ref, run_id: 'r', status: 'succeeded', stage: '已完成' }] })
    }
    return response({ items: [event(1, { status: 'running', stage: '识别中' })], next_cursor: 1 })
  }))
  const first = render(<Subscription />)
  expect(await screen.findByText('succeeded')).toBeVisible()
  await waitFor(() => expect(FakeSource.instances).toHaveLength(1))
  readable = false; act(() => FakeSource.instances[0].onerror?.())
  expect(await screen.findByText('状态暂不可核实')).toBeVisible()
  await waitFor(() => expect(requests.filter(item => item.path.endsWith('/jobs')).length).toBeGreaterThan(1))
  readable = true; act(() => FakeSource.instances[0].onopen?.())
  expect(await screen.findByText('succeeded')).toBeVisible()
  first.unmount(); render(<Subscription />)
  expect(await screen.findByText('succeeded')).toBeVisible()
  expect(requests.every(item => !item.method)).toBe(true)
  expect(requests.some(item => item.path.includes('after=1'))).toBe(true)
})

test('disabling the neutral runtime closes SSE and marks cached running jobs unverified without another request', async () => {
  const requests: string[] = []
  vi.stubGlobal('EventSource', FakeSource)
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    requests.push(path)
    return response(path.endsWith('/jobs') ? { sampled_at: '2026-10-17T01:00:00Z', jobs: [{ job_ref: ref, run_id: 'r', status: 'running', stage: '识别中' }] } : { items: [event(1, { status: 'running', stage: '识别中' })], next_cursor: 1 })
  }))
  const view = render(<Subscription />)
  expect(await screen.findByText('running')).toBeVisible()
  await waitFor(() => expect(FakeSource.instances).toHaveLength(1))
  const requestCount = requests.length
  view.rerender(<Subscription enabled={false} />)
  expect(await screen.findByText('状态暂不可核实')).toBeVisible()
  expect(FakeSource.instances[0].readyState).toBe(2)
  expect(requests).toHaveLength(requestCount)
})

test('lost direct acceptance recovers the exact operation and session after reload', async () => {
  const writes: { path: string; body: Record<string, unknown> }[] = [], accepted = vi.fn()
  let lost = false
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)); writes.push({ path, body })
    if (path.endsWith('/agent/sessions')) return response({ id: 'saved-session' })
    if (!lost) { lost = true; throw new Error('accepted response lost') }
    return response({ id: 'original-run', driver: 'image-text-v1' })
  }))
  const first = render(<DirectImageTextButton workspace="w" selectedAssets={['image-original']} onAccepted={accepted} />)
  fireEvent.click(screen.getByRole('button', { name: '提取所选图片文字' }))
  await screen.findByText('accepted response lost')
  first.unmount()
  render(<DirectImageTextButton workspace="w" selectedAssets={['different-image']} disabled onAccepted={accepted} />)
  fireEvent.click(screen.getByRole('button', { name: '核实原文字提取请求' }))
  await waitFor(() => expect(accepted).toHaveBeenCalledWith('saved-session'))
  expect(writes.filter(item => item.path.endsWith('/agent/sessions'))).toHaveLength(1)
  const direct = writes.filter(item => item.path.endsWith('/direct/image-text'))
  expect(direct).toHaveLength(2); expect(direct[1]).toEqual(direct[0])
  expect(localStorage.getItem('clear-direct-image-text:w:new')).toBeNull()
})

test('Agent disabled still permits the explicit selected-image operation without a model connection', async () => {
  const requests: { path: string; body?: Record<string, unknown> }[] = []
  vi.stubGlobal('EventSource', FakeSource)
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    requests.push({ path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (path.endsWith('/health')) return response({ enabled: false, direct_enabled: true, reason: 'Agent 停用' })
    if (path.endsWith('/task')) return response({ id: 's', session_id: 's', workspace_id: 'w', title: '图片提取', status: 'draft', revision: 1, scope_revision: 7, selected_assets: ['image'], initial_refs: [], plan: [], runs: [], job_refs: [], artifact_refs: [], result_refs: [] })
    if (path.endsWith('/agent/sessions/s')) return response({ id: 's', workspace_id: 'w', runs: [] })
    if (path.includes('/events?')) return response({ items: [], next_cursor: 0 })
    if (path.endsWith('/jobs')) return response({ jobs: [], sampled_at: '2026-10-17T01:00:00Z' })
    if (path.endsWith('/direct/image-text')) return response({ id: 'r', driver: 'image-text-v1' })
    return response({ items: [] })
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><RouterProvider router={createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }], { initialEntries: ['/agent?workspace=w&session=s'] })} /></QueryClientProvider>)
  fireEvent.click(screen.getByText('更多', { selector: 'summary' }))
  fireEvent.click(await screen.findByRole('button', { name: '直接提取文字' }))
  const button = await screen.findByRole('button', { name: '提取所选图片文字' })
  await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button)
  await waitFor(() => expect(requests.some(item => item.path.endsWith('/direct/image-text'))).toBe(true))
  expect(requests.find(item => item.path.endsWith('/direct/image-text'))?.body).toMatchObject({ expected_scope_revision: 7 })
  expect(requests.some(item => item.path.endsWith('/connection') || item.path.endsWith('/messages'))).toBe(false)
  await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).toBeDisabled())
  fireEvent.change(screen.getByRole('textbox', { name: '任务或问题' }), { target: { value: '帮我识别一下这张图片里的文字' } })
  expect(screen.getByRole('button', { name: '发送' })).toBeEnabled()
})

test('a late direct acceptance never navigates the user back into a project they left', async () => {
  const accepted = vi.fn()
  let finish!: (value: unknown) => void
  vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve })))
  const view = render(<DirectImageTextButton workspace="original" sessionId="s" selectedAssets={['image']} onAccepted={accepted} />)
  fireEvent.click(screen.getByRole('button', { name: '提取所选图片文字' }))
  view.rerender(<DirectImageTextButton workspace="other" sessionId="different" selectedAssets={['different-image']} onAccepted={accepted} />)
  await act(async () => finish(response({ id: 'accepted-original-run' })))
  expect(accepted).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: '提取所选图片文字' })).toBeEnabled()
})

test('an unavailable neutral runtime disables direct actions and makes no session or model request', async () => {
  const requests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => { requests.push(path); return response({ enabled: false, direct_enabled: false, reason: '中立运行时不可用' }) }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><RouterProvider router={createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }], { initialEntries: ['/agent?workspace=w&assets=image'] })} /></QueryClientProvider>)
  expect(await screen.findByText('执行服务当前不可用')).toBeVisible()
  fireEvent.click(screen.getByText('更多', { selector: 'summary' }))
  fireEvent.click(screen.getByRole('button', { name: '直接提取文字' }))
  expect(await screen.findByRole('button', { name: '提取所选图片文字' })).toBeDisabled()
  expect(requests).toEqual(['/api/platform/v1/agent/health'])
})

test('explicit PDF page selection validates duplicates and freezes mode and pages after a lost response', async () => {
  const requests: Record<string, unknown>[] = [], accepted = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    expect(path.endsWith('/direct/pdf-text')).toBe(true)
    requests.push(JSON.parse(String(init?.body)))
    if (requests.length === 1) throw new Error('PDF acceptance unknown')
    return response({ id: 'original-pdf-run' })
  }))
  render(<DirectPdfTextButton workspace="w" sessionId="s" selectedAssets={['pdf']} scopeRevision={3} onAccepted={accepted} />)
  fireEvent.click(screen.getByRole('button', { name: 'PDF 指定页文字提取' }))
  fireEvent.change(await screen.findByRole('textbox', { name: 'PDF 页码' }), { target: { value: '2,2' } })
  expect(screen.getByRole('button', { name: '提取指定页文字' })).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox', { name: 'PDF 页码' }), { target: { value: '2,5' } })
  fireEvent.click(screen.getByRole('button', { name: '提取指定页文字' }))
  await screen.findByText('PDF acceptance unknown')
  fireEvent.change(screen.getByRole('textbox', { name: 'PDF 页码' }), { target: { value: '1' } })
  fireEvent.click(screen.getByRole('button', { name: '核实原文字提取请求' }))
  await waitFor(() => expect(accepted).toHaveBeenCalledWith('s'))
  expect(requests).toHaveLength(2); expect(requests[1]).toEqual(requests[0])
  expect(requests[0]).toMatchObject({ page_numbers: [2, 5], mode: 'auto', engine: 'ppocr', expected_scope_revision: 3 })
})

test('long literal OCR text paginates and copies every Unicode character from the pinned revision', async () => {
  const text = '# 原文\t 001.20\n' + '😀汉'.repeat(4600), characters = Array.from(text), head = characters.slice(0, 8000).join(''), tail = characters.slice(8000).join('')
  const resultRef: AgentRef = { ...ref, kind: 'result', object_id: 'result', revision: 'fixed-v1' }
  const writeText = vi.fn(async () => {})
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const fetcher = vi.fn(async (path: string) => {
    const url = new URL(path, 'http://localhost')
    expect(JSON.parse(url.searchParams.get('reference')!)).toEqual(resultRef)
    expect(url.searchParams.get('offset')).toBe('8000')
    return response({ text: tail, text_offset: 8000, text_next_offset: null, text_total_chars: characters.length, text_sha256: 'pinned-hash' })
  })
  vi.stubGlobal('fetch', fetcher)
  const delivery: AgentEvent = { seq: 1, run_id: 'r', kind: 'delivery', created: '2026-10-17T01:00:00Z', payload: { status: 'succeeded', content: '文字已提取', delivery: { data: { readable_text: [{ result_id: 'result', revision: 'fixed-v1', reference: resultRef, text: head, text_offset: 0, text_next_offset: 8000, text_total_chars: characters.length, text_truncated: true, text_sha256: 'pinned-hash', download_url: '/api/text.txt' }] } } } }
  const item = projectConversation([delivery]).items[0] as MessageSegment
  render(<MemoryRouter><AgentMessage item={item} taskId="s" /></MemoryRouter>)
  expect(screen.getByLabelText('识别全文')).toHaveTextContent('后续内容可继续读取')
  expect(screen.getByRole('link', { name: '下载 TXT' })).toHaveAttribute('href', '/api/text.txt')
  fireEvent.click(screen.getByRole('button', { name: '下一段' }))
  await screen.findByText(/第 2 段/)
  await waitFor(() => expect(screen.getByRole('button', { name: /复制全文/ })).not.toHaveClass('ant-btn-loading'))
  fireEvent.click(screen.getByRole('button', { name: '复制全文' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(text))
  expect(fetcher).toHaveBeenCalledOnce()
  expect(screen.queryByRole('heading', { name: /原文/ })).not.toBeInTheDocument()
})

test('a discontinuous or changed long-text page is rejected instead of copying a truncated document', async () => {
  const writeText = vi.fn(async () => {})
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  vi.stubGlobal('fetch', vi.fn(async () => response({ text: 'wrong-version', text_offset: 1, text_next_offset: 2, text_sha256: 'different' })))
  render(<ReadableText taskId="s" result={{ reference: { ...ref, kind: 'result' }, text: 'a', text_offset: 0, text_next_offset: 1, text_total_chars: 2, text_truncated: true, text_sha256: 'original' }} />)
  fireEvent.click(screen.getByRole('button', { name: '复制全文' }))
  expect(await screen.findByText('全文分页未能核对同一结果版本，请重新读取。')).toBeVisible()
  expect(writeText).not.toHaveBeenCalled()
})
