import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Alert, Button, Pagination, Space, Typography } from 'antd'
import { Link } from 'react-router-dom'

type Output = { session_id: string; session_name: string; export_id: string; created_at: string; files: { name: string; bytes: number }[] }
export function ProjectDataOutputs({ workspaceId }: { workspaceId: string }) {
  const [page, setPage] = useState(1)
  const query = useQuery({ queryKey: ['project-data-outputs', workspaceId, page], queryFn: async ({ signal }) => {
    const response = await fetch(`/api/platform/v1/data/outputs?workspace_id=${encodeURIComponent(workspaceId)}&offset=${(page - 1) * 20}&limit=20`, { signal })
    if (!response.ok) throw new Error('数据输出读取失败')
    return response.json() as Promise<{ items: Output[]; total: number }>
  }, refetchInterval: 5000 })
  if (query.isError) return <Alert type="error" title="数据输出读取失败" action={<Button onClick={() => void query.refetch()}>重试</Button>} />
  if (!query.data?.total) return null
  return <section className="panel"><Typography.Title level={3}>ClearData 输出</Typography.Title>
    <div className="artifact-list">{query.data.items.map(item => <article key={item.export_id}>
      <div><Typography.Text strong>{item.session_name}</Typography.Text><Typography.Text type="secondary">{new Date(item.created_at).toLocaleString()}</Typography.Text></div>
      <Space wrap>{item.files.filter(file => /\.(csv|xlsx|parquet)$/.test(file.name)).map(file => <a key={file.name}
        href={`/api/platform/v1/data/sessions/${encodeURIComponent(item.session_id)}/exports/${encodeURIComponent(item.export_id)}/${encodeURIComponent(file.name)}`} download>{file.name}</a>)}</Space>
      <Link to={`/apps/data?workspace=${encodeURIComponent(workspaceId)}&session=${encodeURIComponent(item.session_id)}`}>继续处理</Link>
    </article>)}</div>
    <Pagination aria-label="数据输出分页" current={page} pageSize={20} total={query.data.total} onChange={setPage} showSizeChanger={false} hideOnSinglePage />
  </section>
}
