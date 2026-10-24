import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { TaskPanel } from '../src/features/agent/TaskPanel'
import { createWorkbenchView } from '../src/features/agent/workbenchView'
import type { AgentEvent, AgentRef, AuditTask } from '../src/features/agent/api'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(cleanup)

const jobRef: AgentRef = { kind: 'job', application: 'data', workspace_id: 'workspace', object_id: 'native-job', parent_id: 'data-session', revision: 1 }
const artifactRef: AgentRef = { kind: 'artifact', application: 'data', workspace_id: 'workspace', object_id: 'bundle', revision: 1 }
const task = (extra: Partial<AuditTask> = {}): AuditTask => ({ id: 'task', session_id: 'task', workspace_id: 'workspace', title: '多文件台账清洗', goal: '保留编号前导零与金额文本，完成清洗和来源核验。', revision: 1,
  scope_revision: 2, status: 'running', manual: false, pending_scope: null, selected_assets: ['source'], initial_refs: [], plan: [], runs: [{ id: 'run', status: 'running', error: null }],
  result_refs: [], artifact_refs: [], job_refs: [], ...extra })
const event = (seq: number, kind: string, payload: Record<string, unknown>, run_id = 'run'): AgentEvent => ({ seq, kind, payload, run_id, created: '2026-10-16T06:00:00Z' })
const callbacks = () => ({ rename: vi.fn(), background: vi.fn(), materials: vi.fn(), skills: vi.fn(), busy: false, hasProject: true })

test('completed native output remains downloadable while analysis failed', () => {
  const events = [event(1, 'result_available', { goal_complete: false, result: { result_refs: [], artifact_refs: [artifactRef],
    data: { finalized: false, delivery: [{ bundle_id: 'original-bundle', title: '原文', files: [{ name: 'ocr-text.txt', download_url: '/download/original' }] }] } } }),
    event(2, 'error', { code: 'provider_unavailable', message: '模型暂不可用；点继续' })]
  render(<MemoryRouter><TaskPanel task={task({ status: 'failed' })} events={events} {...callbacks()} /></MemoryRouter>)
  const results = screen.getByLabelText('任务成果')
  expect(within(results).getByRole('link', { name: /ocr-text.txt/ })).toHaveAttribute('href', '/download/original')
  expect(results).toHaveTextContent('后续分析未完成，请查看具体原因。')
  expect(results).not.toHaveTextContent('已完成交付')
})

test.each([
  ['running', '后续分析正在继续。'],
  ['waiting_job', '正在等待工具或资源，随后自动继续。'],
  ['waiting_user', '等待你补充信息后继续。'],
  ['paused', '任务已暂停。'],
  ['cancelled', '后续处理已停止。'],
  ['needs_verification', '后续操作需先核实结果。'],
])('available files remain usable while %s describes the actual next step', (status, notice) => {
  const events = [event(1, 'result_available', { result: { artifact_refs: [artifactRef], data: { delivery: [
    { bundle_id: 'original-bundle', title: '原文', files: [{ name: 'ocr-text.txt', download_url: '/download/original' }] },
  ] } } })]
  render(<MemoryRouter><TaskPanel task={task({ status })} events={events} {...callbacks()} /></MemoryRouter>)
  const results = screen.getByLabelText('任务成果')
  expect(within(results).getByRole('link', { name: /ocr-text.txt/ })).toHaveAttribute('href', '/download/original')
  expect(results).toHaveTextContent(notice)
  expect(results).not.toHaveTextContent('后续分析待继续')
})

test('an observed automatic wait never overrides a user pause or cancellation', () => {
  const events = [event(1, 'result_available', { result: { artifact_refs: [artifactRef] } }),
    event(2, 'agent_activity', { version: 1, generation: 1, phase: 'waiting_job' })]
  const renderTask = (status: string) => <MemoryRouter><TaskPanel task={task({ status })} events={events} {...callbacks()} /></MemoryRouter>
  const mounted = render(renderTask('running'))
  expect(screen.getByLabelText('任务成果')).toHaveTextContent('正在等待工具或资源，随后自动继续。')
  mounted.rerender(renderTask('paused'))
  expect(screen.getByLabelText('任务成果')).toHaveTextContent('任务已暂停。')
  expect(screen.getByLabelText('任务成果')).not.toHaveTextContent('自动继续')
  mounted.rerender(renderTask('cancelled'))
  expect(screen.getByLabelText('任务成果')).toHaveTextContent('后续处理已停止。')
  expect(screen.getByLabelText('任务成果')).not.toHaveTextContent('自动继续')
})

test('a pause receipt stops the continuation notice before the task snapshot refreshes', () => {
  const events = [event(1, 'result_available', { result: { artifact_refs: [artifactRef] } }),
    event(2, 'agent_activity', { version: 1, generation: 1, phase: 'paused' })]
  render(<MemoryRouter><TaskPanel task={task()} events={events} {...callbacks()} /></MemoryRouter>)
  expect(screen.getByLabelText('任务成果')).toHaveTextContent('任务已暂停。')
  expect(screen.getByLabelText('任务成果')).not.toHaveTextContent('正在继续')
})

test('unreviewed delivery stays downloadable without implying approval is required', () => {
  const events = [event(1, 'delivery', { status: 'succeeded', content: '正式成果已完成', delivery: { artifact_refs: [artifactRef], data: { draft: true, review_state: 'unreviewed', delivery: [] } } })]
  render(<MemoryRouter><TaskPanel task={task({ status: 'succeeded' })} events={events} {...callbacks()} /></MemoryRouter>)
  expect(screen.getByLabelText('任务成果')).toHaveTextContent('未复核 · 可下载')
  expect(screen.queryByText(/待用户核准/)).not.toBeInTheDocument()
  expect(screen.getByLabelText('任务成果')).not.toHaveTextContent('已完成交付')
  const formal = createWorkbenchView(task({ status: 'succeeded' }), [...events, event(2, 'delivery', { status: 'succeeded', content: '正式核准成果', delivery: { artifact_refs: [artifactRef], data: { draft: false, review_state: 'not_required_or_confirmed', delivery: [] } } })])
  expect(formal.draftDelivery).toBe(false)
})

test('latest native receipts merge by job identity across runs and unavailable counters remain hidden', () => {
  const events = [event(1, 'job_progress', { generation: 1, job_ref: jobRef, status: 'running', stage: '读取台账', progress_done: 10, progress_total: 100 }, 'old'),
    event(2, 'job_progress', { generation: 1, job_ref: jobRef, status: 'succeeded', stage: '完成', progress_done: 100, progress_total: 100 }, 'old'),
    event(3, 'job_progress', { generation: 2, job_ref: { ...jobRef, revision: 2 }, status: 'unknown', observation: 'unavailable', progress_done: 100, progress_total: 100 })]
  const view = createWorkbenchView(task({ job_refs: [jobRef] }), events)
  expect(view.currentJobs).toHaveLength(1)
  expect(view.finishedJobs).toHaveLength(0)
  expect(view.currentJobs[0]).toMatchObject({ seq: 3, observation: 'unavailable', jobRef: { revision: 2 } })
  render(<MemoryRouter><TaskPanel task={task()} events={events} {...callbacks()} /></MemoryRouter>)
  const jobs = screen.getByLabelText('当前作业')
  expect(jobs).toHaveTextContent('状态暂不可核实')
  expect(within(jobs).queryByText(/已处理/)).not.toBeInTheDocument()
  expect(within(jobs).queryByRole('progressbar')).not.toBeInTheDocument()
  expect(screen.getByLabelText('任务阻碍')).toHaveTextContent('状态暂不可核实')
})

test('stale generations and historical failures do not become the new run blocker', () => {
  const events = [event(1, 'agent_activity', { version: 1, generation: 1, phase: 'failed', reason: '旧任务失败' }, 'old'),
    event(2, 'error', { message: '旧错误' }, 'old'), event(3, 'agent_activity', { version: 1, generation: 2, phase: 'preparing_request' }),
    event(4, 'job_progress', { generation: 1, job_ref: jobRef, status: 'failed' }), event(5, 'error', { generation: 1, message: '迟到的旧错误' })]
  expect(createWorkbenchView(task(), events).blockers).toEqual([])
  expect(createWorkbenchView(task(), events).currentJobs).toEqual([])
  const paused = createWorkbenchView(task({ status: 'paused' }), [...events, event(6, 'agent_activity', { version: 1, generation: 2, phase: 'paused', reason: '同一动作连续三次没有进展' })])
  expect(paused.blockers).toEqual([{ key: 'run', title: '任务已暂停', detail: '同一动作连续三次没有进展' }])
})

test('staged checks and files stay distinct from final delivery and preserve source links', () => {
  const events = [event(1, 'delivery_check', { phase: 'staged', finalized: false, message: '本批文件已保存，继续处理下一份', checks: [{ name: 'row_count', status: 'passed', message: '100000 行' }, { name: 'csv_readback', status: 'measured', message: '已完整读回，测量值不代表已匹配未声明的要求' }] }),
    event(2, 'delivery', { status: 'partial', content: '部分成果', delivery: { artifact_refs: [artifactRef], data: { delivery: [{ bundle_id: 'bundle', title: '清洗结果', files: [{ name: '长文件名称_含来源字段与前导零_清洗结果.csv', download_url: '/api/platform/v1/artifacts/bundle/download/result.csv' }, { name: '不安全链接', download_url: 'javascript:alert(1)' }] }], limitations: ['第二份台账尚未完成'] } } })]
  const view = createWorkbenchView(task({ status: 'partial', artifact_refs: [artifactRef] }), events)
  expect(view.deliveryRefs).toEqual([artifactRef])
  expect(view.bundles[0].files).toHaveLength(1)
  render(<MemoryRouter><TaskPanel task={task({ status: 'partial' })} events={events} {...callbacks()} /></MemoryRouter>)
  expect(screen.getByLabelText('任务交付检查')).toHaveTextContent('本批成果已核验，等待最终交付')
  expect(screen.getByLabelText('任务交付检查')).not.toHaveTextContent('交付检查通过')
  expect(screen.getByLabelText('任务交付检查')).toHaveTextContent('行数100000 行')
  expect(within(screen.getByLabelText('任务交付检查')).getByLabelText('已测量')).toBeInTheDocument()
  const results = screen.getByLabelText('任务成果')
  expect(results).toHaveTextContent('部分成果')
  expect(results).toHaveTextContent('第二份台账尚未完成')
  expect(within(results).getByRole('link', { name: '长文件名称_含来源字段与前导零_清洗结果.csv' })).toHaveAttribute('href', '/api/platform/v1/artifacts/bundle/download/result.csv')
  expect(within(results).getByRole('link', { name: /数据 · 成果文件/ })).toHaveAttribute('href', '/artifacts?workspace=workspace&task_return=task&bundle=bundle')
  expect(within(results).queryByRole('link', { name: '不安全链接' })).not.toBeInTheDocument()
})

test('current plan step is explicit and project materials skills and editing remain reachable', () => {
  const props = callbacks()
  render(<MemoryRouter><TaskPanel task={task({ plan: [{ key: 'read', title: '读取资料', purpose: '确认范围', status: 'completed', depends_on: [], result_refs: [] },
    { key: 'clean', title: '清洗台账并核对字段', purpose: '保留前导零', status: 'running', depends_on: ['read'], result_refs: [] }] })} {...props} /></MemoryRouter>)
  const plan = screen.getByLabelText('任务计划')
  expect(plan).toHaveTextContent('1 / 2')
  expect(within(plan).getByText('清洗台账并核对字段').closest('li')).toHaveAttribute('aria-current', 'step')
  expect(within(plan).getByText('读取资料').closest('li')).not.toHaveAttribute('aria-current')
  fireEvent.click(screen.getByRole('button', { name: '1 份资料' }))
  fireEvent.click(screen.getByRole('button', { name: '项目背景与口径' }))
  fireEvent.click(screen.getByRole('button', { name: 'Agent 技能' }))
  expect(props.materials).toHaveBeenCalledOnce(); expect(props.background).toHaveBeenCalledOnce(); expect(props.skills).toHaveBeenCalledOnce()
  expect(screen.getByLabelText('当前任务').querySelector('.ant-typography-edit')).toBeInTheDocument()
})

test('run details retain the actual plan without making synchronization a user task', () => {
  const actualPlan = [{ key: 'read', title: '读取资料', purpose: '确认范围', status: 'completed', depends_on: [], result_refs: [] },
    { key: 'deliver', title: '核验交付文件', purpose: '读取实际文件', status: 'running', depends_on: ['read'], result_refs: [] }]
  const renderTask = (status: string, plan = actualPlan) => <MemoryRouter><TaskPanel task={task({ status, plan })} {...callbacks()} /></MemoryRouter>
  const mounted = render(renderTask('succeeded'))
  const plan = screen.getByLabelText('任务计划')
  expect(screen.queryByText('成果已交付，计划状态待同步。')).not.toBeInTheDocument()
  expect(plan.closest('details')).not.toHaveAttribute('open')
  expect(plan).toHaveTextContent('1 / 2')
  expect(within(plan).getByText('核验交付文件').closest('li')).toHaveAttribute('data-state', 'current')
  expect(actualPlan[1].status).toBe('running')
  mounted.rerender(renderTask('running'))
  expect(screen.queryByText('成果已交付，计划状态待同步。')).not.toBeInTheDocument()
  mounted.rerender(renderTask('succeeded', actualPlan.map(item => ({ ...item, status: 'completed' }))))
  expect(screen.queryByText('成果已交付，计划状态待同步。')).not.toBeInTheDocument()
})

test('unobserved linked jobs and waiting input show honest state without synthetic progress', () => {
  const data = task({ status: 'waiting_user', runs: [{ id: 'run', status: 'waiting_user', error: null }], job_refs: [jobRef] })
  const events = [event(1, 'question', { question: '金额字段应保留几位小数？', decision_id: 'decision', revision: 1 })]
  render(<MemoryRouter><TaskPanel task={data} events={events} {...callbacks()} /></MemoryRouter>)
  expect(screen.getByLabelText('当前作业')).toHaveTextContent('已关联，等待状态回执')
  expect(screen.getByLabelText('任务阻碍')).toHaveTextContent('金额字段应保留几位小数？')
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})


test('explicit review requirement waits for the user while the file remains available', () => {
  const events = [event(1, 'delivery', { status: 'succeeded', delivery: { artifact_refs: [artifactRef], data: { draft: true, review_state: 'unreviewed', delivery: [] } } })]
  render(<MemoryRouter><TaskPanel task={task({ status: 'waiting_user', delivery_policy: 'reviewed' })} events={events} {...callbacks()} /></MemoryRouter>)
  expect(screen.getByLabelText('任务成果')).toHaveTextContent('未复核 · 待用户核准')
})
