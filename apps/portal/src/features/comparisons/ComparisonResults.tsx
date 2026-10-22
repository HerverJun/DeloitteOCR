import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Form, Input, Modal, Progress, Select, Space, Table, Tag, Typography } from 'antd'
import { stateNames } from '../batches/api'
import { LayoutOptions, ProviderSelect } from './Configuration'
import { activeComparison, comparisonFile, comparisonLabels, comparisonStages, comparisonsApi, defaultLayout, fileLabels,
  type ComparisonBlock, type ComparisonPage, type ComparisonProfile, type ComparisonRun, type ComparisonSide, type LayoutParseConfig, type ProviderConnection } from './api'

export const readableValue = (value: unknown): string => value == null ? '未提取到' : typeof value === 'string' ? value : JSON.stringify(value)
const statusColor = (status: string) => status === 'consistent' || status === 'succeeded' ? 'success' : status === 'different' || status === 'failed' ? 'error' : 'warning'
const pageLabel = (side: ComparisonSide, number: number) => `${side === 'left' ? 'A' : 'B'} 侧 · 第 ${number} 页`

function RecompareDialog({ workspace, run, connections, close, changed }: { workspace: string; run: ComparisonRun; connections: ProviderConnection[]; close: () => void; changed: () => void }) {
  const profiles = useQuery({ queryKey: ['comparison-profiles', workspace], queryFn: () => comparisonsApi.profiles(workspace) })
  const [profile, setProfile] = useState<ComparisonProfile | undefined>(run.profile), [provider, setProvider] = useState<ProviderConnection | undefined>(run.comparison_provider)
  const choices = [...(profiles.data?.items || [])]
  if (profile && !choices.some(item => item.id === profile.id && item.version === profile.version)) choices.push(profile)
  const rerun = useMutation({ mutationFn: () => {
    if (!profile || !provider) throw new Error('请选择比较配置和模型。')
    return comparisonsApi.recompare(workspace, run.comparison_id, run.revision, profile, provider)
  }, onSuccess: () => { changed(); close() } })
  return <Modal title="复用解析结果重新比较" open onCancel={close} footer={<Space><Button onClick={close}>取消</Button>
    <Button type="primary" disabled={!profile || !provider} loading={rerun.isPending} onClick={() => rerun.mutate()}>创建新修订并比较</Button></Space>}>
    <Typography.Paragraph>复用两侧已有解析页，按选定字段和模型生成新报告。原报告保持固定版本；当前未完成的解析页仍会在新报告中标明。</Typography.Paragraph>
    <Typography.Paragraph type="secondary">如需调整字段，请先在比较主题区域编辑并保存配置版本。重新比较会向所选供应商发送解析文字和表格。</Typography.Paragraph>
    {(profiles.error || rerun.error) && <Alert type="error" title={(profiles.error || rerun.error)?.message} />}
    <Form layout="vertical"><Form.Item label="已保存的字段配置"><Select aria-label="重比字段配置" loading={profiles.isPending} value={profile ? `${profile.id}@${profile.version}` : undefined}
      options={choices.map(item => ({ value: `${item.id}@${item.version}`, label: `${item.name} · v${item.version}` }))}
      onChange={key => setProfile(choices.find(item => `${item.id}@${item.version}` === key))} /></Form.Item>
      <ProviderSelect label="重新比较使用的模型" value={provider?.id || null} version={provider?.version} connections={connections} onChange={setProvider} />
    </Form>
    {profile && <Typography.Paragraph>{profile.instructions}</Typography.Paragraph>}
  </Modal>
}

function PageReview({ workspace, run, side, page, connections, close, changed, historical }: { workspace: string; run: ComparisonRun; side: ComparisonSide; page: number;
  connections: ProviderConnection[]; close: () => void; changed: () => void; historical: boolean }) {
  const detail = useQuery({ queryKey: ['comparison-page', workspace, run.comparison_id, run.revision, side, page], queryFn: () => comparisonsApi.page(workspace, run.comparison_id, side, page, run.revision) })
  const [blocks, setBlocks] = useState<ComparisonBlock[]>([]), [dirty, setDirty] = useState(false), [invalidTables, setInvalidTables] = useState<number[]>([])
  const [layout, setLayout] = useState<LayoutParseConfig>(run[side]?.layout || { ...defaultLayout }), [showRerun, setShowRerun] = useState(false)
  useEffect(() => { if (detail.data) {
    const tables = (Array.isArray(detail.data.tables) ? detail.data.tables : []) as { table_id: string; rows: number; columns: number; cells: unknown[] }[]
    setBlocks((detail.data.blocks || []).map(block => { const table = tables.find(item => item.table_id === block.table_id); return table ? { ...block, table: { rows: table.rows, columns: table.columns, cells: table.cells } } : block }))
    setDirty(false); setInvalidTables([])
  } }, [detail.data])
  const review = useMutation({ mutationFn: () => comparisonsApi.review(workspace, run.comparison_id, side, page, run.revision, blocks), onSuccess: () => { changed(); close() } })
  const rerun = useMutation({ mutationFn: () => comparisonsApi.rerun(workspace, run.comparison_id, side, [page], run.revision, layout), onSuccess: () => { changed(); close() } })
  const readonly = historical || activeComparison(run.status)
  return <Modal title={`${pageLabel(side, page)} · 回看与修订`} open width={1080} onCancel={close} footer={<Space wrap>
    <Button onClick={close}>关闭</Button><Button disabled={readonly} onClick={() => setShowRerun(value => !value)}>更换方式重跑此页</Button>
    <Button type="primary" disabled={!dirty || !!invalidTables.length || readonly} loading={review.isPending} onClick={() => review.mutate()}>保存修订并更新报告</Button></Space>}>
    {(detail.error || review.error || rerun.error) && <Alert type="error" title={(detail.error || review.error || rerun.error)?.message} />}
    {detail.data && <><Space wrap><Tag>{stateNames[detail.data.status || ''] || detail.data.status || '已保存'}</Tag><Tag>{detail.data.engine || '引擎信息见原始结果'}</Tag>
      {detail.data.original_url && <a href={detail.data.original_url} target="_blank" rel="noreferrer">打开原件</a>}</Space>
      {detail.data.error && <Alert type="warning" title={detail.data.error} />}
      <Typography.Paragraph type="secondary">修订生成新版本并保留原始识别；重跑会重新调用所选引擎。模型坐标仅供定位参考，缺失坐标时使用页级来源。</Typography.Paragraph>
      {detail.data.image_url && <img className="comparison-page-image" src={detail.data.image_url} alt={`${pageLabel(side, page)}原页`} />}
      <Table<ComparisonBlock> rowKey="block_id" size="small" dataSource={blocks} pagination={{ pageSize: 10 }} loading={detail.isPending} columns={[
        { title: '顺序 / 类型', width: 120, render: (_, block, index) => `${(block.order ?? index) + 1} · ${block.type}` },
        { title: '已保存内容', render: (_, block, index) => <><Input.TextArea aria-label={`块 ${block.block_id} 文字`} autoSize={{ minRows: 2, maxRows: 12 }} value={block.text} disabled={readonly} readOnly={block.table != null} onChange={event => {
          setBlocks(previous => previous.map((item, i) => i === index ? { ...item, text: event.target.value } : item)); setDirty(true)
        }} />{block.table != null && <details><summary>编辑表格单元格与结构 JSON</summary><Typography.Paragraph type="secondary">表格文字由单元格生成，请在下方修改对应单元格的 text。</Typography.Paragraph><Input.TextArea aria-label={`块 ${block.block_id} 表格`} defaultValue={JSON.stringify(block.table, null, 2)} autoSize={{ minRows: 4, maxRows: 12 }} disabled={readonly} onChange={event => {
          try { const table = JSON.parse(event.target.value); if (!table || typeof table !== 'object') throw new Error('invalid table')
            setBlocks(previous => previous.map((item, i) => i === index ? { ...item, table } : item)); setInvalidTables(previous => previous.filter(i => i !== index)); setDirty(true)
          } catch { setInvalidTables(previous => [...new Set([...previous, index])]) }
        }} />{invalidTables.includes(index) && <Typography.Text type="danger">表格 JSON 无效，尚不能保存。</Typography.Text>}</details>}</> },
        { title: '来源位置', width: 120, render: (_, block) => block.bbox ? <span>{block.coordinate_kind === 'model_estimated' || (block.source as { coordinate_origin?: string } | undefined)?.coordinate_origin === 'model_estimate' ? '模型估计坐标' : '区域坐标'}<br />{block.bbox.join(', ')}</span> : '页级引用' },
      ]} />
      <details><summary>查看原始响应与诊断</summary><pre className="comparison-raw">{JSON.stringify(detail.data.raw_response ?? detail.data, null, 2)}</pre></details>
    </>}
    {showRerun && <section className="comparison-rerun"><Typography.Title level={4}>此页重跑配置</Typography.Title><LayoutOptions label="重跑" value={layout} onChange={setLayout} connections={connections} />
      <Button loading={rerun.isPending} disabled={layout.mode === 'multimodal' && !layout.provider_id} onClick={() => rerun.mutate()}>按以上配置重跑第 {page} 页</Button></section>}
  </Modal>
}

export function ComparisonResults({ workspace, run, connections, refresh, latestRevision = run.revision, selectRevision }: { workspace: string; run: ComparisonRun; connections: ProviderConnection[]; refresh: () => void; latestRevision?: number; selectRevision?: (revision: number) => void }) {
  const [focus, setFocus] = useState<{ side: ComparisonSide; page: number } | null>(null), [filter, setFilter] = useState('all')
  const [recompare, setRecompare] = useState(false)
  const control = useMutation({ mutationFn: (action: 'cancel' | 'resume') => comparisonsApi.control(workspace, run.comparison_id, action), onSuccess: refresh })
  const progress = run.progress, active = activeComparison(run.status), files = run.files || []
  const historical = !!run.outdated || run.revision < latestRevision
  const rows = run.result?.items || [], visible = filter === 'all' ? rows : rows.filter(row => row.status === filter)
  const openEvidence = (side: string | undefined, page: number | undefined) => { if (page) setFocus({ side: side === 'right' || side === 'b' || side === 'B' ? 'right' : 'left', page }) }
  return <section className="panel comparison-results" aria-label="对比结果">
    <div className="section-head"><div><Typography.Title level={3}>{run.title || '文档主题对比'}</Typography.Title><Typography.Text type="secondary">修订 v{run.revision} · {comparisonStages[run.stage] || run.stage}</Typography.Text></div>
      <Space wrap>{selectRevision && latestRevision > 1 && <Select aria-label="报告历史修订" value={run.revision} onChange={selectRevision} options={Array.from({ length: latestRevision }, (_, index) => ({ value: index + 1, label: `修订 v${index + 1}${index + 1 === latestRevision ? ' · 最新' : ''}` }))} />}
        <Tag>{stateNames[run.status] || run.status}</Tag>{!active && !historical && <Button disabled={!run.left?.document || !run.right?.document} title={!run.left?.document || !run.right?.document ? '两侧解析结果保存后可重新比较' : undefined} onClick={() => setRecompare(true)}>按新配置重新比较</Button>}
        {active && !historical && <Button loading={control.isPending} onClick={() => control.mutate('cancel')}>取消任务</Button>}
        {!historical && ['failed', 'partial', 'cancelled', 'interrupted', 'paused', 'needs_verification'].includes(run.status) && <Button loading={control.isPending} onClick={() => control.mutate('resume')}>继续未完成部分</Button>}</Space></div>
    {(run.error || control.error) && <Alert type="error" title={run.error || control.error?.message} />}
    {(run.outdated || historical) && <Alert type="warning" title="材料来源已有更新" description="当前报告保留原版本，完成修订后的比较可查看新报告。" />}
    {progress && <div className="comparison-progress" role="status" aria-live="polite"><Progress percent={progress.total ? Math.round(progress.processed / progress.total * 100) : 0} status={run.status === 'failed' ? 'exception' : active ? 'active' : 'normal'} />
      <Typography.Text>共 {progress.total} 页 · 已处理 {progress.processed} · 成功 {progress.succeeded} · 失败 {progress.failed}
        {progress.current_page ? ` · 当前 ${pageLabel(progress.current_side === 'right' ? 'right' : 'left', progress.current_page)}` : ''}</Typography.Text></div>}
    {!!files.length && <nav className="comparison-downloads" aria-label="导出固定成果">{files.map(name => <a key={name} href={comparisonFile(workspace, run.comparison_id, name, run.revision)} download>{fileLabels[name] || name}</a>)}</nav>}
    <div className="comparison-page-lists">{(['left', 'right'] as const).map(side => {
      const pages: ComparisonPage[] = run[side]?.document?.pages || run[side]?.pages || []
      return <div key={side}><Typography.Title level={4}>{side === 'left' ? 'A 侧 · PDF' : 'B 侧 · 幻灯片'}解析页</Typography.Title>
        {pages.length ? <div className="comparison-page-buttons">{pages.map(page => <Button key={page.page_number} danger={page.status === 'failed' || !!page.error} onClick={() => setFocus({ side, page: page.page_number })}>
          第 {page.page_number} 页{page.error || page.status === 'failed' ? ' · 待核查' : ''}</Button>)}</div> : <Typography.Text type="secondary">页面准备完成后可逐页回看。</Typography.Text>}</div>
    })}</div>
    {run.result ? <><div className="section-head"><Typography.Title level={4}>逐项对照</Typography.Title><Select aria-label="按比较结果筛选" value={filter} onChange={setFilter}
      options={[{ value: 'all', label: `全部（${rows.length}）` }, ...Object.entries(comparisonLabels).map(([value, label]) => ({ value, label: `${label}（${rows.filter(row => row.status === value).length}）` }))]} /></div>
      <Table rowKey={(row, index) => row.id || String(index)} size="small" dataSource={visible} scroll={{ x: 860 }} pagination={{ pageSize: 15 }} columns={[
        { title: '项目 / 字段', width: 150, render: (_, row) => <><strong>{row.entity || '未确认对象'}</strong><div>{run.profile?.fields.find(field => field.key === row.field)?.label || row.field}</div></> },
        { title: 'A 侧', width: 190, render: (_, row) => <div className="comparison-value">{readableValue(row.a_value)}</div> },
        { title: 'B 侧', width: 190, render: (_, row) => <div className="comparison-value">{readableValue(row.b_value)}</div> },
        { title: '结论与证据', render: (_, row) => <><Tag color={statusColor(row.status)}>{comparisonLabels[row.status] || row.status}</Tag><p className="comparison-value">{row.explanation}</p>
          {(row.evidence || []).map((evidence, i) => <div key={i} className="comparison-evidence"><Button type="link" disabled={!evidence.page_number} onClick={() => openEvidence(evidence.side, evidence.page_number)}>
            {evidence.side === 'right' || evidence.side === 'b' || evidence.side === 'B' ? 'B' : 'A'} 侧 · 第 {evidence.page_number || '?'} 页</Button><q>{evidence.quote || evidence.text || '查看原页证据'}</q></div>)}</> },
      ]} /></> : !active && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未生成比较结果；可先回看已保存页面。" />}
    {focus && <PageReview key={`${run.comparison_id}:${run.revision}:${focus.side}:${focus.page}`} workspace={workspace} run={run} side={focus.side} page={focus.page} connections={connections} close={() => setFocus(null)} changed={refresh} historical={historical} />}
    {recompare && <RecompareDialog workspace={workspace} run={run} connections={connections} close={() => setRecompare(false)} changed={refresh} />}
  </section>
}
