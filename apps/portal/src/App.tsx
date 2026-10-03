import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Form, Input, Layout, Menu, Modal, Select, Skeleton, Space, Tag, Typography } from 'antd'
import { Link, Outlet, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import type { AppHealth, AppManifest, AppManifestPage, Workspace, WorkspacePage } from '../../../packages/contracts/types.generated'
import { ApplicationCard, RecentWorks } from './features/catalog/ApplicationCards'
import { DataApplicationCard } from './features/data/DataWorkbench'
import { ForensicApplicationCard } from './features/forensics/ForensicWorkbench'
import { brands, brandName } from './clear-ui/theme'
import { QuickStart } from './features/start/QuickStart'

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

const primaryNavigation = [
  { key: '/', label: '开始处理' }, { key: '/projects', label: '项目' },
  { key: '/apps', label: 'Clear 工具' }, { key: '/artifacts', label: '输出' },
]
const advancedNavigation = [
  { key: '/audit', label: '审计底稿' }, { key: '/todos', label: '统一待办' },
  { key: '/assets', label: '资料库' }, { key: '/batches', label: '模板批次' },
  { key: '/jobs', label: '运行任务' }, { key: '/settings', label: '设置' },
]
const nav = [...primaryNavigation, ...advancedNavigation]

export function App() {
  const location = useLocation(), navigate = useNavigate(), workspaceId = useWorkspaceId()
  const [navigationCollapsed, setNavigationCollapsed] = useState(false)
  const [narrowNavigation, setNarrowNavigation] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const workspaces = useWorkspaces()
  const selected = workspaces.data?.find(item => item.workspace_id === workspaceId)
  const active = location.pathname === '/workspaces' ? '/projects' : nav.find(item => item.key !== '/' && (location.pathname === item.key || location.pathname.startsWith(`${item.key}/`)))?.key || '/'
  const isAdvanced = advancedNavigation.some(item => item.key === active)
  const scoped = (path: string, id: string | null = workspaceId) => `${path}${id ? `?workspace=${encodeURIComponent(id)}` : ''}`
  return <Layout className="shell">
    <Header className="topbar"><div className="topbar-left">
      <Button className="navigation-toggle" aria-label={navigationCollapsed ? '展开导航' : '收起导航'}
        aria-expanded={!navigationCollapsed} aria-controls="platform-navigation" onClick={() => setNavigationCollapsed(value => !value)}>
        <span aria-hidden="true">☰</span>
      </Button>
      <Link className="brand" to={scoped('/')} title="返回审计平台首页" aria-label={`${brands.platform} · 返回审计平台首页`}><img src="/brand/clear-mark.svg" alt="" /><span>{brands.platform}</span></Link>
      <span className="breadcrumb"><span>/</span> {isAdvanced && '高级功能 / '}{nav.find(item => item.key === active)?.label}</span>
    </div>
      {workspaces.isError ? <Button size="small" onClick={() => workspaces.refetch()}>项目加载失败 · 重试</Button> :
        <Select aria-label="当前项目" className="workspace-select" value={selected?.workspace_id || undefined}
          placeholder={workspaces.isPending ? '读取项目…' : '选择项目'} loading={workspaces.isPending}
          allowClear
          onChange={id => navigate(scoped(location.pathname, id ?? null))}
          options={workspaces.data?.map(item => ({ value: item.workspace_id, label: item.name }))} />}
    </Header>
    <Layout className="workspace-shell">
      <Sider id="platform-navigation" className="sidebar" breakpoint="lg" collapsedWidth="0" width={216}
        inert={narrowNavigation && navigationCollapsed}
        collapsed={navigationCollapsed} trigger={null}
        onBreakpoint={broken => { setNarrowNavigation(broken); setNavigationCollapsed(broken) }}>
        <div className="sidebar-context"><span>本地作业平台</span><small>{selected?.name || '选择项目后开始'}</small></div>
        <Menu theme="dark" mode="inline" selectedKeys={[active]} openKeys={advancedOpen || isAdvanced ? ['advanced'] : []}
          onOpenChange={keys => setAdvancedOpen(keys.includes('advanced'))}
          items={[...primaryNavigation, { key: 'advanced', label: '高级功能', children: advancedNavigation }]}
          onClick={({ key }) => { navigate(scoped(key)); if (narrowNavigation) setNavigationCollapsed(true) }} aria-label="主导航" />
        <div className="sidebar-foot">本机工作台 · v0.1</div>
      </Sider>
      {narrowNavigation && !navigationCollapsed && <button className="navigation-backdrop" aria-label="关闭导航" onClick={() => setNavigationCollapsed(true)} />}
      <Content className="page">
      {workspaceId && workspaces.isPending ? <div role="status" aria-label="正在读取项目"><Skeleton active /></div> :
        workspaceId && workspaces.isError ? <Alert type="error" title="无法读取项目" action={<Button onClick={() => void workspaces.refetch()}>重试</Button>} /> :
        workspaceId && !selected ? <Alert type="warning" title="项目不存在或已失效" description="请选择已有项目，或清除当前选择。"
          action={<Button onClick={() => navigate(scoped(location.pathname, null))}>清除项目选择</Button>} /> : <Outlet />}
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
  return <QuickStart />
}

export function Workspaces() {
  const list = useWorkspaces(), workspaceId = useWorkspaceId(), client = useQueryClient(), navigate = useNavigate()
  const [params] = useSearchParams()
  const [open, setOpen] = useState(params.get('create') === '1')
  const [form] = Form.useForm<{ name: string }>()
  const pendingKey = 'clear-workspace-create'
  useEffect(() => {
    const saved = localStorage.getItem(pendingKey)
    if (saved) { try { form.setFieldsValue({ name: JSON.parse(saved).name }) } catch { /* Preserve unknown intent. */ } }
  }, [form])
  const create = useMutation({ mutationFn: async (name: string) => {
    const saved = localStorage.getItem(pendingKey)
    const payload = saved ? JSON.parse(saved) : { schema_version: '1.0', name, request_id: `w${crypto.randomUUID().replaceAll('-', '')}` }
    if (payload.name !== name) throw new Error('上次创建尚未确认，请使用原名称重试；确认后可修改项目名称。')
    localStorage.setItem(pendingKey, JSON.stringify(payload))
    const response = await fetch('/api/platform/v1/workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) })
    if (!response.ok) throw new Error(`创建失败（${response.status}）`)
    return response.json() as Promise<Workspace>
  }, onSuccess: item => {
    localStorage.removeItem(pendingKey)
    // The confirmed server response is immediately visible while the list refreshes.
    client.setQueryData<Workspace[]>(['workspaces'], previous => [...(previous || []).filter(value => value.workspace_id !== item.workspace_id), item])
    setOpen(false); form.resetFields()
    navigate(`/?workspace=${encodeURIComponent(item.workspace_id)}`)
    void client.invalidateQueries({ queryKey: ['workspaces'] })
  } })
  return <><PageHeading eyebrow="PROJECTS" title="项目" description="按项目整理资料和输出。填写一个名称即可开始使用 Clear 工具。" />
    <div className="section-head"><Title level={3}>已有项目</Title><Button type="primary" onClick={() => setOpen(true)}>新建项目</Button></div>
    <QueryState pending={list.isPending} error={list.isError} retry={() => void list.refetch()} empty={list.data?.length === 0}>
      <div className="workspace-list">{list.data?.map(item => <Card key={item.workspace_id} className={item.workspace_id === workspaceId ? 'selected' : ''}>
        <Space orientation="vertical"><Link to={`/?workspace=${encodeURIComponent(item.workspace_id)}`}>{item.name}</Link>
          <Link to={`/artifacts?workspace=${encodeURIComponent(item.workspace_id)}`}>查看项目输出</Link></Space><Tag>{item.workspace_id === workspaceId ? '当前项目' : '选择并开始'}</Tag>
      </Card>)}</div>
    </QueryState>
    <Modal title="新建项目" open={open} onCancel={() => { setOpen(false); create.reset() }}
      onOk={() => void form.validateFields().then(values => create.mutate(values.name)).catch(() => undefined)}
      okText="创建" okButtonProps={{ loading: create.isPending }} cancelText="取消">
      <Form form={form} layout="vertical"><Form.Item label="名称" name="name" rules={[{ required: true, whitespace: true, message: '请输入名称' }, { max: 256 }]}>
        <Input autoFocus maxLength={256} placeholder="例如：季度资料整理" /></Form.Item></Form>
      {create.isError && <Alert type="error" showIcon title={create.error.message} />}
    </Modal></>
}

export function Applications() {
  const workspaceId = useWorkspaceId(), apps = useApps(workspaceId)
  return <><PageHeading eyebrow="APPLICATIONS" title="Clear 工具" description="选择当前项目，再打开工具处理资料和生成输出。" />
    <div className="app-grid" aria-label="Clear 应用目录"><DataApplicationCard />
      <QueryState pending={apps.isPending} error={apps.isError} retry={() => void apps.refetch()}>
        {apps.data?.map(app => <ApplicationCard key={app.app_id} app={app} workspaceId={workspaceId} projectMode={!!workspaceId} />)}
      </QueryState><ForensicApplicationCard />
    </div><RecentWorks workspaceId={workspaceId} /></>
}

export function Application() {
  const { appId } = useParams(), workspaceId = useWorkspaceId()
  const validId = !!appId && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(appId)
  const safeId = encodeURIComponent(appId || '')
  const info = useQuery({ queryKey: ['app', workspaceId, appId], enabled: validId, queryFn: ({ signal }) => read<AppManifest>(`/api/platform/v1/apps/${safeId}`, signal) })
  const health = useQuery({ queryKey: ['app-health', workspaceId, appId], enabled: validId, queryFn: ({ signal }) => read<AppHealth>(`/api/platform/v1/apps/${safeId}/health`, signal) })
  if (!validId) return <Missing />
  return <><PageHeading eyebrow="APPLICATION / STATUS" title={info.data ? brandName(info.data.app_id, info.data.display_name) : '应用状态'} description="核实本机实例后打开工具。无需先建立工作空间。" />
    <QueryState pending={info.isPending || health.isPending} error={info.isError || health.isError}
      retry={() => { void info.refetch(); void health.refetch() }}>
      <div className="panel"><div className="section-head"><Title level={3}>{info.data && brandName(info.data.app_id, info.data.display_name)}</Title><Tag>{info.data?.app_version}</Tag></div>
        <div className="status-list">{(['service', 'ui', 'cpu', 'gpu', 'models'] as const).map(key => <div key={key}>
          <Text>{({ service: '服务', ui: '界面', cpu: 'CPU', gpu: 'GPU', models: '模型' })[key]}</Text>
          <Tag color={health.data?.[key].state === 'ready' ? 'success' : 'default'}>{health.data?.[key].state || 'unknown'}</Tag>
          <Text type="secondary">{health.data?.[key].reason}</Text></div>)}</div></div>
      {info.data && <ApplicationCard key={info.data.app_id} app={info.data} workspaceId={workspaceId} />}
    </QueryState><div className="back-link"><ScopeLink to="/apps">← 返回应用目录</ScopeLink></div></>
}

export function Placeholder({ title, description }: { title: string; description: string }) {
  return <><PageHeading eyebrow="WORKBENCH" title={title} description={description} />
    <div className="empty-panel"><Empty description={`${title}功能尚未开放`} /><ScopeLink to="/apps">查看可用应用 →</ScopeLink></div></>
}
export function Missing() { return <><PageHeading eyebrow="NOT FOUND" title="页面不存在" description="这个地址不属于当前门户。" /><ScopeLink to="/">返回首页 →</ScopeLink></> }
