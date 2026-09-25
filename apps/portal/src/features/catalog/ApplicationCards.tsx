import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Skeleton, Space, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import type { AppHealth, AppInstallation, AppManifest, LaunchReceipt, RecentWork, RecentWorkPage } from '../../../../../packages/contracts/types.generated'

const { Text, Paragraph } = Typography
const ID = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/
const BASE = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/?$/

async function response<T>(path: string, signal?: AbortSignal): Promise<T | null> {
  const result = await fetch(path, { signal, headers: { Accept: 'application/json' } })
  if (result.status === 404) return null
  if (!result.ok) throw new Error(`服务请求失败（${result.status}）`)
  return result.json() as Promise<T>
}

function liveAddress(receipt: LaunchReceipt): string {
  const match = BASE.exec(receipt.base_url)
  if (!match || Number(match[1]) > 65535) throw new Error('服务地址未通过本机校验')
  return receipt.base_url
}

function pendingKey(appId: string) { return `workbench:pending-launch:${appId}` }
function newLaunchId() { return `l${crypto.randomUUID().replaceAll('-', '')}` }

export function ApplicationCard({ app, workspaceId }: { app: AppManifest; workspaceId: string | null }) {
  const client = useQueryClient()
  const safeId = encodeURIComponent(app.app_id)
  const health = useQuery({ queryKey: ['app-health', workspaceId, app.app_id],
    queryFn: ({ signal }) => response<AppHealth>(`/api/platform/v1/apps/${safeId}/health`, signal) })
  const installation = useQuery({ queryKey: ['app-installation', app.app_id],
    queryFn: ({ signal }) => response<AppInstallation>(`/api/platform/v1/apps/${safeId}/installation`, signal) })
  const current = useQuery({ queryKey: ['app-current', app.app_id],
    queryFn: ({ signal }) => response<LaunchReceipt>(`/api/platform/v1/apps/${safeId}/launches/current`, signal),
    staleTime: 0 })
  const [pending, setPending] = useState(() => sessionStorage.getItem(pendingKey(app.app_id)))
  const pendingRef = useRef(pending)
  const popupRef = useRef<Window | null>(null)
  const inFlightRef = useRef(false)
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      popupRef.current?.close()
      popupRef.current = null
    }
  }, [])
  const [verified, setVerified] = useState<LaunchReceipt | null>(null)
  const [failure, setFailure] = useState('')
  const remember = (id: string | null) => {
    pendingRef.current = id; setPending(id)
    if (id) sessionStorage.setItem(pendingKey(app.app_id), id)
    else sessionStorage.removeItem(pendingKey(app.app_id))
  }
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['app-current', app.app_id] })
    void client.invalidateQueries({ queryKey: ['app-health', workspaceId, app.app_id] })
    void client.invalidateQueries({ queryKey: ['recent-work'] })
  }
  const begin = (action: 'start' | 'verify' | 'new' | 'open') => {
    if (inFlightRef.current) return
    inFlightRef.current = true
    // Reserve a blank window synchronously with the user gesture; it cannot
    // navigate to a stale native address before the live GET completes.
    try {
      popupRef.current = window.open('', '_blank')
      if (popupRef.current) popupRef.current.opener = null
    } catch {
      inFlightRef.current = false
      setFailure('未能创建新窗口；请允许弹出窗口后再次核实并打开。')
      return
    }
    launch.mutate({ action })
  }
  const open = (receipt: LaunchReceipt) => {
    const address = liveAddress(receipt)
    setVerified(receipt)
    const popup = popupRef.current
    popupRef.current = null
    if (popup) {
      popup.location.replace(address)
      setFailure('')
    } else {
      setFailure('未能创建新窗口；请允许弹出窗口后再次核实并打开。')
    }
  }
  const launch = useMutation({ mutationFn: async ({ action }: { action: 'start' | 'verify' | 'new' | 'open' }) => {
    setFailure('')
    if (action === 'new') remember(null)
    if (action !== 'verify') {
      const currentResponse = await response<LaunchReceipt>(`/api/platform/v1/apps/${safeId}/launches/current`)
      if (currentResponse) return currentResponse
      if (action === 'open') throw new Error('原实例已退出；请明确发起新启动。')
    }
    const id = pendingRef.current || newLaunchId()
    if (!ID.test(id)) throw new Error('待核实请求身份无效')
    if (!pendingRef.current) remember(id)
    const path = `/api/platform/v1/apps/${safeId}/launches/${encodeURIComponent(id)}`
    // A lost POST may have started a service but failed to persist its recent
    // event, or never reached the server. Retry the exact same durable ID.
    const result = await fetch(path, { method: 'POST', headers: { Accept: 'application/json' } })
    if (result.status === 409) {
      const active = await response<LaunchReceipt>(`/api/platform/v1/apps/${safeId}/launches/current`)
      if (active) return active
    }
    if (!result.ok) {
      if (result.status === 503) throw new Error('启动条件或服务暂不可用；请检查本机安装与存储状态后重试。')
      if (result.status === 410) throw new Error('原启动实例已退出；可明确发起新启动。')
      throw new Error(`启动失败（${result.status}）；可核实原请求。`)
    }
    return result.json() as Promise<LaunchReceipt>
  }, onSuccess: receipt => {
    if (!mountedRef.current) return
    remember(null)
    refresh()
    try { open(receipt) } catch (error) {
      popupRef.current?.close(); popupRef.current = null
      setFailure(error instanceof Error ? error.message : '服务地址不可用')
    }
  }, onError: error => {
    if (!mountedRef.current) return
    popupRef.current?.close(); popupRef.current = null
    setFailure(error instanceof Error ? error.message : '启动状态无法确认')
  }, onSettled: () => { inFlightRef.current = false } })
  const missing = installation.data?.installed === false
  const storageUnavailable = installation.data?.state === 'storage_unavailable'
  const launchReady = installation.data?.launch_ready === true
  const linked = `/apps/${safeId}${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ''}`
  return <Card className="application-card" title={<Link to={linked}>{app.display_name}</Link>} extra={<Tag>{app.app_id}</Tag>}>
    {health.isPending || current.isPending || installation.isPending ? <Skeleton active paragraph={{ rows: 2 }} /> :
      health.isError || current.isError || installation.isError || !installation.data ?
        <Alert type="error" title="无法核实应用状态" action={<Button onClick={() => { void health.refetch(); void current.refetch(); void installation.refetch() }}>重试</Button>} /> : <>
        <Paragraph type="secondary">输入：{app.input_types.join('、') || '由工具定义'} · 输出：{app.output_types.join('、') || '由工具定义'}<br />
          声明能力：{app.capabilities.join('、') || '无'} · 资源：{app.resources.join('、') || '由工具报告'}</Paragraph>
        <div className="application-state"><Tag color={current.data ? 'success' : missing || storageUnavailable ? 'error' : 'default'}>
          {current.data ? '实例已核实' : missing ? '未安装' : storageUnavailable ? '存储未配置' : health.data?.service?.state === 'unavailable' ? '暂不可用' : '尚未运行'}</Tag>
          <Text type="secondary">{missing ? '本机未登记安装包' : storageUnavailable ? '请配置本机数据目录后启动' : current.data ? `实例 ${current.data.instance_id}` : health.data?.service?.reason}</Text>
        </div>
        <Space wrap>
          <Button type="primary" disabled={!launchReady} loading={launch.isPending}
            onClick={() => begin(pendingRef.current ? 'verify' : 'start')}>
            {pending ? '核实原请求' : current.data ? '打开工具' : '启动并打开'}</Button>
          {pending && <Button disabled={launch.isPending || !launchReady} onClick={() => begin('new')}>明确新启动</Button>}
        </Space>
        {failure && <Alert className="application-message" type="warning" title={failure} />}
        {verified && <Paragraph className="verified-address"><Button type="link" disabled={launch.isPending || !launchReady} onClick={() => begin('open')}>再次核实并打开 ↗</Button></Paragraph>}
      </>}
  </Card>
}

export function ApplicationCards({ apps, workspaceId }: { apps: AppManifest[]; workspaceId: string | null }) {
  return <div className="app-grid">{apps.map(app => <ApplicationCard key={app.app_id} app={app} workspaceId={workspaceId} />)}</div>
}

export function RecentWorks({ workspaceId }: { workspaceId: string | null }) {
  const [search] = useSearchParams()
  const scope = workspaceId && search.get('workspace') === workspaceId ? workspaceId : null
  const recent = useQuery({ queryKey: ['recent-work', scope], queryFn: async ({ signal }) => {
    const result: RecentWork[] = []
    let cursor: string | null = null
    let revision: number | null = null
    do {
      const query = new URLSearchParams({ limit: '200' })
      if (scope) query.set('workspace_id', scope)
      if (cursor && revision !== null) { query.set('cursor', cursor); query.set('snapshot_revision', String(revision)) }
      const page = await response<RecentWorkPage>(`/api/platform/v1/recent-work?${query}`, signal)
      if (!page || (revision !== null && page.snapshot_revision !== revision)) throw new Error('最近记录已更新')
      result.push(...page.items); revision = page.snapshot_revision; cursor = page.next_cursor
    } while (cursor)
    return result
  } })
  return <section className="recent-work"><div className="section-head"><Typography.Title level={3}>最近工作</Typography.Title>
    <Text type="secondary">{scope ? '当前工作空间' : '直接打开的工具'}</Text></div>
    {recent.isPending ? <Skeleton active paragraph={{ rows: 2 }} /> : recent.isError ?
      <Alert type="error" title="无法读取最近记录" action={<Button onClick={() => void recent.refetch()}>重试</Button>} /> :
      !recent.data?.length ? <Empty description="没有已核验的最近操作" /> : <div className="recent-list">
        {recent.data.map(item => <div key={item.recent_id}>
          <Text strong>{item.app_id}</Text><Text type="secondary">{item.source === 'platform_launch' ? '平台已核验启动' : '原生项目已打开'}</Text>
          <Text type="secondary">{item.module_project_id || item.launch_id}</Text>
          <Text type="secondary">{new Date(item.opened_at).toLocaleString()}</Text>
          <Link to={`/apps/${encodeURIComponent(item.app_id)}${scope ? `?workspace=${encodeURIComponent(scope)}` : ''}`}>继续（先核实实例）→</Link>
        </div>)}
      </div>}
  </section>
}
