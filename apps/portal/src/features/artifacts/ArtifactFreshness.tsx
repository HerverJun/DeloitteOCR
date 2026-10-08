import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Space, Tag } from 'antd'
import { Link } from 'react-router-dom'

type Freshness = { kind: 'screen_batch' | 'other'; batch_id?: string; state: 'current' | 'historical' | 'untracked'; reason?: string; current?: boolean; snapshot_id?: string; current_snapshot_id?: string; export_options?: Record<string, unknown> }
export function ArtifactFreshness({ workspaceId, bundleId, inputSetId, projectId, handoffId }: { workspaceId: string; bundleId: string; inputSetId?: string; projectId?: string; handoffId?: string }) {
  const query = useQuery({ queryKey: ['artifact-freshness', workspaceId, bundleId], queryFn: async ({ signal }) => {
    const response = await fetch(`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(bundleId)}/freshness`, { signal })
    if (!response.ok) throw new Error('当前成果版本尚未核实，历史文件仍可下载')
    return response.json() as Promise<Freshness>
  }, refetchInterval: query => query.state.data?.kind === 'screen_batch' ? 5000 : false })
  if (query.error) return <Alert type="warning" title={query.error.message} action={<Button onClick={() => void query.refetch()}>核实当前版本</Button>} />
  if (query.isPending) return <p>正在核实成果是否仍对应当前结果…</p>
  const value = query.data
  if (!value || value.kind !== 'screen_batch') return null
  const params = new URLSearchParams({ workspace: workspaceId, screen_batch: value.batch_id || '' })
  if (inputSetId) params.set('set', inputSetId)
  if (projectId) params.set('project', projectId)
  if (handoffId) params.set('handoff', handoffId)
  return <section aria-label="屏拍成果当前版本">
    <Space wrap><Tag color={value.state === 'current' ? 'success' : 'warning'}>{value.state === 'current' ? '当前导出版本' : '历史导出版本'}</Tag>
      <Link to={`/apps/ocr/workflow?${params}`}>{value.state === 'current' ? '返回批次查看当前结果' : '返回当前批次重新导出'}</Link></Space>
    {value.state === 'historical' && <Alert type="warning" title="原图、选定候选、复核状态或导出设置已变化" description="这份成果保留历史版本，可继续下载；请回批次核对当前范围和数值列，再重新导出。" />}
    <details><summary>导出版本与选项</summary><p>导出快照：{value.snapshot_id}</p><p>当前快照：{value.current_snapshot_id}</p><pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(value.export_options, null, 2)}</pre></details>
  </section>
}
