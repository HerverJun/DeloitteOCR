import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Space, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { brandName } from '../../clear-ui/theme'
import { useNativeWindow } from '../catalog/nativeWindow'

function NativeOutputs({ workspaceId, appId }: { workspaceId: string; appId: 'ocr' | 'classification' }) {
  const base = `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/${appId}`
  const native = useNativeWindow(`outputs:${workspaceId}:${appId}`)
  const projects = useQuery({ queryKey: [`${appId}-projects`, workspaceId], queryFn: async ({ signal }) => {
    const response = await fetch(`${base}/projects`, { signal })
    if (!response.ok) throw new Error('工具尚未连接')
    return response.json() as Promise<{ items: { project_id: string; name: string; link_id: string | null }[] }>
  } })
  if (projects.isError) return <section className="panel"><Space><Typography.Text>{brandName(appId)} 结果</Typography.Text>
    <Link to={`/apps/${appId}?workspace=${encodeURIComponent(workspaceId)}`}>连接工具以查看结果与导出</Link>
    <Button onClick={() => void projects.refetch()}>刷新</Button></Space></section>
  const linked = projects.data?.items.filter(item => item.link_id) || []
  if (!linked.length) return null
  return <section className="panel"><Typography.Title level={3}>{brandName(appId)} 结果与导出</Typography.Title>
    <Typography.Paragraph type="secondary">打开关联作业，在工具内查看处理结果并下载文件。</Typography.Paragraph>
    <div className="artifact-list">{linked.map(item => <article key={item.project_id}><Typography.Text strong>{item.name}</Typography.Text>
      <Button disabled={native.opening} onClick={() => native.open(async signal => {
        const response = await fetch(`${base}/links/${encodeURIComponent(item.link_id!)}/quick-navigation`, { method: 'POST', signal })
        if (!response.ok) throw new Error('无法打开结果，请连接工具后重试。')
        const result = await response.json() as { url: string }
        return { url: result.url, kind: 'ticket' }
      })}>打开结果与导出</Button></article>)}</div>
    {native.error && <Alert type="error" title={native.error} />}
  </section>
}

export function ProjectNativeOutputs({ workspaceId }: { workspaceId: string }) {
  return <>{(['ocr', 'classification'] as const).map(appId => <NativeOutputs key={`${workspaceId}:${appId}`} workspaceId={workspaceId} appId={appId} />)}</>
}
