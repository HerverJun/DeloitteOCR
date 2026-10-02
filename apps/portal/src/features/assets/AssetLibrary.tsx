import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { useSearchParams } from 'react-router-dom'
import './assets.css'

const { Title, Text } = Typography
type Asset = {
  asset_id: string; original_name: string; media_type: string; size_bytes: number
  revision: number; created_at: string; tags: string[]
  source: { root_id: string; relative_path: string }
}
type AssetPage = { items: Asset[]; total: number; next_offset: number | null;
  facets: { media_types: string[]; tags: string[] } }
type AssetSet = { asset_set_id: string; revision: number; members: Asset[] }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(typeof error.detail === 'string' ? error.detail : `操作未完成（${response.status}）`)
  }
  return response.json() as Promise<T>
}

function bytes(value: number) {
  return value < 1024 ? `${value} B` : value < 1024 * 1024
    ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`
}

export function AssetLibrary() {
  const [params, setParams] = useSearchParams()
  const workspaceId = params.get('workspace')
  const setId = params.get('set')
  const focusedId = params.get('asset')
  const client = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [mediaType, setMediaType] = useState<string>()
  const [tag, setTag] = useState<string>()
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string[]>([])
  const [source, setSource] = useState<Asset | null>(null)
  const [editing, setEditing] = useState<Asset | null>(null)
  const [editTags, setEditTags] = useState<string[]>([])
  useEffect(() => { setSelected([]); setPage(1); setKeyword(''); setKeywordInput(''); setMediaType(undefined); setTag(undefined) }, [workspaceId])
  useEffect(() => { setPage(1) }, [keyword, mediaType, tag])
  const base = workspaceId ? `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/assets` : ''
  const focused = useQuery({ queryKey: ['asset-focus', workspaceId, focusedId], enabled: !!workspaceId && !!focusedId,
    queryFn: ({ signal }) => request<Asset>(`${base}/${encodeURIComponent(focusedId || '')}`, { signal }) })
  const shownSource = source || focused.data || null
  const listing = useQuery({ queryKey: ['assets', workspaceId, keyword, mediaType, tag, page], enabled: !!workspaceId,
    queryFn: ({ signal }) => {
      const query = new URLSearchParams({ offset: String((page - 1) * 30), limit: '30' })
      if (keyword) query.set('keyword', keyword)
      if (mediaType) query.set('media_type', mediaType)
      if (tag) query.set('tag', tag)
      return request<AssetPage>(`${base}?${query}`, { signal })
    } })
  const fixed = useQuery({ queryKey: ['asset-set', workspaceId, setId], enabled: !!workspaceId && !!setId,
    queryFn: ({ signal }) => request<AssetSet>(`${base}/sets/${encodeURIComponent(setId || '')}`, { signal }) })
  const upload = useMutation({ mutationFn: (chosen: File) => request<Asset>(
    `${base}/uploads?name=${encodeURIComponent(chosen.name)}`,
    { method: 'POST', body: chosen, headers: { 'Content-Type': 'application/octet-stream' } }),
    onSuccess: () => { setFile(null); setPage(1); void client.invalidateQueries({ queryKey: ['assets', workspaceId] }) } })
  const saveTags = useMutation({ mutationFn: () => request<{ tags: string[] }>(`${base}/${editing?.asset_id}/tags`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tags: editTags }),
  }), onSuccess: () => { setEditing(null); void client.invalidateQueries({ queryKey: ['assets', workspaceId] }) } })
  const makeSet = useMutation({ mutationFn: () => request<AssetSet>(`${base}/sets`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_ids: selected }),
  }), onSuccess: result => {
    const next = new URLSearchParams(params)
    next.set('set', result.asset_set_id)
    setParams(next)
  } })
  const openUrl = (asset: Asset, download = false) =>
    `${base}/${encodeURIComponent(asset.asset_id)}/content?revision=${asset.revision}${download ? '&download=true' : ''}`

  return <div className="asset-library">
    <div className="asset-heading"><div><Text className="eyebrow">LIBRARY / INPUTS</Text><Title level={1}>资料库</Title></div>
      {workspaceId && <div className="asset-upload"><input aria-label="选择上传文件" type="file" onChange={event => setFile(event.target.files?.[0] || null)} />
        <Button type="primary" disabled={!file} loading={upload.isPending} onClick={() => file && upload.mutate(file)}>上传资料</Button></div>}</div>
    {!workspaceId ? <Empty description="先在顶部选择工作空间" /> : <>
      {upload.isError && <Alert type="error" showIcon title={upload.error.message} className="asset-alert" />}
      <div className="asset-filters"><Input.Search aria-label="搜索资料" placeholder="搜索文件名、来源或标签" value={keywordInput}
        onChange={event => setKeywordInput(event.target.value)} onSearch={value => setKeyword(value.trim())} allowClear enterButton="搜索" />
        <Select aria-label="资料类型" value={mediaType} onChange={setMediaType} allowClear placeholder="全部类型"
          options={listing.data?.facets.media_types.map(value => ({ value, label: value }))} />
        <Select aria-label="资料标签" value={tag} onChange={setTag} allowClear placeholder="全部标签"
          options={listing.data?.facets.tags.map(value => ({ value, label: value }))} /></div>
      {listing.isError && <Alert type="error" showIcon title={listing.error.message}
        action={<Button onClick={() => void listing.refetch()}>重试</Button>} className="asset-alert" />}
      <div className="asset-table-head"><Title level={3}>已登记资料 <Text type="secondary">{listing.data?.total ?? 0}</Text></Title>
        <Space><Text type="secondary">已选 {selected.length}</Text><Button disabled={!selected.length} loading={makeSet.isPending}
          onClick={() => makeSet.mutate()}>固定输入集合</Button></Space></div>
      {makeSet.isError && <Alert type="error" showIcon title={makeSet.error.message} className="asset-alert" />}
      <Table<Asset> size="small" rowKey="asset_id" loading={listing.isPending} dataSource={listing.data?.items || []}
        scroll={{ x: 850 }} rowSelection={{ selectedRowKeys: selected, preserveSelectedRowKeys: true,
          onChange: keys => setSelected(keys.map(String)) }}
        pagination={{ current: page, pageSize: 30, total: listing.data?.total || 0,
          showSizeChanger: false, onChange: setPage }}
        columns={[{ title: '名称', dataIndex: 'original_name', width: 210, ellipsis: true,
          render: (_value, item) => <strong title={item.original_name}>{item.original_name}</strong> },
          { title: '类型', dataIndex: 'media_type', width: 170 },
          { title: '大小', dataIndex: 'size_bytes', width: 100, render: value => bytes(value) },
          { title: '标签', dataIndex: 'tags', width: 200, render: (_value, item) => <Space size={4} wrap>{item.tags.map(label => <Tag key={label}>{label}</Tag>)}</Space> },
          { title: '登记时间', dataIndex: 'created_at', width: 175, render: value => new Date(value).toLocaleString('zh-CN') },
          { title: '操作', key: 'actions', width: 215, render: (_value, item) => <Space size="small">
            <a href={openUrl(item)} target="_blank" rel="noreferrer">打开原件</a>
            <Button type="link" size="small" onClick={() => setSource(item)}>来源</Button>
            <Button type="link" size="small" onClick={() => { setEditing(item); setEditTags(item.tags) }}>标签</Button>
          </Space> }]}
        locale={{ emptyText: <Empty description={keyword || mediaType || tag ? '没有符合条件的资料' : '尚无资料'} /> }} />
      {setId && <section className="asset-fixed"><div className="asset-table-head"><Title level={3}>当前输入集合</Title>
        <Button onClick={() => { const next = new URLSearchParams(params); next.delete('set'); setParams(next) }}>清除选择</Button></div>
        {fixed.isPending && <Text>正在读取…</Text>}
        {fixed.isError && <Alert type="error" title="输入集合无法读取" action={<Button onClick={() => void fixed.refetch()}>重试</Button>} />}
        {fixed.data && <><Text type="secondary">版本 {fixed.data.revision} · {fixed.data.members.length} 份资料</Text>
          <ul>{fixed.data.members.map(item => <li key={item.asset_id}>{item.original_name}</li>)}</ul>
          <a className="asset-app-link" href={`/batches?workspace=${encodeURIComponent(workspaceId)}&set=${encodeURIComponent(setId)}`}>选择模板启动处理批次</a>
          {fixed.data.members.filter(item => /\.(csv|xlsx|parquet)$/i.test(item.original_name)).length >= 2 &&
            <a href={`/apps/data?workspace=${encodeURIComponent(workspaceId)}&asset_set=${encodeURIComponent(setId)}`}>在数据工作台使用</a>}
          {fixed.data.members.some(item => /\.(png|jpe?g|bmp|tiff?|webp|heic|heif|pdf)$/i.test(item.original_name)) &&
            <a className="asset-app-link" href={`/apps/ocr/workflow?workspace=${encodeURIComponent(workspaceId)}&set=${encodeURIComponent(setId)}`}>在 OCR 中使用</a>}
          {fixed.data.members.length > 0 && fixed.data.members.every(item => /\.(jpe?g|png|webp|bmp)$/i.test(item.original_name)) &&
            <a className="asset-app-link" href={`/apps/classification/workflow?workspace=${encodeURIComponent(workspaceId)}&set=${encodeURIComponent(setId)}`}>在图像分类中使用</a>}
        </>}
      </section>}
    </>}
    {focused.isError && <Alert type="error" title="来源资料无法读取" />}
    <Modal title="资料来源" open={!!shownSource} footer={null} onCancel={() => {
      setSource(null)
      if (focusedId) { const next = new URLSearchParams(params); next.delete('asset'); setParams(next) }
    }}>
      {shownSource && <dl className="asset-source"><dt>文件</dt><dd>{shownSource.original_name}</dd><dt>来源</dt>
        <dd>{shownSource.source.root_id === 'uploads' ? '平台上传' : shownSource.source.root_id === 'data' ? '数据作业上传' : shownSource.source.root_id}</dd>
        <dt>原始位置</dt><dd>{shownSource.source.relative_path}</dd><dt>版本</dt><dd>{shownSource.revision}</dd>
        <dt>原件</dt><dd><a href={openUrl(shownSource)} target="_blank" rel="noreferrer">打开原件</a></dd></dl>}
    </Modal>
    <Modal title="编辑标签" open={!!editing} onCancel={() => { setEditing(null); saveTags.reset() }}
      okText="保存" okButtonProps={{ loading: saveTags.isPending }} onOk={() => saveTags.mutate()}>
      <Select mode="tags" aria-label="资料标签编辑" style={{ width: '100%' }} value={editTags} onChange={setEditTags}
        options={listing.data?.facets.tags.map(value => ({ value, label: value }))} tokenSeparators={[',', '，']} />
      {saveTags.isError && <Alert type="error" title={saveTags.error.message} className="asset-alert" />}
    </Modal>
  </div>
}
