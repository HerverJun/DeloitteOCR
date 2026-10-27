import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { useWorkspaceId, Workspaces } from '../../App'
import { assets, newRequestId, stateNames } from '../batches/api'
import { UploadQueue } from '../batches/UploadQueue'
import { documentsApi, documentsBase, documentsUrl, parseableDocument, type DocumentCell, type DocumentPage, type DocumentRun } from './api'
import { request } from '../batches/api'
import { navigationHref } from '../../clear-ui/navigation'
import './documents.css'

const { Title, Text, Paragraph } = Typography
const activeStates = ['queued', 'running', 'cancelling', 'waiting_resource']
const blockNames: Record<string, string> = { title: '标题', doc_title: '文档标题', paragraph_title: '段落标题', heading: '标题', text: '正文', paragraph: '正文', table: '表格', image: '图片', formula: '公式', header: '页眉', footer: '页脚', footnote: '脚注' }
type IdentifiedPage = DocumentPage & { engine?: string; engine_identity?: unknown; model_revisions?: unknown }

function TableReview({ workspace, documentId, tableId, close }: { workspace: string; documentId: string; tableId: string; close: () => void }) {
  const [cells, setCells] = useState<DocumentCell[]>([]), [changed, setChanged] = useState(false)
  const table = useQuery({ queryKey: ['document-table', workspace, documentId, tableId], queryFn: () => documentsApi.table(workspace, documentId, tableId) })
  useEffect(() => { if (table.data) { setCells(table.data.cells); setChanged(false) } }, [table.data])
  const save = useMutation({ mutationFn: () => documentsApi.review(workspace, documentId, table.data!, cells), onSuccess: () => { setChanged(false); void table.refetch() } })
  return <Modal title="表格校对" open width={920} onCancel={close} footer={<Space wrap>
    <Button onClick={close}>关闭</Button><Button type="primary" loading={save.isPending} disabled={!changed || !table.data} onClick={() => save.mutate()}>保存人工修订</Button>
    <Button disabled={changed || save.isPending || !table.data} href={`${documentsUrl(workspace)}/${encodeURIComponent(documentId)}/tables/${encodeURIComponent(tableId)}/export.xlsx`}>导出已保存 XLSX</Button>
  </Space>}>
    <Paragraph type="secondary">人工修订单独保存，原始识别保留。单元格没有可靠坐标时保留页级来源。</Paragraph>
    {(table.error || save.error) && <Alert type="error" title={(table.error || save.error)?.message} />}
    <Table<DocumentCell> rowKey={row => `${row.row}:${row.column}`} loading={table.isPending} size="small" dataSource={cells} pagination={{ pageSize: 20 }} columns={[
      { title: '行 / 列', width: 95, render: (_, cell) => `${cell.row + 1} / ${cell.column + 1}` },
      { title: '合并范围', width: 100, render: (_, cell) => `${cell.row_span || 1} 行 × ${cell.column_span || 1} 列` },
      { title: '识别文字与人工修订', render: (_, cell) => <Input.TextArea aria-label={`第 ${cell.row + 1} 行第 ${cell.column + 1} 列`} autoSize value={cell.text} disabled={save.isPending}
        onChange={event => { setCells(previous => previous.map(value => value.row === cell.row && value.column === cell.column ? { ...value, text: event.target.value } : value)); setChanged(true) }} /> },
    ]} />
  </Modal>
}

export function DocumentPanel({ workspace }: { workspace: string }) {
  const [params, setParams] = useSearchParams(), [assetId, setAssetId] = useState(params.get('asset') || '')
  const selected = params.get('document') || '', [page, setPage] = useState(1), [blockPage, setBlockPage] = useState(1)
  const [tableId, setTableId] = useState(''), [query, setQuery] = useState(''), [search, setSearch] = useState(''), [copyMessage, setCopyMessage] = useState('')
  const [runPage, setRunPage] = useState(1)
  const [uploadBusy, setUploadBusy] = useState(false)
  const media = useQuery({ queryKey: ['document-assets', workspace], queryFn: ({ signal }) => assets(workspace, signal) })
  const listing = useQuery({ queryKey: ['documents', workspace, runPage], queryFn: () => documentsApi.list(workspace, (runPage - 1) * 5), refetchInterval: value => value.state.data?.items.some(item => activeStates.includes(item.status)) ? 1500 : false })
  const detail = useQuery({ queryKey: ['document-detail', workspace, selected], enabled: !!selected, queryFn: () => documentsApi.get(workspace, selected), refetchInterval: value => value.state.data && activeStates.includes(value.state.data.status) ? 1200 : false })
  const pages: IdentifiedPage[] = detail.data?.document?.pages || [], pageData = pages.find(value => value.page_number === page)
  const pageName = (number: number) => detail.data?.document?.pagination_kind === 'logical' ? `第 ${number} 逻辑页` : detail.data?.document?.pagination_kind === 'slide' ? `第 ${number} 张幻灯片` : `第 ${number} 页`
  const blocks = useQuery({ queryKey: ['document-blocks', workspace, selected, detail.data?.result_hash, page, blockPage], enabled: !!selected && !!pageData,
    queryFn: () => documentsApi.page(workspace, selected, page, (blockPage - 1) * 30) })
  const matches = useQuery({ queryKey: ['document-search', workspace, selected, search], enabled: !!selected && !!search,
    queryFn: () => request<{ items?: { page_number: number; block_id: string; text?: string; snippet?: string }[]; matches?: { page_number: number; block_id: string; text?: string; snippet?: string }[]; total: number }>(`${documentsBase(workspace)}/${encodeURIComponent(selected)}/search?q=${encodeURIComponent(search)}`) })
  const choose = (value: DocumentRun) => { setParams(previous => { const next = new URLSearchParams(previous); next.set('document', value.document_id); return next }); setPage(1); setBlockPage(1); setSearch(''); setCopyMessage('') }
  const pendingKey = `clear-document-parse:${workspace}`
  const pending = useRef<{ request_id: string; asset_id: string; asset_revision: number; blob_hash: string } | null>(null)
  useEffect(() => { try { pending.current = JSON.parse(localStorage.getItem(pendingKey) || 'null') } catch { pending.current = null } }, [pendingKey])
  useEffect(() => { if (params.get('asset')) setAssetId(params.get('asset')!) }, [params.get('asset')])
  const selectedAsset = media.data?.find(value => value.asset_id === assetId && parseableDocument(value.original_name))
  const parse = useMutation({ mutationFn: async () => {
    if (!pending.current && !selectedAsset) throw new Error(media.isPending ? '正在读取资料，请稍候。' : '请先选择需要解析的资料。')
    pending.current ||= { request_id: newRequestId(), asset_id: selectedAsset!.asset_id, asset_revision: selectedAsset!.revision, blob_hash: selectedAsset!.blob_hash }
    localStorage.setItem(pendingKey, JSON.stringify(pending.current))
    return documentsApi.parse(workspace, { asset_id: pending.current.asset_id, revision: pending.current.asset_revision, blob_hash: pending.current.blob_hash, original_name: '', media_type: '' }, pending.current.request_id)
  }, onSuccess: value => { pending.current = null; localStorage.removeItem(pendingKey); choose(value); void listing.refetch() } })
  const control = useMutation({ mutationFn: (action: 'cancel' | 'resume') => documentsApi.control(workspace, selected, action), onSuccess: () => { void detail.refetch(); void listing.refetch() } })
  const downloadBase = `${documentsUrl(workspace)}/${encodeURIComponent(selected)}`
  return <section className="panel document-workbench" aria-label="解析文档">
    <div className="section-head"><div><Title level={3}>解析文档</Title><Text type="secondary">阅读文档结构、按页回查、校对表格与导出</Text></div><Link to={navigationHref('/assets', params.toString(), '', '/apps/ocr/documents')}>资料库</Link></div>
    <Paragraph>支持 PDF、图片、DOCX、PPTX、OFD。原生内容直接读取，扫描内容由 Clear 调度识别；表格计算继续使用 ClearData。</Paragraph>
    <Paragraph type="secondary">选择文件后自动上传并选中，再点击「解析文档」。已有资料可以直接从下方选择。</Paragraph>
    <UploadQueue workspaceId={workspace} compact autoUpload multiple={false} accept=".pdf,.jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp,.docx,.pptx,.ofd" buttonLabel="选择文档"
      disabled={parse.isPending || !!pending.current} onFilesSelected={() => setAssetId('')} onBusy={setUploadBusy} onUploaded={id => { setAssetId(id); void media.refetch() }} />
    <Space wrap className="document-controls"><Select aria-label="待解析文档" showSearch optionFilterProp="label" placeholder="或选择项目中已有的文档" value={assetId || undefined} onChange={setAssetId} loading={media.isPending} disabled={uploadBusy || parse.isPending || !!pending.current}
      options={media.data?.filter(asset => parseableDocument(asset.original_name)).map(asset => ({ value: asset.asset_id, label: `${asset.original_name} · v${asset.revision}` }))} />
      <Button type="primary" loading={parse.isPending || uploadBusy || (!pending.current && media.isPending)} disabled={uploadBusy || !pending.current && !selectedAsset} onClick={() => parse.mutate()}>{uploadBusy ? '正在准备文档…' : pending.current ? '核实上次解析请求' : '解析文档'}</Button>
      <Button onClick={() => { void media.refetch(); void listing.refetch(); if (selected) void detail.refetch() }}>刷新</Button>
    </Space>
    {!!pending.current && <Text type="secondary" role="status">上次解析提交尚未确认，请先核实原请求，避免重复解析或误用新文档。</Text>}
    {!pending.current && <Text type="secondary" role="status">{uploadBusy ? '文件正在上传，完成后会自动选中。' : assetId && !selectedAsset && media.isFetching ? '正在读取所选文档，请稍候。' : selectedAsset ? `已选择「${selectedAsset.original_name}」，可以开始解析。` : '选择文档后即可开始解析。'}</Text>}
    {(media.error || listing.error || detail.error || parse.error || control.error) && <Alert type="error" title={(media.error || listing.error || detail.error || parse.error || control.error)?.message} />}
    {selected && detail.data && <div className="document-result"><Space wrap><Title level={4}>解析结果</Title><Tag>{stateNames[detail.data.status] || detail.data.status}</Tag>
      <a href={`${downloadBase}/original`} target="_blank" rel="noreferrer">查看原件</a>
      {activeStates.includes(detail.data.status) && <Button loading={control.isPending} onClick={() => control.mutate('cancel')}>取消解析</Button>}
      {['cancelled', 'failed', 'interrupted', 'paused', 'needs_verification', 'partial'].includes(detail.data.status) && <Button loading={control.isPending} onClick={() => control.mutate('resume')}>重新启动解析</Button>}
    </Space>
      {detail.data.error && <Alert type="error" title="解析未完成" description={detail.data.error} />}
      {detail.data.document && <>
        {!!detail.data.document.warnings?.length && <Alert type="warning" title="部分内容需要核查" description={<ul>{detail.data.document.warnings.map((value, i) => <li key={i}>{typeof value === 'string' ? value : JSON.stringify(value)}</li>)}</ul>} />}
        <Space wrap><Select aria-label="解析结果页码" value={page} onChange={value => { setPage(value); setBlockPage(1) }} options={pages.map(value => ({ value: value.page_number, label: `${pageName(value.page_number)}${value.needs_ocr ? ' · 扫描内容待识别' : ''}` }))} />
          <Button onClick={() => void navigator.clipboard.writeText((blocks.data?.blocks || pageData?.blocks || []).map(block => block.text).join('\n\n')).then(() => setCopyMessage('当前显示文字已复制')).catch(() => setCopyMessage('无法写入剪贴板，请选中下方文字复制。'))}>复制当前文字</Button>
          <a href={`${downloadBase}/files/document.md`} download>导出 Markdown</a><a href={`${downloadBase}/files/document.json`} download>导出结构 JSON</a><a href={`${downloadBase}/files/raw-bundle.zip`} download>下载原始输出包</a>
          <Link to={`/agent?workspace=${encodeURIComponent(workspace)}&document=${encodeURIComponent(selected)}&assets=${encodeURIComponent(detail.data.source?.asset_id || detail.data.asset_id || assetId)}`}>交给 Agent 读取原始解析</Link>
          <Text type="secondary">表格人工修订请在校对中查看或导出。</Text>
        </Space>{copyMessage && <Text role="status">{copyMessage}</Text>}
        {pageData?.needs_ocr && <Alert type="warning" title="此页仍有扫描内容待识别" description="请查看任务状态或在文档识别中完成增强识别；当前显示已保存内容。" />}
        {blocks.error && <Alert type="error" title={blocks.error.message} />}
        <Table rowKey="block_id" size="small" loading={blocks.isPending} dataSource={blocks.data?.blocks || pageData?.blocks || []} pagination={{ current: blockPage, pageSize: 30, total: blocks.data?.total || pageData?.blocks.length || 0, onChange: setBlockPage, showSizeChanger: false }} columns={[
          { title: '阅读顺序 / 类型', width: 130, render: (_, block) => `${block.order + 1} · ${blockNames[block.type] || block.type}` },
          { title: '已保存内容', render: (_, block) => <div className="document-block-text">{block.text || (block.type === 'image' ? '图片内容请回查原件' : '无文字内容')}{block.table_id && <div><Button onClick={() => setTableId(block.table_id!)}>进入表格校对</Button></div>}</div> },
          { title: '来源', width: 105, render: (_, block) => <span>{pageName(page)}<br />{block.bbox ? '有区域位置' : '页级来源'}</span> },
        ]} />
        <Input.Search aria-label="搜索已解析文档" placeholder="搜索已保存的文字" value={query} onChange={event => setQuery(event.target.value)} onSearch={value => setSearch(value.trim())} enterButton="搜索" />
        {matches.error && <Alert type="error" title={matches.error.message} />}
        {search && matches.data && <div><Paragraph>找到 {matches.data.total} 处</Paragraph>{(matches.data.items || matches.data.matches || []).map((match, i) => <Button key={`${match.block_id}:${i}`} type="link" onClick={() => { setPage(match.page_number); setBlockPage(1) }}>{pageName(match.page_number)} · {match.snippet || match.text}</Button>)}</div>}
        <details><summary>运行、模型与原件身份</summary><pre>{JSON.stringify({ document_id: selected, source: detail.data.document.source, parser: detail.data.document.engine,
          current_page: pageData && { page_number: pageData.page_number, engine: pageData.engine, engine_identity: pageData.engine_identity, model_revisions: pageData.model_revisions } }, null, 2)}</pre><a href={`${downloadBase}/files/raw-middle.json`} download>下载原始解析结果</a></details>
      </>}
      {!detail.data.document && !detail.data.error && <Empty description={activeStates.includes(detail.data.status) ? '正在处理，结果保存后会自动显示' : '尚无可读取的结果'} />}
    </div>}
    {!!listing.data?.items.length && <details className="document-history" open={!selected}><summary>历史解析 · {listing.data.total} 份</summary><Table<DocumentRun> rowKey="document_id" size="small" dataSource={listing.data.items} pagination={{ current: runPage, pageSize: 5, total: listing.data.total, onChange: setRunPage, showSizeChanger: false }} columns={[
      { title: '文档', render: (_, value) => value.source?.original_name || value.original_name || value.document_id },
      { title: '状态', render: (_, value) => <Tag>{stateNames[value.status] || value.status}</Tag> },
      { title: '操作', render: (_, value) => <Button type="link" onClick={() => choose(value)}>查看解析结果</Button> },
    ]} /></details>}
    {tableId && <TableReview key={`${selected}:${tableId}`} workspace={workspace} documentId={selected} tableId={tableId} close={() => setTableId('')} />}
  </section>
}

export function DocumentWorkbench() {
  const workspace = useWorkspaceId()
  return <><div className="page-heading"><Text className="eyebrow">CLEAR / DOCUMENTS</Text><Title level={1}>解析文档</Title><Paragraph>保留原件、阅读顺序和原始结果，连接 Agent、表格校对与成果导出。</Paragraph></div>
    {workspace ? <DocumentPanel key={workspace} workspace={workspace} /> : <Workspaces continueTo="/apps/ocr/documents" />}</>
}
