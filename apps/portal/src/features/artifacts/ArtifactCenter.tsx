import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Pagination, Space, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import './artifacts.css'

const { Title, Text } = Typography
type Input = { role: string; name: string; asset_id: string; revision: number }
type File = { name: string; artifact_id: string; revision: number; size_bytes: number; availability: string }
type Bundle = { bundle_id: string; session_id: string; session_name: string; export_id: string;
  result_id: string | null; created_at: string; input_set_id: string; input_set_revision: number;
  inputs: Input[]; files: File[] }
type BundlePage = { items: Bundle[]; total: number; next_offset: number | null }

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal })
  if (!response.ok) throw new Error(`成果读取失败（${response.status}）`)
  return response.json() as Promise<T>
}

function size(value: number) {
  return value < 1024 ? `${value} B` : value < 1024 * 1024
    ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`
}

function BundleDetails({ bundle, workspaceId, base }: { bundle: Bundle; workspaceId: string; base: string }) {
  const scope = encodeURIComponent(workspaceId)
  return <section className="artifact-detail">
    <div className="artifact-detail-head"><div><Tag>{bundle.result_id ? '差异结果' : '清洗表'}</Tag>
      <Title level={2}>{bundle.session_name}</Title><Text type="secondary">生成于 {new Date(bundle.created_at).toLocaleString('zh-CN')}</Text></div>
      <Link to={`/apps/data?workspace=${scope}&session=${encodeURIComponent(bundle.session_id)}`}>返回数据作业</Link></div>
    <div className="artifact-meta"><div><Text type="secondary">输入集合</Text><strong>版本 {bundle.input_set_revision}</strong></div>
      <div><Text type="secondary">成果文件</Text><strong>{bundle.files.length} 件</strong></div>
      <div><Text type="secondary">作业类型</Text><strong>{bundle.result_id ? '表格比对' : '表格清洗'}</strong></div></div>
    <div className="artifact-section"><Title level={3}>输入资料</Title>
      <ul>{bundle.inputs.map(input => <li key={`${input.role}-${input.asset_id}`}><span>{input.name}</span>
        <Text type="secondary">版本 {input.revision}</Text>
        <Link to={`/assets?workspace=${scope}&asset=${encodeURIComponent(input.asset_id)}`}>查看来源</Link></li>)}</ul></div>
    <div className="artifact-section"><Title level={3}>成果文件</Title>
      <ul>{bundle.files.map(file => <li key={file.artifact_id}><span>{file.name}</span>
        <Text type="secondary">版本 {file.revision} · {size(file.size_bytes)}</Text>
        {file.availability === 'available'
          ? <a href={`${base}/${encodeURIComponent(bundle.bundle_id)}/files/${encodeURIComponent(file.name)}`}>下载</a>
          : <Tag color="error">不可用</Tag>}</li>)}</ul></div>
  </section>
}

export function ArtifactCenter() {
  const [params] = useSearchParams()
  const workspaceId = params.get('workspace')
  const bundleId = params.get('bundle')
  const [page, setPage] = useState(1)
  const base = workspaceId ? `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts` : ''
  const list = useQuery({ queryKey: ['artifact-bundles', workspaceId, page], enabled: !!workspaceId && !bundleId,
    queryFn: ({ signal }) => read<BundlePage>(`${base}?offset=${(page - 1) * 20}&limit=20`, signal) })
  const detail = useQuery({ queryKey: ['artifact-bundle', workspaceId, bundleId], enabled: !!workspaceId && !!bundleId,
    queryFn: ({ signal }) => read<Bundle>(`${base}/${encodeURIComponent(bundleId || '')}`, signal) })
  return <div className="artifact-center"><div className="artifact-heading"><Text className="eyebrow">OUTPUTS / WORKSPACE</Text>
    <Title level={1}>成果中心</Title></div>
    {!workspaceId ? <Empty description="先在顶部选择工作空间" /> : bundleId ? <>
      <div className="artifact-back"><Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}`}>返回全部成果</Link></div>
      {detail.isPending && <Text>正在读取成果…</Text>}
      {detail.isError && <Alert type="error" title={detail.error.message}
        action={<Button onClick={() => void detail.refetch()}>重试</Button>} />}
      {detail.data && <BundleDetails bundle={detail.data} workspaceId={workspaceId} base={base} />}
    </> : <>
      {list.isError && <Alert type="error" title={list.error.message}
        action={<Button onClick={() => void list.refetch()}>重试</Button>} />}
      {list.isPending && <Text>正在读取成果…</Text>}
      {list.data?.total === 0 && <Empty description="当前工作空间暂无成果" />}
      <div className="artifact-list">{list.data?.items.map(bundle => <article key={bundle.bundle_id}>
        <div><Tag>{bundle.result_id ? '差异结果' : '清洗表'}</Tag><Title level={3}>{bundle.session_name}</Title>
          <Text type="secondary">{new Date(bundle.created_at).toLocaleString('zh-CN')}</Text></div>
        <div className="artifact-list-input"><Text type="secondary">输入</Text><span>{bundle.inputs.map(item => item.name).join(' · ')}</span></div>
        <Space><Text type="secondary">{bundle.files.length} 件文件</Text>
          <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(bundle.bundle_id)}`}>查看成果</Link></Space>
      </article>)}</div>
      {(list.data?.total || 0) > 20 && <Pagination current={page} pageSize={20} total={list.data?.total}
        onChange={setPage} showSizeChanger={false} />}
    </>}
  </div>
}
