import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Select, Space, Spin, Tag, Typography } from 'antd'
import { useSearchParams } from 'react-router-dom'

import { brandName } from '../../clear-ui/theme'
import { InspectionTasks } from '../forensics/ForensicWorkbench'
import { useSession } from '../tasks/session'
import { TaskHistory } from '../tasks/TaskHistory'

const { Title, Text, Paragraph } = Typography

type NativeError = { native_item_id: string; message: string }
type Counts = { total: number | null; succeeded: number; failed: number; cancelled: number; pending: number }
export type Job = {
  platform_job_id: string; app_id: string; module_project_id: string; link_id: string
  native_job_ids: string[]; status: string; native_status: string | null; stage: string
  counts: Counts; progress_done: number | null; progress_total: number | null
  capabilities: string[]; availability: string; native_errors: NativeError[]; updated_at: string
}
type JobPage = { items: Job[]; next_offset: number | null }
type JobLink = { link_id: string; app_id: string; module_project_id: string; role: string }
type ActionResponse = { request_state: 'sent' | 'unknown'; job: Job; new_job?: Job }

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { Accept: 'application/json', ...init?.headers } })
  if (!response.ok) throw new Error(`服务请求失败（${response.status}）`)
  return response.json() as Promise<T>
}

export function actionLabel(action: string): string {
  return ({ cancel: '取消', pause: '暂停', resume: '继续', retry_failed: '重试失败项' } as Record<string, string>)[action] || action
}

export function jobProgress(job: Job): string {
  const { total, succeeded, failed, cancelled, pending } = job.counts
  if (total === null) return job.progress_total === null ? '原生阶段进度未提供' : `阶段进度 ${job.progress_done ?? 0}/${job.progress_total}`
  return `共 ${total} · 完成 ${succeeded} · 失败 ${failed} · 取消 ${cancelled} · 未完成 ${pending}`
}

function JobRow({ job, workspaceId, onRefresh }: { job: Job; workspaceId: string; onRefresh: () => void }) {
  const [notice, setNotice] = useState<string | null>(null)
  const action = useMutation({ mutationFn: (name: string) => json<ActionResponse>(
    `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/jobs/${encodeURIComponent(job.platform_job_id)}/actions/${name}`,
    { method: 'POST' }), onSuccess: result => {
      setNotice(result.request_state === 'sent'
        ? '请求已送达原生工具；等待刷新确认实际状态。'
        : '响应不明；请刷新核实原生状态，暂勿重复操作。')
      window.setTimeout(onRefresh, 1200)
    }, onError: () => setNotice('请求未确认；请刷新核实后再操作。') })
  return <article className="panel" aria-label={`${job.app_id} 任务 ${job.platform_job_id}`}>
    <div className="section-head"><Title level={3}>{brandName(job.app_id)} · {job.module_project_id}</Title>
      <Space><Tag>{job.status}</Tag><Tag color={job.availability === 'ready' ? 'success' : 'warning'}>{job.availability}</Tag></Space></div>
    <Paragraph><Text strong>阶段：</Text>{job.stage || '原生服务未提供'} <Text type="secondary">· 原生状态 {job.native_status || '未取得'}</Text></Paragraph>
    <Paragraph>{jobProgress(job)}</Paragraph>
    <Paragraph type="secondary">原生任务：{job.native_job_ids.join('、')}</Paragraph>
    {job.stage.toLowerCase().includes('gpu') && <Paragraph>GPU 等待信息：{job.stage}</Paragraph>}
    {job.native_errors?.length > 0 && <div aria-label="原生错误"><Text strong>原生错误</Text><ul>
      {job.native_errors.map((error, index) => <li key={`${error.native_item_id}-${index}`}>{error.native_item_id}：{error.message}</li>)}
    </ul></div>}
    <Space wrap><Button onClick={onRefresh}>刷新状态</Button>
      {job.capabilities.map(name => <Button key={name} disabled={action.isPending || job.availability !== 'ready'}
        onClick={() => action.mutate(name)}>{actionLabel(name)}</Button>)}
    </Space>
    {notice && <Alert className="application-message" type={notice.includes('已送达') ? 'info' : 'warning'} title={notice} />}
  </article>
}

export function TaskCenter() {
  const { multiuser } = useSession()
  return multiuser ? <TaskHistory /> : <LegacyTaskCenter />
}

function LegacyTaskCenter() {
  const [params] = useSearchParams()
  const workspaceId = params.get('workspace')
  const client = useQueryClient()
  const [linkId, setLinkId] = useState<string | undefined>()
  const [nativeIds, setNativeIds] = useState('')
  const [offset, setOffset] = useState(0)
  useEffect(() => { setOffset(0); setLinkId(undefined) }, [workspaceId])
  const base = workspaceId ? `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/jobs` : ''
  const list = useQuery({ queryKey: ['jobs', workspaceId, offset], enabled: !!workspaceId,
    queryFn: ({ signal }) => json<JobPage>(`${base}?limit=50&offset=${offset}`, { signal }), refetchInterval: 15_000 })
  const links = useQuery({ queryKey: ['job-links', workspaceId], enabled: !!workspaceId,
    queryFn: ({ signal }) => json<{ items: JobLink[] }>(`${base}/links`, { signal }) })
  const chosen = links.data?.items.find(item => item.link_id === linkId)
  const register = useMutation({ mutationFn: () => json<Job>(base, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ link_id: linkId, app_id: chosen?.app_id,
      native_job_ids: nativeIds.split(/[\s,，]+/).filter(Boolean) }),
  }), onSuccess: () => { setNativeIds(''); setOffset(0); void client.invalidateQueries({ queryKey: ['jobs', workspaceId] }) } })
  const refresh = () => void client.invalidateQueries({ queryKey: ['jobs', workspaceId] })

  return <><div className="page-heading"><Text className="eyebrow">TASKS / NATIVE</Text>
    <Title level={1}>任务中心</Title><Paragraph>核实原生工具的任务状态、错误和可用操作。控制请求送达后仍需再次刷新确认。</Paragraph></div>
    {!workspaceId ? <Empty description="先在顶部选择工作空间" /> : <><InspectionTasks key={workspaceId} workspaceId={workspaceId} />
      <section className="panel"><div className="section-head"><Title level={3}>跟踪原生任务</Title><Text type="secondary">仅已关联的项目</Text></div>
        <Space wrap><Select aria-label="关联项目" placeholder="选择关联项目" style={{ minWidth: 260 }} value={linkId}
          onChange={setLinkId} loading={links.isPending} options={links.data?.items.map(link => ({
            value: link.link_id, label: `${link.app_id} / ${link.module_project_id}`,
            disabled: link.role === 'viewer',
          }))} />
          <Input aria-label="原生任务 ID" style={{ width: 270 }} value={nativeIds} onChange={event => setNativeIds(event.target.value)}
            placeholder="原生任务 ID，多个用逗号分隔" />
          <Button onClick={() => register.mutate()} loading={register.isPending}
            disabled={!chosen || !nativeIds.trim()}>加入任务中心</Button></Space>
        {links.isError && <Alert className="application-message" type="error" title="关联项目读取失败" action={<Button onClick={() => void links.refetch()}>重试</Button>} />}
        {register.isError && <Alert className="application-message" type="error" title="任务登记失败，请核实原生任务 ID 与当前实例。" />}
      </section>
      <section><div className="section-head"><Title level={3}>已跟踪任务</Title><Button onClick={refresh}>刷新全部</Button></div>
        {list.isPending ? <Spin aria-label="正在加载任务" /> : list.isError ?
          <Alert type="error" title="任务读取失败" action={<Button onClick={() => void list.refetch()}>重试</Button>} /> :
          list.data?.items.length ? <>{list.data.items.map(job => <JobRow key={job.platform_job_id} job={job}
            workspaceId={workspaceId} onRefresh={refresh} />)}
            <Space><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button>
              <Text type="secondary">第 {Math.floor(offset / 50) + 1} 页</Text>
              <Button disabled={list.data.next_offset === null} onClick={() => setOffset(list.data.next_offset!)}>下一页</Button></Space></> :
            <><Empty description={offset ? '这一页没有任务' : '此工作空间还没有已跟踪任务'} />
              {offset > 0 && <Button onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button>}</>}
      </section></>}
  </>
}
