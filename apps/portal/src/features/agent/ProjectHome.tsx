import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, List, Space, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { useWorkspaceId, Workspaces } from '../../App'
import { agentRequest, type AuditProfile, type AuditTask } from './api'
import { statusLabels } from './TaskPanel'
import type { Finding } from './FindingsWorkspace'

export function Projects() {
  const [params] = useSearchParams()
  return params.get('workspace') && params.get('create') !== '1' ? <ProjectHome /> : <Workspaces />
}

export function ProjectHome() {
  const workspace = useWorkspaceId()
  const overview = useQuery({ queryKey: ['audit-overview', workspace], enabled: !!workspace,
    queryFn: () => agentRequest<{ profile: AuditProfile; tasks: { items: AuditTask[] }; asset_count: number; findings: { items: Finding[]; total: number }; artifacts: { total: number; items: { bundle_id: string; title: string; status?: string }[] }; formal_projects: { items: { project_id: string; project: { title: string } }[]; total: number } }>(`/workspaces/${workspace}/audit-overview`) })
  if (!workspace) return null
  if (overview.error) return <Alert type="error" title={overview.error.message} action={<Button onClick={() => void overview.refetch()}>重试</Button>} />
  const value = overview.data
  if (!value) return <Typography.Paragraph>正在读取项目概况…</Typography.Paragraph>
  return <section className="audit-project-home"><div className="section-head audit-project-heading"><div><span className="audit-eyebrow">PROJECT OVERVIEW</span><Typography.Title level={2}>审计项目概况</Typography.Title></div><Link to={`/workspaces?workspace=${workspace}`}>管理项目</Link></div>
    <div className="audit-project-context"><Typography.Text strong>{value.profile.audit_object || '审计对象待填写'}</Typography.Text><span>{value.profile.period_start || '起始日期待填写'} — {value.profile.period_end || '结束日期待填写'}</span><Typography.Paragraph>{value.profile.objective || '可以直接开始任务，再逐步补充项目背景。'}</Typography.Paragraph></div>
    <div className="audit-project-stats"><Link to={`/?workspace=${workspace}`}><strong>{value.tasks.items.length}</strong>最近审计任务</Link><Link to={`/assets?workspace=${workspace}`}><strong>{value.asset_count}</strong>资料</Link><Link to={`/findings?workspace=${workspace}`}><strong>{value.findings.total}</strong>发现</Link><Link to={`/artifacts?workspace=${workspace}`}><strong>{value.artifacts.total}</strong>成果</Link></div>
    <Space wrap><Link to={`/?workspace=${workspace}`}><Button type="primary">开始智能审计</Button></Link><Link to={`/?workspace=${workspace}`}><Button onClick={() => localStorage.setItem(`clear-audit-draft:${workspace}:new`, JSON.stringify({ goal: '读取项目已有任务、发现与成果，汇总审计进展，明确尚缺的资料及有更新的结果。', assets: [] }))}>让 Agent 汇总项目</Button></Link><Link to={`/apps?workspace=${workspace}`}>手动工具</Link></Space>
    <div className="audit-project-columns"><div><Typography.Title level={4}>继续任务</Typography.Title><List dataSource={value.tasks.items} locale={{ emptyText: '从一个审计目标开始' }} renderItem={item => <List.Item><Link to={`/?workspace=${workspace}&session=${item.id}`}>{item.title}</Link><Tag>{statusLabels[item.status] || item.status}</Tag></List.Item>} /></div>
      <div><Typography.Title level={4}>发现与成果</Typography.Title>{value.findings.items.length ? <List dataSource={value.findings.items} renderItem={item => <List.Item><Link to={`/findings?workspace=${workspace}&session=${item.task_id}`}>{item.title}</Link>{item.outdated ? <Tag color="warning">需更新</Tag> : <Tag>v{item.revision}</Tag>}</List.Item>} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="执行结果可由 Agent 整理为发现与底稿" />}
        {value.artifacts.items.slice(0, 8).map(item => <div className="audit-project-artifact" key={item.bundle_id}><Link to={`/artifacts?workspace=${workspace}&bundle=${item.bundle_id}`}>{item.title}</Link>{item.status === 'outdated' && <Tag color="warning">历史版本</Tag>}</div>)}</div></div>
    {!!value.formal_projects.total && <Card size="small" title="正式审计档案"><Typography.Paragraph type="secondary">查看项目内的审计程序、固定底稿和人工署名记录。</Typography.Paragraph>{value.formal_projects.items.map(item => <Link key={item.project_id} to={`/audit?workspace=${workspace}&project=${item.project_id}`}>{item.project.title}　</Link>)}</Card>}
  </section>
}
