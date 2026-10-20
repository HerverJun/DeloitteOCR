import { useMemo } from 'react'
import { CheckOutlined, DownloadOutlined, FileOutlined, InfoCircleOutlined, ProfileOutlined } from '@ant-design/icons'
import { Button, Empty, Input, List, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { AuditTask, AgentRef, AgentEvent } from './api'
import { referencePath } from './referencePath'
import { createWorkbenchView, type WorkbenchJob, type WorkbenchView } from './workbenchView'
import type { ConversationProjection } from './conversationProjection'
import './task-panel.css'

import { statusLabels } from './statusLabels'
export { statusLabels } from './statusLabels'

export { referencePath } from './referencePath'

export function referenceReviewLabel(ref: AgentRef) {
  if (ref.coverage?.draft === true) return '草稿 · 未复核'
  if (ref.application === 'multimodal' && ref.kind === 'result') return ref.coverage?.reviewed === true ? '人工复核已保存' : '未复核'
  return undefined
}
export function ReferenceLinks({ refs, taskId }: { refs: AgentRef[]; taskId?: string }) {
  return <div className="audit-reference-links">{refs.map(ref => <span className="audit-reference-item" key={`${ref.kind}-${ref.object_id}-${ref.revision}`}><Link to={referencePath(ref, taskId)}>
    {({ data: '数据', ocr: '识别', classification: '分类', forensics: '取证', multimodal: '专项分析', platform: '资料' })[ref.application || 'platform']} · {ref.kind === 'artifact' ? '成果文件' : ref.kind === 'table' ? '表格' : ref.kind === 'job' ? '作业' : '来源'} 
    {referenceReviewLabel(ref) && <span className="audit-reference-review-state">{referenceReviewLabel(ref)}</span>}
  </Link><details className="reference-identity"><summary>来源详情</summary><div><span>固定版本</span><code>{String(ref.revision)}</code>{ref.sha256 && <><span>SHA256</span><code>{ref.sha256}</code></>}</div></details></span>)}</div>
}

const applicationLabels: Record<string, string> = { data: '数据处理', ocr: '文字识别', classification: '智能分类', forensics: '图像取证', multimodal: '专项分析', platform: '平台作业' }
const jobStatusLabels: Record<string, string> = { ...statusLabels, queued: '排队中', pending: '等待执行', success: '已完成', error: '未完成', unknown: '状态未确认' }
const emptyEvents: AgentEvent[] = []
function TaskJob({ job, taskId }: { job: WorkbenchJob; taskId?: string }) {
  const unavailable = job.observation === 'unavailable'
  const hasProgress = !unavailable && typeof job.processed === 'number' && Number.isFinite(job.processed) && job.processed >= 0
  const hasTotal = hasProgress && typeof job.total === 'number' && Number.isFinite(job.total) && job.total > 0
  const sampled = job.sampledAt ? new Date(job.sampledAt) : undefined
  return <div className="audit-task-job" data-state={['succeeded', 'success', 'completed', 'failed', 'cancelled', 'partial'].includes(job.status) ? 'finished' : 'active'}>
    <div className="audit-task-job-heading"><i className="audit-task-job-dot" aria-hidden="true" /><span>{applicationLabels[job.jobRef.application || 'platform'] || '关联作业'}</span></div>
    <p className="audit-task-job-title">{unavailable ? '状态暂不可核实' : !job.observed ? '已关联，等待状态回执' : job.stage || jobStatusLabels[job.status] || job.status || '等待状态回执'}</p>
    {!unavailable && job.stage && job.status && <span className="audit-task-job-count">{jobStatusLabels[job.status] || job.status}</span>}
    {hasProgress && <span className="audit-task-job-count">已处理 {job.processed!.toLocaleString('zh-CN')}{hasTotal ? ` / ${job.total!.toLocaleString('zh-CN')}` : ''}</span>}
    {hasTotal && <progress aria-label="作业实际进度" value={job.processed} max={job.total} />}
    {sampled && Number.isFinite(sampled.getTime()) && <time className="audit-task-job-updated" dateTime={job.sampledAt}>{unavailable ? '尝试读取于' : '更新于'} {sampled.toLocaleTimeString('zh-CN', { hour12: false })}</time>}
    <ReferenceLinks refs={[job.jobRef]} taskId={taskId} />
  </div>
}

export function TaskPanel({ task, events = emptyEvents, projection, view: providedView, rename, background, materials, skills, tools, busy, hasProject }: {
  task?: AuditTask; events?: AgentEvent[]; projection?: ConversationProjection; view?: WorkbenchView; rename: (name: string) => void; background: () => void; materials: () => void; skills: () => void; tools?: () => void; busy: boolean; hasProject: boolean
}) {
  const view = useMemo(() => providedView || createWorkbenchView(task, events, projection), [providedView, task, events, projection])
  const check = view.deliveryCheck
  const completed = task?.plan.filter(item => ['completed', 'succeeded', 'success'].includes(item.status)).length || 0
  const unsyncedPlan = task?.status === 'succeeded' && task.plan.some(item => ['pending', 'running', 'in_progress', 'active', 'executing'].includes(item.status))
  return <aside className="audit-task-panel audit-task-panel-refined" aria-label="当前任务">
    <div className="audit-panel-heading"><Typography.Text strong>当前任务</Typography.Text><Tag>{task ? statusLabels[task.status] || task.status : '尚未开始'}</Tag></div>
    {task ? <><section className="audit-task-overview" aria-label="任务目标">
      <Typography.Title level={4} className="audit-task-title" editable={{ onChange: rename }}>{task.title}</Typography.Title>
      <Typography.Paragraph className="audit-task-goal">{task.goal || '开始对话后，目标会保存在这里。'}</Typography.Paragraph>
      <div className="audit-task-meta"><span>当前资料</span><Button type="link" onClick={materials}>{task.selected_assets.length} 份资料</Button></div>
      {tools && !!task.authorized_reference_ids?.length && <Button type="link" onClick={tools}>人脸参考已授权 {task.authorized_reference_ids?.length || 0} 项</Button>}
      {tools && (task.delivery_policy !== 'auto' && !!task.delivery_policy || task.delivery_policy_resolution?.require_review) && <Button type="link" onClick={tools}>交付要求：{task.delivery_policy === 'reviewed' || task.delivery_policy_resolution?.require_review ? '正式交付须核准' : task.delivery_policy === 'draft' ? '仅草稿' : '按用户目标判断'}</Button>}
      {!!task.pending_scope && <p className="audit-task-scope-notice">新资料将在下一步处理前加入。</p>}
    </section>
      {!!task.plan.length && <section className="audit-task-section" aria-label="任务计划"><h3 className="audit-task-section-heading">工作安排{!!task.plan.length && <span>{completed} / {task.plan.length}</span>}</h3>
        {unsyncedPlan && <p className="audit-task-plan-notice" role="note">成果已交付，计划状态待同步。</p>}
        {task.plan.length ? <ol className="audit-task-plan">{task.plan.map((item, index) => {
          const complete = ['completed', 'succeeded', 'success'].includes(item.status), current = ['running', 'in_progress', 'active', 'executing'].includes(item.status)
          return <li className="audit-task-plan-item" key={item.key} data-state={complete ? 'complete' : current ? 'current' : ['blocked', 'failed', 'needs_verification'].includes(item.status) ? 'blocked' : 'pending'} aria-current={current ? 'step' : undefined}>
            <span className="audit-task-plan-mark" aria-hidden="true">{complete ? <CheckOutlined /> : index + 1}</span><div className="audit-task-plan-copy">
              <p className="audit-task-plan-title">{item.title}</p><span className="audit-task-plan-status">{statusLabels[item.status] || item.status}</span>
              {item.purpose && <p className="audit-task-plan-purpose">{item.purpose}</p>}<ReferenceLinks refs={item.result_refs} taskId={task.id} />
            </div></li>
        })}</ol> : <p className="audit-task-empty">任务形成计划后，会在这里逐步更新。</p>}
      </section>}
      {(!!view.currentJobs.length || !!view.finishedJobs.length) && <section className="audit-task-section" aria-label="当前作业"><h3 className="audit-task-section-heading">当前作业{!!view.currentJobs.length && <span>{view.currentJobs.length} 项</span>}</h3>
        {view.currentJobs.length ? view.currentJobs.map(job => <TaskJob key={job.id} job={job} taskId={task.id} />) : <p className="audit-task-empty">当前没有等待中的外部作业。</p>}
        {!!view.finishedJobs.length && <details className="audit-task-finished-jobs"><summary>已结束作业 {view.finishedJobs.length} 项</summary>{view.finishedJobs.map(job => <TaskJob key={job.id} job={job} taskId={task.id} />)}</details>}
      </section>}
      {(!!view.blockers.length || task.manual) && <section className="audit-task-section" aria-label="任务阻碍"><h3 className="audit-task-section-heading">阻碍与待办</h3>
        {view.blockers.length ? view.blockers.map(blocker => <div className="audit-task-blocker" key={blocker.key}><strong>{blocker.title}</strong>{blocker.detail && <p>{blocker.detail}</p>}{blocker.reference && <ReferenceLinks refs={[blocker.reference]} taskId={task.id} />}</div>) : <p className="audit-task-empty">当前没有记录到阻碍。</p>}
        {task.manual && <p className="audit-task-scope-notice">已停止 Agent 后续动作。原生作业可继续完成，手动修改后带回最新结果。</p>}
      </section>}
      {check && <section className="audit-task-section" aria-label="任务交付检查"><h3 className="audit-task-section-heading">交付检查</h3>
        {check ? <><div className="audit-task-check-state" data-state={check.staged ? 'staged' : check.phase}>{check.phase === 'passed' && !check.staged ? <CheckOutlined aria-hidden="true" /> : <InfoCircleOutlined aria-hidden="true" />}
          {check.staged ? '本批成果已核验，等待最终交付' : check.phase === 'passed' ? '交付检查通过' : check.phase === 'failed' ? '交付检查未通过' : '正在检查交付成果'}</div>
          {check.message && <p className="audit-task-check-message">{check.message}</p>}
          {!!check.checks.length && <ul className="audit-task-checks">{check.checks.map((item, index) => <li key={`${item.name}:${index}`}><span className="audit-task-check-mark" data-state={item.status} aria-label={item.status === 'passed' ? '通过' : item.status === 'failed' ? '未通过' : item.status === 'measured' ? '已测量' : '待检查'}>{item.status === 'passed' ? '✓' : item.status === 'failed' ? '!' : '·'}</span><span>{item.name}{item.message && <small>{item.message}</small>}</span></li>)}</ul>}
        </> : <p className="audit-task-empty">交付前会检查回执、文件与任务要求。</p>}
      </section>}
      <section className="audit-task-section" aria-label="任务成果"><h3 className="audit-task-section-heading">成果与来源</h3>
        {view.availableResults && !['succeeded', 'answered'].includes(task.status) && <p className="audit-task-scope-notice" role="status">已完成成果可直接读取和下载；后续分析待继续。</p>}
        {view.bundles.length || view.deliveryRefs.length ? <><span className="audit-task-delivery-status" data-state={view.draftDelivery ? 'draft' : task.status}>{view.draftDelivery ? '未复核草稿 · 待用户核准' : task.status === 'succeeded' ? '已完成交付' : task.status === 'partial' ? '部分成果' : task.status === 'needs_verification' ? '待核实成果' : '已保存成果'}</span>
          {view.bundles.map(bundle => <div className="audit-task-bundle" key={bundle.id}><strong className="audit-task-bundle-title">{bundle.title}</strong>{bundle.files.map(file => <a className="audit-task-file" key={file.downloadUrl} href={file.downloadUrl}><FileOutlined aria-hidden="true" /><span className="audit-task-file-name">{file.name}</span><DownloadOutlined aria-hidden="true" /></a>)}</div>)}
          <ReferenceLinks refs={view.deliveryRefs} taskId={task.id} />
          {!!view.limitations.length && <ul className="audit-task-limitations">{view.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>}
        </> : <p className="audit-task-empty">文件与来源会随任务成果保存在这里。</p>}
        <Link className="audit-findings-entry" to={`/findings?workspace=${task.workspace_id}&session=${task.id}`}>查看发现与底稿 →</Link>
      </section>
    </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="目标、资料和执行成果会持续保存在同一项任务中。" />}
    <div className="audit-task-panel-footer"><Button type="text" block icon={<ProfileOutlined aria-hidden="true" />} disabled={busy || !hasProject} onClick={background}>项目背景与口径</Button>
      {tools && <Button type="text" block icon={<InfoCircleOutlined aria-hidden="true" />} onClick={tools}>平台工具与参考授权</Button>}
      <Button type="text" block icon={<InfoCircleOutlined aria-hidden="true" />} onClick={skills}>Agent 技能</Button></div>
  </aside>
}

export function RecentTasks({ tasks, current, choose, keyword, search }: { tasks: AuditTask[]; current: string | null; choose: (id: string) => void; keyword: string; search: (value: string) => void }) {
  return <><Input.Search aria-label="查找任务" placeholder="查找任务" value={keyword} onChange={event => search(event.target.value)} />
    <List dataSource={tasks} locale={{ emptyText: '本项目还没有任务' }} renderItem={item => <List.Item><Button type={current === item.id ? 'primary' : 'text'} onClick={() => choose(item.id)}>{item.title}</Button><Tag>{statusLabels[item.status] || item.status}</Tag></List.Item>} /></>
}
