import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Typography, type InputRef } from 'antd'
import ArrowRightOutlined from '@ant-design/icons/es/icons/ArrowRightOutlined'
import BookOutlined from '@ant-design/icons/es/icons/BookOutlined'
import SearchOutlined from '@ant-design/icons/es/icons/SearchOutlined'
import FilterOutlined from '@ant-design/icons/es/icons/FilterOutlined'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { useApps, useWorkspaceId, useWorkspaces } from '../../App'
import { ApplicationCard, RecentWorks } from './ApplicationCards'
import { ToolGlyph } from './ToolIdentity'
import { toolCatalog, toolCategories, type ToolCategory } from './toolCatalog'
import { navigationHref } from '../../clear-ui/navigation'
import { ReferenceLinks } from '../agent/TaskPanel'
import { agentRequest, type AuditTask } from '../agent/api'
import './directory.css'

export function ToolDirectory() {
  const workspaceId = useWorkspaceId(), apps = useApps(workspaceId), workspaces = useWorkspaces()
  const location = useLocation(), [params] = useSearchParams(), taskId = params.get('task_return')
  const [category, setCategory] = useState<ToolCategory | 'all'>('all'), [search, setSearch] = useState('')
  const input = useRef<InputRef>(null)
  const task = useQuery({ queryKey: ['audit-task', taskId], enabled: !!taskId && !!workspaceId,
    queryFn: () => agentRequest<AuditTask>(`/agent/sessions/${encodeURIComponent(taskId!)}/task`) })
  const selected = workspaces.data?.find(item => item.workspace_id === workspaceId)
  const href = (path: string) => navigationHref(path, location.search, location.hash, location.pathname)
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault(); input.current?.focus()
      }
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [])
  const query = search.trim().toLocaleLowerCase()
  const visible = toolCatalog.filter(tool => (category === 'all' || tool.category === category)
    && (!query || `${tool.id} ${tool.title} ${tool.brand} ${tool.summary} ${tool.formats} ${tool.keywords}`.toLocaleLowerCase().includes(query)))
  const known = new Set<string>(toolCatalog.map(tool => tool.id))
  const extras = category === 'all' ? (apps.data || []).filter(app => !known.has(app.app_id)
    && (!query || `${app.app_id} ${app.display_name}`.toLocaleLowerCase().includes(query))) : []
  const resultCount = visible.length + extras.length
  return <div className="manual-directory">
    <header className="directory-heading">
      <div><Typography.Title level={1}>工具</Typography.Title>
        <Typography.Paragraph>选择要完成的事，导入资料后开始处理。</Typography.Paragraph></div>
      <Link className="directory-guide" to={href('/help')}><BookOutlined /> 使用指南</Link>
    </header>

    <section className="directory-context" aria-label="当前工作范围">
      <div><strong>{selected ? `当前项目 · ${selected.name}` : '可直接打开工具'}</strong>
        <p>{selected ? '工具沿用当前项目，已有资料也可直接选用。' : '需要项目的工具会引导你选择或创建项目。'}</p></div>
      <Link to={href('/projects')}>{selected ? '查看项目' : '选择项目'} <ArrowRightOutlined /></Link>
    </section>

    <section className="directory-tools" aria-label="工具检索">
      <div className="directory-toolbar">
        <Input ref={input} className="directory-search" prefix={<SearchOutlined />} value={search} allowClear
          onChange={event => setSearch(event.target.value)} placeholder="搜索工具或要做的事，如 AI 生成、录音转文字" aria-label="搜索工具或要做的事"
          suffix={!search && <kbd>Ctrl K</kbd>} />
        <div className="directory-filters" role="group" aria-label="按资料类型筛选">
          {[{ id: 'all', label: '全部工具' }, ...toolCategories].map(filter => <button key={filter.id} type="button" aria-pressed={category === filter.id}
            onClick={() => setCategory(filter.id as ToolCategory | 'all')}>{filter.label}</button>)}
        </div>
      </div>
      <div className="directory-result-caption" role="status" aria-live="polite">
        {resultCount} 个工具{category !== 'all' || query ? '符合筛选' : '，选择后直接进入'}
      </div>
      {apps.isError && <Alert className="directory-load-error" type="warning" showIcon title="其他已安装工具暂时无法读取"
        description="下方工具入口仍可使用。" action={<Button onClick={() => void apps.refetch()}>重试读取</Button>} />}
      {toolCategories.map(group => {
        const tools = visible.filter(tool => tool.category === group.id)
        return !!tools.length && <section className="directory-tool-group" aria-labelledby={`tools-${group.id}`} key={group.id}>
          <header><h2 id={`tools-${group.id}`}>{group.title}</h2><p>{group.description}</p></header>
          <div className="directory-tool-grid">
            {tools.map(tool => <Link className="directory-tool-link" key={tool.id} data-tool={tool.id} to={href(tool.path)} aria-label={`打开${tool.title}`}>
              <span className="directory-tool-icon" aria-hidden="true"><ToolGlyph appId={tool.id} /></span>
              <span className="directory-tool-copy"><strong>{tool.title}{tool.experimental && <small className="directory-experiment">实验</small>}</strong>
                <span>{tool.summary}</span><small>{tool.formats}</small></span>
              <span className="directory-tool-open">打开 <ArrowRightOutlined aria-hidden="true" /></span>
            </Link>)}
          </div>
        </section>
      })}
      {!!extras.length && <section className="directory-tool-group" aria-label="其他已安装工具"><header><h2>其他已安装工具</h2></header>
        <div className="app-grid">{extras.map(app => <article className="catalog-tool" key={app.app_id}>
          <ApplicationCard app={app} workspaceId={workspaceId} projectMode={!!workspaceId} />
        </article>)}</div>
      </section>}
      {!resultCount && <div className="directory-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={query ? `没有找到与“${search.trim()}”匹配的工具` : '这个分类下没有工具'} />
        <Button icon={<FilterOutlined />} onClick={() => { setSearch(''); setCategory('all'); input.current?.focus() }}>显示全部工具</Button></div>}
    </section>

    {task.data?.workspace_id === workspaceId && !!(task.data.result_refs?.length || task.data.initial_refs?.length || task.data.job_refs?.length) &&
      <section className="panel directory-task-results"><Typography.Title level={3}>当前任务已有结果</Typography.Title>
        <ReferenceLinks taskId={taskId || undefined} refs={[...task.data.result_refs, ...task.data.initial_refs, ...task.data.job_refs]} /></section>}
    <RecentWorks workspaceId={workspaceId} />
  </div>
}
