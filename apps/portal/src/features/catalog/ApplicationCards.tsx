import { ToolIdentity } from './ToolIdentity'
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Progress, Skeleton, Space, Tag, Typography } from 'antd'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import type { AppHealth, AppInstallation, AppManifest, LaunchReceipt, RecentWork, RecentWorkPage } from '../../../../../packages/contracts/types.generated'

import { brandName, applicationDescriptions } from '../../clear-ui/theme'
import { nativeAddress, platformHomeAddress } from './nativeWindow'
import { ApplicationDeclaration, healthReasonText } from './ApplicationDeclaration'

const { Text, Paragraph } = Typography
const ID = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/

async function response<T>(path: string, signal?: AbortSignal): Promise<T | null> {
  const result = await fetch(path, { signal, headers: { Accept: 'application/json' } })
  if (result.status === 404) return null
  if (!result.ok) throw new Error(`服务请求失败（${result.status}）`)
  return result.json() as Promise<T>
}

function liveAddress(receipt: LaunchReceipt): string {
  return nativeAddress({ url: receipt.base_url, kind: 'base' })
}

function pendingKey(appId: string) { return `workbench:pending-launch:${appId}` }
function newLaunchId() { return `l${crypto.randomUUID().replaceAll('-', '')}` }
function elapsed(seconds: number) {
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export function ApplicationCard({ app, workspaceId, projectMode = false }: { app: AppManifest; workspaceId: string | null; projectMode?: boolean }) {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const context = new URLSearchParams(params)
  if (workspaceId) context.set('workspace', workspaceId)
  else context.delete('workspace')
  const client = useQueryClient()
  const safeId = encodeURIComponent(app.app_id)
  const health = useQuery({ queryKey: ['app-health', workspaceId, app.app_id],
    queryFn: ({ signal }) => response<AppHealth>(`/api/platform/v1/apps/${safeId}/health`, signal), refetchInterval: 15_000 })
  const installation = useQuery({ queryKey: ['app-installation', app.app_id],
    queryFn: ({ signal }) => response<AppInstallation>(`/api/platform/v1/apps/${safeId}/installation`, signal),
    refetchInterval: query => query.state.data?.state === 'verifying' ? 2000 : false })
  const verificationState = installation.data?.state
  const previousVerification = useRef(verificationState)
  useEffect(() => {
    if (previousVerification.current === 'verifying' && verificationState && verificationState !== 'verifying') {
      void health.refetch()
    }
    previousVerification.current = verificationState
  }, [verificationState, health.refetch])
  const current = useQuery({ queryKey: ['app-current', app.app_id],
    queryFn: ({ signal }) => response<LaunchReceipt>(`/api/platform/v1/apps/${safeId}/launches/current`, signal),
    staleTime: 0, refetchInterval: 15_000 })
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
      if (projectMode && workspaceId) { launch.mutate({ action }); return }
      popupRef.current = window.open('', '_blank')
      if (popupRef.current) popupRef.current.opener = null
      else {
        inFlightRef.current = false
        if (current.data) setVerified(current.data)
        setFailure('未能创建新窗口；请允许弹出窗口后再次核实并打开。')
        return
      }
    } catch {
      inFlightRef.current = false
      setFailure('未能创建新窗口；请允许弹出窗口后再次核实并打开。')
      return
    }
    launch.mutate({ action })
  }
  const open = async (receipt: LaunchReceipt) => {
    if (projectMode && workspaceId) {
      navigate(`/apps/${encodeURIComponent(app.app_id)}/start?${context}`)
      setFailure('')
      return
    }
    let address = liveAddress(receipt)
    setVerified(receipt)
    const popup = popupRef.current
    if (popup) {
      if (app.app_id === 'ocr' || app.app_id === 'classification') {
        const result = await fetch(`/api/platform/v1/apps/${safeId}/launches/${encodeURIComponent(receipt.launch_id)}/navigation`,
          { method: 'POST', headers: { Accept: 'application/json' } })
        if (!result.ok) throw new Error(`${brandName(app.app_id)} 工作台连接暂不可用，请重新核实并打开。`)
        const value = await result.json() as { url: string }
        address = nativeAddress({ url: value.url, baseUrl: address, kind: app.app_id === 'ocr' ? 'session' : 'base' })
        address = platformHomeAddress(address, receipt.instance_id, window.location.origin, workspaceId)
      }
      if (!mountedRef.current) return
      popup.location.replace(address)
      popupRef.current = null
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
  }, onSuccess: async receipt => {
    if (!mountedRef.current) return
    remember(null)
    refresh()
    try { await open(receipt) } catch (error) {
      popupRef.current?.close(); popupRef.current = null
      setFailure(error instanceof Error ? error.message : '服务地址不可用')
    }
  }, onError: error => {
    if (!mountedRef.current) return
    popupRef.current?.close(); popupRef.current = null
    setFailure(error instanceof Error ? error.message : '启动状态无法确认')
    refresh()
  }, onSettled: () => { inFlightRef.current = false } })
  const missing = installation.data?.installed === false
  const verifying = installation.data?.state === 'verifying'
  const storageUnavailable = installation.data?.state === 'storage_unavailable'
  const progress = installation.data?.verification
  const verificationFailed = progress?.phase === 'failed'
  const verificationPassed = progress?.phase === 'passed' || progress?.phase === 'cached'
  const percent = progress?.total_files ? Math.floor(progress.checked_files / progress.total_files * 100) : 0
  const launchReady = !verifying && installation.data?.launch_ready === true
  const serviceUnavailable = health.data?.service?.state === 'unavailable'
  const awaitingStartup = launchReady && !current.data && health.data?.service?.state === 'unknown'
    && health.data.service.reason === 'Service has not been probed'
  const serviceSummary = !verifying && !verificationFailed && !missing && !storageUnavailable && !current.data && serviceUnavailable
    ? healthReasonText(health.data?.service?.reason)
    : awaitingStartup ? '按需启动，打开时检查运行状态'
      : progress?.message || (verifying ? '应用文件校验中，可先使用数据工作台' : missing ? healthReasonText(health.data?.service?.reason) || '本机未登记安装包' : storageUnavailable ? '请配置本机数据目录后启动' : current.data ? `实例 ${current.data.instance_id}` : healthReasonText(health.data?.service?.reason))
  const linked = `/apps/${safeId}${context.size ? `?${context}` : ''}`
  const presentation = applicationDescriptions[app.app_id]
  return <Card className="application-card" title={<ToolIdentity appId={app.app_id} to={linked} label={brandName(app.app_id, app.display_name)} />}>
    <Text className="application-purpose">{presentation?.purpose || app.display_name}</Text>
    <Paragraph className="application-description" type="secondary">{presentation?.description || '查看工具信息，核实本机服务后开始处理资料。'}</Paragraph>
    {health.isPending || current.isPending || installation.isPending ? <Skeleton active paragraph={{ rows: 2 }} /> :
      health.isError || current.isError || installation.isError || !installation.data ?
        <Alert type="error" title="无法核实应用状态" action={<Button onClick={() => { void health.refetch(); void current.refetch(); void installation.refetch() }}>重试</Button>} /> : <>
        <div className="application-state"><Tag color={verificationFailed ? 'error' : verifying && !verificationPassed ? 'processing' : current.data ? 'success' : missing || storageUnavailable || serviceUnavailable ? 'error' : awaitingStartup ? 'default' : verificationPassed ? 'success' : 'default'}>
          {verificationFailed ? '校验失败' : verifying ? progress?.phase === 'waiting' ? '等待校验' : verificationPassed ? '校验完成' : '正在校验' : current.data ? '实例已核实' : missing ? '未安装' : storageUnavailable ? '存储未配置' : serviceUnavailable ? '暂不可用' : awaitingStartup ? '待启动' : verificationPassed ? '已校验' : '尚未运行'}</Tag>
          <Text type="secondary">{serviceSummary}</Text>
        </div>
        {progress && progress.phase !== 'cached' && <div role="status" aria-live="polite" className="application-verification">
          <Progress percent={percent} status={verificationFailed ? 'exception' : progress.phase === 'checking' ? 'active' : progress.phase === 'passed' ? 'success' : 'normal'}
            aria-label={`${brandName(app.app_id, app.display_name)}文件校验进度`} />
          <Paragraph type="secondary">已校验 {progress.checked_files.toLocaleString()} / {progress.total_files.toLocaleString()} 个文件 · 已用时 {elapsed(progress.elapsed_seconds)}</Paragraph>
          {progress.current_file && <Paragraph type="secondary" style={{ overflowWrap: 'anywhere', fontSize: 13 }}>当前文件：{progress.current_file}</Paragraph>}
          {verifying && <Paragraph type="secondary">完整校验仅在首次启动或程序包变更时进行，可先使用数据工作台。</Paragraph>}
        </div>}
        <Space wrap className="application-actions">
          <Button type="primary" disabled={!launchReady} loading={launch.isPending}
            onClick={() => begin(pendingRef.current ? 'verify' : current.data ? 'open' : 'start')}>
            {pending ? '核实原请求' : current.data ? '打开工具' : '启动并打开'}</Button>
          {pending && <Button disabled={launch.isPending || !launchReady} onClick={() => begin('new')}>明确新启动</Button>}
        </Space>
        {failure && <Alert className="application-message" type="warning" title={failure} />}
        {verified && <Paragraph className="verified-address"><Button type="link" disabled={launch.isPending || !launchReady} onClick={() => begin('open')}>再次核实并打开 ↗</Button></Paragraph>}
      </>}
    <ApplicationDeclaration declaration={app} health={health.data} />
  </Card>
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
          <Text strong>{brandName(item.app_id)}</Text><Text type="secondary">{item.source === 'platform_launch' ? '平台已核验启动' : '原生项目已打开'}</Text>
          <Text type="secondary">{item.module_project_id || item.launch_id}</Text>
          <Text type="secondary">{new Date(item.opened_at).toLocaleString()}</Text>
          <Link to={`/apps/${encodeURIComponent(item.app_id)}${scope ? `?workspace=${encodeURIComponent(scope)}` : ''}`}>继续（先核实实例）→</Link>
        </div>)}
      </div>}
  </section>
}
