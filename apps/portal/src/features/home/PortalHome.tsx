import { Alert, Button, Empty, Skeleton, Tag } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { Link, useLocation } from 'react-router-dom'
import ArrowRightOutlined from '@ant-design/icons/es/icons/ArrowRightOutlined'
import PlusOutlined from '@ant-design/icons/es/icons/PlusOutlined'
import FolderOpenOutlined from '@ant-design/icons/es/icons/FolderOpenOutlined'
import ClockCircleOutlined from '@ant-design/icons/es/icons/ClockCircleOutlined'
import { useWorkspaceId, useWorkspaces } from '../../App'
import { navigationHref } from '../../clear-ui/navigation'
import { useRecentVisits } from '../../clear-ui/recentVisits'
import { useSession } from '../tasks/session'
import { ToolGlyph } from '../catalog/ToolIdentity'
import { toolCatalog } from '../catalog/toolCatalog'
import { statusLabels } from '../agent/statusLabels'
import type { AuditTask } from '../agent/api'
import './home.css'

type Overview = { tasks: { items: AuditTask[] }; artifacts: { items: { bundle_id: string; title: string; status?: string }[] } }
export function PortalHome() {
  const location = useLocation(), workspaceId = useWorkspaceId(), workspaces = useWorkspaces(), { user } = useSession()
  const selected = workspaces.data?.find(item => item.workspace_id === workspaceId)
  const href = (path: string) => navigationHref(path, location.search)
  const visits = useRecentVisits(user?.id || 'single').filter(item => !item.workspace || workspaces.data?.some(project => project.workspace_id === item.workspace))
  const overview = useQuery({ queryKey: ['audit-overview', workspaceId], enabled: !!workspaceId,
    queryFn: async ({ signal }) => {
      const response = await fetch(`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId!)}/audit-overview`, { signal })
      if (!response.ok) throw new Error('暂时无法读取项目工作记录')
      const value = await response.json()
      if (!Array.isArray(value.tasks?.items) || !Array.isArray(value.artifacts?.items)) throw new Error('项目记录格式不可读取，请重试')
      return value as Overview
    } })
  return <div className="portal-home">
    <header className="home-heading"><div><span className="eyebrow">CLEAR WORKSPACE</span><h1>开始今天的工作</h1><p>{selected ? selected.name : '继续已有工作，或把一个新需求交给 Agent。'}</p></div>
      <Link className="home-start" to={href('/agent')}><PlusOutlined aria-hidden="true" />新任务<ArrowRightOutlined aria-hidden="true" /></Link></header>
    <section className="home-quick-start" aria-labelledby="home-start-title"><div><span className="home-section-label">AGENT</span><h2 id="home-start-title">你想完成什么？</h2><p>问一个问题、处理文件，或调用专业工具完成分析。</p></div>
      <div className="home-intents">{[['解释与整理', '读取资料，概括重点并解释内容。'], ['文件与表格', '清洗数据、计算和生成可下载文件。'], ['审计与核对', '对照来源，整理差异和底稿草稿。']].map(([title, text]) => <Link key={title} to={href('/agent')}><strong>{title}</strong><span>{text}</span><ArrowRightOutlined aria-hidden="true" /></Link>)}</div></section>
    <div className="home-work-grid"><section className="home-section" aria-labelledby="home-continue-title"><header><h2 id="home-continue-title"><ClockCircleOutlined aria-hidden="true" />继续工作</h2>{workspaceId && <Link to={href('/agent')}>全部任务<ArrowRightOutlined aria-hidden="true" /></Link>}</header>
      {workspaceId ? overview.isPending ? <Skeleton active paragraph={{ rows: 3 }} /> : overview.error ? <Alert type="error" title={overview.error.message} action={<Button onClick={() => void overview.refetch()}>重试</Button>} /> :
        overview.data?.tasks.items.length ? <div className="home-work-list">{overview.data.tasks.items.slice(0, 6).map(task => <Link key={task.id} to={`/agent?workspace=${encodeURIComponent(workspaceId)}&session=${encodeURIComponent(task.id)}`}><span><strong>{task.title}</strong><small>Agent 任务</small></span><Tag>{statusLabels[task.status] || task.status}</Tag><ArrowRightOutlined aria-hidden="true" /></Link>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前项目还没有任务，从新任务开始" /> :
        visits.length ? <div className="home-work-list">{visits.slice(0, 6).map(item => <Link key={item.href} to={item.href}><span><strong>{item.title}</strong><small>最近访问 · {new Date(item.visited).toLocaleDateString('zh-CN')}</small></span><ArrowRightOutlined aria-hidden="true" /></Link>)}</div> : <div className="home-empty"><p>工作记录会出现在这里。</p><Link to="/agent">开始第一个任务<ArrowRightOutlined aria-hidden="true" /></Link></div>}
    </section><section className="home-section" aria-labelledby="home-projects-title"><header><h2 id="home-projects-title"><FolderOpenOutlined aria-hidden="true" />{selected ? '项目成果' : '我的项目'}</h2><Link to={href(selected ? '/artifacts' : '/projects')}>查看全部<ArrowRightOutlined aria-hidden="true" /></Link></header>
      {selected ? overview.isPending ? <Skeleton active /> : overview.error ? <p className="home-muted">项目记录暂不可用，可重试或继续使用工具。</p> : overview.data?.artifacts.items.length ? <div className="home-work-list">{overview.data.artifacts.items.slice(0, 5).map(item => <Link key={item.bundle_id} to={`/artifacts?workspace=${encodeURIComponent(workspaceId!)}&bundle=${encodeURIComponent(item.bundle_id)}`}><span><strong>{item.title}</strong><small>{item.status === 'outdated' ? '历史版本' : '查看文件与来源'}</small></span><ArrowRightOutlined aria-hidden="true" /></Link>)}</div> : <div className="home-empty"><p>处理完成后，可在这里查看项目成果。</p><Link to={href('/assets')}>查看项目资料<ArrowRightOutlined aria-hidden="true" /></Link></div> :
        workspaces.isPending ? <Skeleton active /> : workspaces.error ? <Alert type="error" title="项目读取失败" action={<Button onClick={() => void workspaces.refetch()}>重试</Button>} /> :
          <><div className="home-work-list">{workspaces.data?.slice(0, 3).map(item => <Link key={item.workspace_id} to={`/projects?workspace=${encodeURIComponent(item.workspace_id)}`}><span><strong>{item.name}</strong><small>资料、任务与成果</small></span><ArrowRightOutlined aria-hidden="true" /></Link>)}</div><Link className="home-create-project" to="/projects?create=1"><PlusOutlined aria-hidden="true" />新建项目</Link></>}
    </section></div>
    <section className="home-tools" aria-labelledby="home-tools-title"><header><h2 id="home-tools-title">常用工具</h2><Link to={href('/apps')}>全部工具与专项扩展<ArrowRightOutlined aria-hidden="true" /></Link></header><nav aria-label="常用工具">{toolCatalog.map(tool => <Link key={tool.id} to={href(tool.path)}><ToolGlyph appId={tool.id} /><span><strong>{tool.title}</strong><small>{tool.summary}</small></span><ArrowRightOutlined aria-hidden="true" /></Link>)}</nav></section>
    <footer className="home-footer"><span>Clear · 资料、处理与成果</span><Link to={href('/help')}>使用指南<ArrowRightOutlined aria-hidden="true" /></Link></footer>
  </div>
}
