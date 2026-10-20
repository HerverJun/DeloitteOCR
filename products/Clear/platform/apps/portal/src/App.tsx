import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Form, Input, Layout, Menu, Modal, Popover, Select, Skeleton, Space, Tag, Typography } from 'antd'
import { Link, Navigate, Outlet, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import type { AppHealth, AppManifest, AppManifestPage, Workspace, WorkspacePage } from '../../../packages/contracts/types.generated'
import { ToolGlyph } from './features/catalog/ToolIdentity'
import AppstoreOutlined from '@ant-design/icons/es/icons/AppstoreOutlined'
import BookOutlined from '@ant-design/icons/es/icons/BookOutlined'
import CommentOutlined from '@ant-design/icons/es/icons/CommentOutlined'
import FolderOpenOutlined from '@ant-design/icons/es/icons/FolderOpenOutlined'
import MenuOutlined from '@ant-design/icons/es/icons/MenuOutlined'
import SettingOutlined from '@ant-design/icons/es/icons/SettingOutlined'
import HomeOutlined from '@ant-design/icons/es/icons/HomeOutlined'
import { healthStateText, healthReasonText } from './features/catalog/ApplicationDeclaration'
import { brands, brandName } from './clear-ui/theme'
import { PortalHome } from './features/home/PortalHome'
import { navigationHref } from './clear-ui/navigation'
import { toolCatalog } from './features/catalog/toolCatalog'
import { ManualReturn } from './features/agent/ManualReturn'
import { ProjectNavigation } from './features/agent/ProjectNavigation'
import { rememberVisit, useRecentVisits } from './clear-ui/recentVisits'
import { useSession } from './features/tasks/session'

const ApplicationCard = lazy(() => import('./features/catalog/ApplicationCards').then(module => ({ default: module.ApplicationCard })))
const ToolDirectory = lazy(() => import('./features/catalog/ToolDirectory').then(module => ({ default: module.ToolDirectory })))
const AgentWorkbench = lazy(() => import('./features/agent/AgentWorkbench').then(module => ({ default: module.AgentWorkbench })))
const loadingPage = <div role="status" aria-label="正在读取工作区"><Skeleton active paragraph={{ rows: 4 }} /></div>

const { Header, Sider, Content } = Layout
const { Title, Text, Paragraph } = Typography

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error(`服务请求失败（${response.status}）`)
  return response.json() as Promise<T>
}

async function pages<T extends { items: unknown[]; next_cursor: string | null; snapshot_revision: number }>(path: string, signal: AbortSignal): Promise<T['items']> {
  const items: T['items'] = []
  let cursor: string | null = null
  let revision: number | null = null
  do {
    const params = new URLSearchParams({ limit: '200' })
    if (cursor !== null && revision !== null) { params.set('cursor', cursor); params.set('snapshot_revision', String(revision)) }
    const page: T = await read<T>(`${path}?${params}`, signal)
    if (revision !== null && revision !== page.snapshot_revision) throw new Error('列表已更新，请重试')
    revision = page.snapshot_revision
    items.push(...page.items)
    cursor = page.next_cursor
  } while (cursor !== null)
  return items
}

export function useWorkspaceId() { const [params] = useSearchParams(); return params.get('workspace') || null }
export function useWorkspaces() { return useQuery({ queryKey: ['workspaces'], queryFn: ({ signal }) => pages<WorkspacePage>('/api/platform/v1/workspaces', signal) as Promise<Workspace[]> }) }
export function useApps(workspaceId: string | null) { return useQuery({ queryKey: ['apps', workspaceId], queryFn: ({ signal }) => pages<AppManifestPage>('/api/platform/v1/apps', signal) as Promise<AppManifest[]> }) }

function ScopeLink({ to, children }: { to: string; children: React.ReactNode }) {
  const workspaceId = useWorkspaceId()
  const path = to.split('?')[0]
  return <Link to={`${path}${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ''}`}>{children}</Link>
}

function NavigationIcon({ kind }: { kind: 'home' | 'audit' | 'project' | 'tools' | 'menu' }) {
  const Icon = kind === 'home' ? HomeOutlined : kind === 'audit' ? CommentOutlined : kind === 'project' ? FolderOpenOutlined : kind === 'tools' ? AppstoreOutlined : MenuOutlined
  return <Icon className="navigation-icon" aria-hidden="true" />
}

const primaryNavigation = [
  { key: '/', icon: <NavigationIcon kind="home" />, label: '工作首页' },
  { key: '/agent', icon: <NavigationIcon kind="audit" />, label: 'Agent' }, { key: '/projects', icon: <NavigationIcon kind="project" />, label: '项目' },
  { key: '/apps', icon: <NavigationIcon kind="tools" />, label: '工具' },
]
const projectPaths = ['/projects', '/workspaces', '/assets', '/findings', '/artifacts', '/audit', '/todos', '/batches', '/jobs']

export function App() {
  const location = useLocation(), navigate = useNavigate(), workspaceId = useWorkspaceId()
  const [navigationCollapsed, setNavigationCollapsed] = useState(() => !!window.matchMedia?.('(max-width: 991px)').matches)
  const [narrowNavigation, setNarrowNavigation] = useState(() => !!window.matchMedia?.('(max-width: 991px)').matches)
  const workspaces = useWorkspaces()
  const { user } = useSession()
  const [toolPicker, setToolPicker] = useState(false)
  const visits = useRecentVisits(user?.id || 'single').filter(item => item.href.startsWith('/agent?') && (!workspaceId || item.workspace === workspaceId) && (!item.workspace || workspaces.data?.some(project => project.workspace_id === item.workspace)))
  const selected = workspaces.data?.find(item => item.workspace_id === workspaceId)
  const isProject = projectPaths.some(path => location.pathname === path || location.pathname.startsWith(`${path}/`))
  const isAgent = location.pathname === '/agent'
  const isSettings = location.pathname === '/settings' || location.pathname.startsWith('/settings/')
  const active = isAgent ? '/agent' : isProject ? '/projects' : ['/apps', '/manual', '/data', '/forensics'].some(path => location.pathname === path || location.pathname.startsWith(`${path}/`)) ? '/apps' : '/'
  const scoped = (path: string, id: string | null = workspaceId) => `${path}${id ? `?workspace=${encodeURIComponent(id)}` : ''}`
  const mainHref = (path: string) => navigationHref(path, location.search, '', location.pathname)
  useEffect(() => {
    if (location.pathname === '/' || location.pathname === '/agent' && new URLSearchParams(location.search).has('session')) return
    const label = location.pathname.startsWith('/apps/') ? toolCatalog.find(tool => location.pathname.startsWith(tool.path))?.title || '专业工具' : ({ '/agent': 'Agent', '/projects': '项目', '/assets': '资料库', '/artifacts': '成果', '/jobs': '任务', '/findings': '发现', '/apps': '工具' } as Record<string, string>)[location.pathname]
    if (label) rememberVisit(user?.id || 'single', { href: location.pathname + location.search, title: selected ? `${label} · ${selected.name}` : label, workspace: workspaceId })
  }, [location.pathname, location.search, selected?.name, workspaceId, user?.id])
  return <Layout className={"shell" + (isAgent ? " agent-shell" : "")}>
    <a className="skip-link" href="#platform-content">跳至工作区</a>
    <Header className="topbar"><div className="topbar-left">
      <Button className="navigation-toggle" aria-label={navigationCollapsed ? '展开导航' : '收起导航'}
        aria-expanded={!navigationCollapsed} aria-controls="platform-navigation" onClick={() => setNavigationCollapsed(value => !value)}>
        <NavigationIcon kind="menu" />
      </Button>
      <Link className="brand" to={mainHref('/')} title="返回审计平台首页" aria-label={`${brands.platform} · 返回审计平台首页`}><img src="/brand/clear-mark.svg" alt="" /><span>{brands.platform}</span></Link>
      <span className="breadcrumb"><span>/</span> {isSettings ? '设置' : location.pathname === '/help' ? '使用指南' : primaryNavigation.find(item => item.key === active)?.label}</span>
    </div>
      <div className="topbar-controls"><Popover trigger="click" placement="bottomRight" open={toolPicker} onOpenChange={setToolPicker}
        title="选择工具" content={<nav className="tool-picker" aria-label="工具快捷入口">{toolCatalog.map(tool => <Link key={tool.id} to={navigationHref(tool.path, location.search, location.hash, location.pathname)} onClick={() => setToolPicker(false)}><ToolGlyph appId={tool.id} /><span><strong>{tool.title}</strong><small>{tool.summary}</small></span></Link>)}</nav>}>
        <Button type="text" className="tool-picker-trigger" icon={<AppstoreOutlined aria-hidden="true" />} aria-expanded={toolPicker}>工具</Button></Popover>{workspaces.isError ? <Button size="small" onClick={() => workspaces.refetch()}>项目加载失败 · 重试</Button> :
        <Select aria-label="当前项目" className="workspace-select" value={selected?.workspace_id || undefined}
          placeholder={workspaces.isPending ? '读取项目…' : '选择项目'} loading={workspaces.isPending}
          allowClear
          onChange={id => navigate(scoped(location.pathname, id ?? null))}
          options={workspaces.data?.map(item => ({ value: item.workspace_id, label: item.name }))} />}
      <Link to={`/settings${location.search}`} className="topbar-settings"><SettingOutlined /> <span>设置</span></Link>
      </div>
    </Header>
    <Layout className="workspace-shell">
      <Sider id="platform-navigation" className="sidebar" breakpoint="lg" collapsedWidth="0" width={208}
        inert={narrowNavigation && navigationCollapsed}
        collapsed={navigationCollapsed} trigger={null}
        onBreakpoint={broken => { setNarrowNavigation(broken); setNavigationCollapsed(broken) }}>
        <div className="sidebar-section-label sidebar-first-label">工作空间</div>
        <Menu theme="light" mode="inline" selectedKeys={isSettings || location.pathname === '/help' ? [] : [active]}
          items={primaryNavigation}
          onClick={({ key }) => { navigate(mainHref(key)); if (narrowNavigation) setNavigationCollapsed(true) }} aria-label="主导航" />
        {isAgent && !!visits.length && <nav className="sidebar-recent" aria-label="最近会话"><div className="sidebar-section-label">最近会话</div>{visits.slice(0, 8).map(item => <Link key={item.href} to={item.href} title={item.title} onClick={() => { if (narrowNavigation) setNavigationCollapsed(true) }}>{item.title}</Link>)}</nav>}
        <nav className="sidebar-utilities" aria-label="工作与成果"><div className="sidebar-section-label">工作与成果</div>{[{ path: '/assets', name: '资料库' }, { path: '/jobs', name: '任务中心' }, { path: '/artifacts', name: '成果' }].map(item => <Link key={item.path} to={mainHref(item.path)} onClick={() => { if (narrowNavigation) setNavigationCollapsed(true) }}>{item.name}</Link>)}</nav>
        <div className="sidebar-foot"><Link to={scoped('/help')}><BookOutlined /> 使用指南</Link><div><span className="sidebar-local-dot" />本地审计工作台</div></div>
      </Sider>
      {narrowNavigation && !navigationCollapsed && <button className="navigation-backdrop" aria-label="关闭导航" onClick={() => setNavigationCollapsed(true)} />}
      <Content id="platform-content" tabIndex={-1} className={`page${isAgent ? ' audit-home-page' : ''}${['/apps/data', '/apps/forensics'].includes(location.pathname) ? ' workbench-page' : ''}`}>
      <ManualReturn />
      {isProject && <ProjectNavigation />}
      {workspaceId && workspaces.isPending ? <div role="status" aria-label="正在读取项目"><Skeleton active /></div> :
        workspaceId && workspaces.isError ? <Alert type="error" title="无法读取项目" action={<Button onClick={() => void workspaces.refetch()}>重试</Button>} /> :
        workspaceId && !selected ? <Alert type="warning" title="项目不存在或已失效" description="请选择已有项目，或清除当前选择。"
          action={<Button onClick={() => navigate(scoped(location.pathname, null))}>清除项目选择</Button>} /> : <Suspense fallback={loadingPage}><Outlet /></Suspense>}
      </Content>
    </Layout>
  </Layout>
}

function QueryState({ pending, error, retry, children, empty }: {
  pending: boolean; error: boolean; retry: () => void; children: React.ReactNode; empty?: boolean
}) {
  if (pending) return <div role="status" aria-label="正在加载"><Skeleton active paragraph={{ rows: 3 }} /></div>
  if (error) return <Alert type="error" showIcon title="暂时无法取得服务数据" description="确认平台服务已启动后重试。" action={<Button onClick={retry}>重试</Button>} />
  if (empty) return <Empty description="目前没有记录" />
  return <>{children}</>
}

function PageHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  return <div className="page-heading"><Text className="eyebrow">{eyebrow}</Text><Title level={1}>{title}</Title><Paragraph>{description}</Paragraph></div>
}

export function Home() {
  const { search, hash } = useLocation()
  const params = new URLSearchParams(search)
  if (params.get('session') || params.get('continue')) return <Navigate replace to={`/agent${search}${hash}`} />
  return <PortalHome />
}

export function AgentHome() { return <Suspense fallback={loadingPage}><AgentWorkbench /></Suspense> }

export function Workspaces({ continueTo }: { continueTo?: '/apps/forensics' | '/apps/ocr/start' | '/apps/classification/start' | '/apps/face' | '/apps/audio' | '/apps/ocr/seals' | '/apps/forensics/deep' } = {}) {
  const list = useWorkspaces(), workspaceId = useWorkspaceId(), client = useQueryClient(), navigate = useNavigate()
  const [keyword, setKeyword] = useState('')
  const [params] = useSearchParams()
  const location = useLocation()
  const mounted = useRef(true), currentLocationKey = useRef(location.key)
  currentLocationKey.current = location.key
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const workspaceHref = (id: string) => {
    if (!continueTo) return `/?workspace=${encodeURIComponent(id)}`
    const next = new URLSearchParams(params)
    next.set('workspace', id)
    next.delete('create')
    return `${continueTo}?${next}${location.hash}`
  }
  const [open, setOpen] = useState(params.get('create') === '1')
  const [form] = Form.useForm<{ name: string }>()
  const pendingKey = 'clear-workspace-create'
  useEffect(() => {
    const saved = localStorage.getItem(pendingKey)
    if (saved) { try { form.setFieldsValue({ name: JSON.parse(saved).name }) } catch { /* Preserve unknown intent. */ } }
  }, [form])
  const create = useMutation({ mutationFn: async (name: string) => {
    const entryLocationKey = location.key
    const saved = localStorage.getItem(pendingKey)
    const payload = saved ? JSON.parse(saved) : { schema_version: '1.0', name, request_id: `w${crypto.randomUUID().replaceAll('-', '')}` }
    if (payload.name !== name) throw new Error('上次创建尚未确认，请使用原名称重试；确认后可修改项目名称。')
    localStorage.setItem(pendingKey, JSON.stringify(payload))
    const response = await fetch('/api/platform/v1/workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) })
    if (!response.ok) throw new Error(`创建失败（${response.status}）`)
    return { item: await response.json() as Workspace, entryLocationKey }
  }, onSuccess: ({ item, entryLocationKey }) => {
    localStorage.removeItem(pendingKey)
    // The confirmed server response is immediately visible while the list refreshes.
    client.setQueryData<Workspace[]>(['workspaces'], previous => [...(previous || []).filter(value => value.workspace_id !== item.workspace_id), item])
    void client.invalidateQueries({ queryKey: ['workspaces'] })
    if (!mounted.current || currentLocationKey.current !== entryLocationKey) return
    setOpen(false); form.resetFields()
    navigate(workspaceHref(item.workspace_id))
  } })
  const content = <>{!continueTo && <PageHeading eyebrow="PROJECTS" title="项目" description="按项目整理资料和输出。填写一个名称即可开始使用 Clear 工具。" />}
    <div className="section-head"><Title level={3}>{continueTo ? '选择项目后继续' : '已有项目'}</Title><Button type="primary" onClick={() => setOpen(true)}>新建项目</Button></div>
    <Input.Search className="project-search" aria-label="搜索项目" placeholder="搜索项目名称" value={keyword} onChange={event => setKeyword(event.target.value)} allowClear />
    {continueTo && <Paragraph type="secondary">选择已有项目或新建项目，随后直接进入当前工具。资料和结果会保存在所选项目中。</Paragraph>}
    <QueryState pending={list.isPending} error={list.isError} retry={() => void list.refetch()} empty={!continueTo && list.data?.length === 0}>
      {continueTo && list.data?.length === 0 && <Empty description="暂无项目，新建后即可继续" />}
      {!!list.data?.length && !list.data.some(item => item.name.toLocaleLowerCase().includes(keyword.trim().toLocaleLowerCase())) && <Empty description="没有匹配的项目" />}
      <div className="workspace-list">{list.data?.filter(item => item.name.toLocaleLowerCase().includes(keyword.trim().toLocaleLowerCase())).map(item => <Card key={item.workspace_id} className={item.workspace_id === workspaceId ? 'selected' : ''}>
        <Space orientation="vertical"><Link to={workspaceHref(item.workspace_id)}>{item.name}</Link>
          {!continueTo && <Link to={`/artifacts?workspace=${encodeURIComponent(item.workspace_id)}`}>查看项目输出</Link>}</Space><Tag>{item.workspace_id === workspaceId ? '当前项目' : continueTo ? '选择并继续' : '选择并开始'}</Tag>
      </Card>)}</div>
    </QueryState>
    <Modal title="新建项目" open={open} closable={!create.isPending} mask={{ closable: !create.isPending }} keyboard={!create.isPending}
      onCancel={() => { if (!create.isPending) { setOpen(false); create.reset() } }}
      onOk={() => void form.validateFields().then(values => create.mutate(values.name)).catch(() => undefined)}
      okText={continueTo ? '创建并继续' : '创建'} okButtonProps={{ loading: create.isPending }} cancelText="取消" cancelButtonProps={{ disabled: create.isPending }}>
      <Form form={form} layout="vertical"><Form.Item label="名称" name="name" rules={[{ required: true, whitespace: true, message: '请输入名称' }, { max: 256 }]}>
        <Input autoFocus maxLength={256} placeholder="例如：季度资料整理" /></Form.Item></Form>
      {create.isError && <Alert type="error" showIcon title={create.error.message} />}
    </Modal></>
  return continueTo ? <section className="panel tool-project-entry">{content}</section> : content
}

export function Applications() {
  return <Suspense fallback={loadingPage}><ToolDirectory /></Suspense>
}

export function Application() {
  const { appId } = useParams(), workspaceId = useWorkspaceId()
  const validId = !!appId && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(appId)
  const safeId = encodeURIComponent(appId || '')
  const info = useQuery({ queryKey: ['app', workspaceId, appId], enabled: validId, queryFn: ({ signal }) => read<AppManifest>(`/api/platform/v1/apps/${safeId}`, signal) })
  const health = useQuery({ queryKey: ['app-health', workspaceId, appId], enabled: validId, queryFn: ({ signal }) => read<AppHealth>(`/api/platform/v1/apps/${safeId}/health`, signal), refetchInterval: 15_000 })
  if (!validId) return <Missing />
  return <><PageHeading eyebrow="APPLICATION / STATUS" title={info.data ? brandName(info.data.app_id, info.data.display_name) : '应用状态'} description="核实本机实例后打开工具。无需先建立工作空间。" />
    <QueryState pending={info.isPending || health.isPending} error={info.isError || health.isError}
      retry={() => { void info.refetch(); void health.refetch() }}>
      <div className="panel"><div className="section-head"><Title level={3}>{info.data && brandName(info.data.app_id, info.data.display_name)}</Title><Tag>{info.data?.app_version}</Tag></div>
        <div className="status-list">{(['service', 'ui', 'cpu', 'gpu', 'models'] as const).map(key => <div key={key}>
          <Text>{({ service: '服务', ui: '界面', cpu: 'CPU', gpu: 'GPU', models: '模型' })[key]}</Text>
          <Tag color={health.data?.[key].state === 'ready' ? 'success' : 'default'}>{healthStateText(health.data?.[key].state)}</Tag>
          <Text type="secondary">{healthReasonText(health.data?.[key].reason)}</Text></div>)}</div></div>
      {info.data && <Suspense fallback={loadingPage}><ApplicationCard key={info.data.app_id} app={info.data} workspaceId={workspaceId} /></Suspense>}
    </QueryState><div className="back-link"><ScopeLink to="/apps">← 返回应用目录</ScopeLink></div></>
}

export function Placeholder({ title, description }: { title: string; description: string }) {
  return <><PageHeading eyebrow="WORKBENCH" title={title} description={description} />
    <div className="empty-panel"><Empty description={`${title}功能尚未开放`} /><ScopeLink to="/apps">查看可用应用 →</ScopeLink></div></>
}
export function Missing() { return <><PageHeading eyebrow="NOT FOUND" title="页面不存在" description="这个地址不属于当前门户。" /><ScopeLink to="/">返回首页 →</ScopeLink></> }
