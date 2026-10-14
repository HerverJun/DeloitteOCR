import { Button, Empty, Input, List, Space, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { AuditTask, AgentRef } from './api'
import { referencePath } from './referencePath'

export const statusLabels: Record<string, string> = { draft: '草稿', answered: '已回复', manual: '人工处理中', running: '处理中', waiting_job: '等待工具完成', waiting_user: '等待补充信息', needs_verification: '需要核实', succeeded: '已完成', partial: '部分成果', cancelled: '已停止', incomplete: '未形成交付', failed: '未完成', pending: '待处理', completed: '已完成', blocked: '待补充', skipped: '已跳过' }

export { referencePath } from './referencePath'

export function ReferenceLinks({ refs, taskId }: { refs: AgentRef[]; taskId?: string }) {
  return <div className="audit-reference-links">{refs.map(ref => <Link key={`${ref.kind}-${ref.object_id}-${ref.revision}`} to={referencePath(ref, taskId)}>
    {({ data: '数据', ocr: '识别', classification: '分类', forensics: '取证', platform: '资料' })[ref.application || 'platform']} · {ref.kind === 'artifact' ? '成果文件' : ref.kind === 'table' ? '表格' : ref.kind === 'job' ? '作业' : '来源'} <small>{String(ref.revision)}</small>
  </Link>)}</div>
}

export function TaskPanel({ task, rename, background, materials, skills, busy, hasProject }: {
  task?: AuditTask; rename: (name: string) => void; background: () => void; materials: () => void; skills: () => void; busy: boolean; hasProject: boolean
}) {
  return <aside className="audit-task-panel" aria-label="当前审计任务">
    <div className="audit-panel-heading"><Typography.Text strong>当前任务</Typography.Text><Tag>{task ? statusLabels[task.status] || task.status : '尚未开始'}</Tag></div>
    {task ? <><Typography.Title level={4} editable={{ onChange: rename }}>{task.title}</Typography.Title>
      <Typography.Paragraph className="audit-task-goal">{task.goal || '先保存目标，或直接开始对话。'}</Typography.Paragraph>
      <div className="audit-task-meta"><span>资料范围 v{task.scope_revision}</span><Button type="link" onClick={materials}>{task.selected_assets.length} 份资料</Button></div>
      {!!task.pending_scope && <Typography.Paragraph type="secondary">新资料将在下一步处理前加入。</Typography.Paragraph>}
      {!!task.plan.length && <div className="audit-task-section"><Typography.Text strong>工作安排</Typography.Text>
        <List dataSource={task.plan} renderItem={item => <List.Item className="audit-plan-item"><Tag>{statusLabels[item.status] || item.status}</Tag><div><Typography.Text>{item.title}</Typography.Text>{item.purpose && <small>{item.purpose}</small>}<ReferenceLinks refs={item.result_refs} taskId={task.id} /></div></List.Item>} />
      </div>}
      {!!(task.artifact_refs.length || task.result_refs.length) && <div className="audit-task-section"><Typography.Text strong>结果与成果</Typography.Text><ReferenceLinks refs={[...task.artifact_refs, ...task.result_refs]} taskId={task.id} /></div>}
      <Link className="audit-findings-entry" to={`/findings?workspace=${task.workspace_id}&session=${task.id}`}>查看发现与底稿 →</Link>
      {task.manual && <Typography.Paragraph type="secondary">已停止 Agent 后续动作。原生作业可继续完成，手动修改后带回最新结果。</Typography.Paragraph>}
    </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="目标、资料和执行成果会持续保存在同一项任务中。" />}
    <Button type="text" block disabled={busy || !hasProject} onClick={background}>项目背景与口径</Button>
    <Button type="text" block onClick={skills}>Agent 技能</Button>
  </aside>
}

export function RecentTasks({ tasks, current, choose, keyword, search }: { tasks: AuditTask[]; current: string | null; choose: (id: string) => void; keyword: string; search: (value: string) => void }) {
  return <><Input.Search aria-label="查找审计任务" placeholder="查找任务" value={keyword} onChange={event => search(event.target.value)} />
    <List dataSource={tasks} locale={{ emptyText: '本项目还没有任务' }} renderItem={item => <List.Item><Button type={current === item.id ? 'primary' : 'text'} onClick={() => choose(item.id)}>{item.title}</Button><Tag>{statusLabels[item.status] || item.status}</Tag></List.Item>} /></>
}
