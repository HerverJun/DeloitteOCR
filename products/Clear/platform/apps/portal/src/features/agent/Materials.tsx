import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Empty, Input, Modal, Pagination, Tabs, Typography } from 'antd'
import { UploadQueue } from '../batches/UploadQueue'
import { agentRequest, type AuditProfile } from './api'
import './materials.css'

type Asset = { asset_id: string; original_name: string; size_bytes: number; revision: number }
const assetPageSize = 50
export function Materials({ open, workspace, selected, close, save }: { open: boolean; workspace: string | null; selected: string[]; close: () => void; save: (ids: string[]) => Promise<void> }) {
  const [ids, setIds] = useState(selected), [uploading, setUploading] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState('')
  const [tab, setTab] = useState('upload'), [keyword, setKeyword] = useState('')
  const [queryKeyword, setQueryKeyword] = useState(''), [page, setPage] = useState(1)
  const scope = useRef({ workspace, open, generation: 0 }), saveAttempt = useRef(0)
  if (scope.current.workspace !== workspace || scope.current.open !== open) scope.current = { workspace, open, generation: scope.current.generation + 1 }
  const uploadGeneration = scope.current.generation
  const assets = useQuery({ queryKey: ['assets', workspace, 'agent-picker', queryKeyword, page], enabled: open && !!workspace && tab === 'existing',
    queryFn: ({ signal }) => agentRequest<{ items: Asset[]; total: number }>(`/workspaces/${encodeURIComponent(workspace!)}/assets?${new URLSearchParams({ offset: String((page - 1) * assetPageSize), limit: String(assetPageSize), keyword: queryKeyword })}`, { signal }) })
  useEffect(() => { setUploading(false); if (open) { setIds(selected); setError(''); setTab('upload'); setKeyword(''); setQueryKeyword(''); setPage(1) } }, [open, workspace])
  useEffect(() => { saveAttempt.current += 1; setUploading(false); setSaving(false) }, [workspace])
  useEffect(() => {
    if (keyword.trim() === queryKeyword) return
    const timer = setTimeout(() => { setQueryKeyword(keyword.trim()); setPage(1) }, 250)
    return () => clearTimeout(timer)
  }, [keyword, queryKeyword])
  const visibleAssets = assets.data?.items || []
  const submit = async () => {
    if (!workspace || uploading || saving) return
    const generation = scope.current.generation, attempt = ++saveAttempt.current
    setSaving(true); setError('')
    try { await save([...ids]); if (scope.current.generation === generation) close() }
    catch (caught) { if (scope.current.generation === generation) setError((caught as Error).message) }
    finally { if (saveAttempt.current === attempt) setSaving(false) }
  }
  return <Modal title="添加文件" rootClassName="agent-materials-modal" open={open} onCancel={close} width={680} okText="加入对话" cancelText="取消"
    okButtonProps={{ disabled: !workspace || uploading || saving || !ids.length && !selected.length, loading: saving }} onOk={() => { void submit() }}>
    <Typography.Paragraph type="secondary">上传文件或选用项目中的文件，然后在对话里说明你想完成的工作。</Typography.Paragraph>
    <div className="agent-materials-selection" role="status">{ids.length ? `已选择 ${ids.length} 份文件` : '尚未选择文件'}</div>
    {error && <Alert type="error" title={error} />}
    {!workspace ? <Typography.Paragraph>正在准备项目…</Typography.Paragraph> : <Tabs activeKey={tab} onChange={setTab} items={[
      { key: 'upload', label: '上传文件', children: open && <UploadQueue key={workspace} workspaceId={workspace} compact autoUpload disabled={saving}
        onBusy={value => { if (scope.current.generation === uploadGeneration) setUploading(value) }}
        onUploaded={id => { if (scope.current.generation === uploadGeneration) setIds(old => old.includes(id) ? old : [...old, id]) }} /> },
      { key: 'existing', label: '已有文件', children: <div className="agent-materials-existing">
        <Input.Search aria-label="搜索已有文件" placeholder="搜索项目中的文件名" value={keyword} onChange={event => setKeyword(event.target.value)} onSearch={value => { setQueryKeyword(value.trim()); setPage(1) }} allowClear />
        {assets.error ? <Alert type="error" title="暂时无法读取已有文件" description={assets.error.message} action={<Button size="small" onClick={() => { void assets.refetch() }}>重试</Button>} /> : assets.isPending ? <Typography.Text type="secondary">正在读取文件…</Typography.Text> : visibleAssets.length ? <div className="audit-asset-picker">{visibleAssets.map(asset => <Checkbox key={asset.asset_id} checked={ids.includes(asset.asset_id)} disabled={saving} onChange={event => setIds(old => event.target.checked ? [...old, asset.asset_id] : old.filter(id => id !== asset.asset_id))}>
          <span>{asset.original_name}</span><small>{asset.size_bytes >= 1024 * 1024 ? `${(asset.size_bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.ceil(asset.size_bytes / 1024))} KB`}</small></Checkbox>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={keyword.trim() ? '没有匹配的文件' : '项目中还没有文件，可切换到“上传文件”添加'} />}
        {!!assets.data?.total && <Pagination aria-label="已有文件分页" current={page} pageSize={assetPageSize} total={assets.data.total} showSizeChanger={false} disabled={saving || assets.isFetching} showTotal={total => `共 ${total} 份文件，跨页保留选择`} onChange={setPage} />}
      </div> },
    ]} />}
  </Modal>
}

export function ProjectBackground({ open, profile, close, save }: { open: boolean; profile?: AuditProfile; close: () => void; save: (value: AuditProfile) => Promise<void> }) {
  const [value, setValue] = useState<AuditProfile>({ revision: 0, audit_object: '', period_start: '', period_end: '', objective: '', rules: '' }), [busy, setBusy] = useState(false), [error, setError] = useState('')
  useEffect(() => { if (open && profile) setValue(profile) }, [open, profile])
  const change = (key: keyof AuditProfile, text: string) => setValue(old => ({ ...old, [key]: text }))
  return <Modal title="项目背景与审计口径" open={open} onCancel={close} okText="保存背景" cancelText="稍后填写" okButtonProps={{ loading: busy, disabled: !profile }} onOk={() => {
    setBusy(true); setError(''); void save(value).then(close).catch(e => setError(e.message)).finally(() => setBusy(false))
  }}>
    <Typography.Paragraph type="secondary">可以逐步填写。Agent 会在后续对话中读取这里保存的范围和规则。</Typography.Paragraph>
    {error && <Alert type="error" title={error} />}
    <label className="audit-field">审计对象<Input value={value.audit_object} maxLength={200} onChange={e => change('audit_object', e.target.value)} /></label>
    <div className="audit-period">{(['period_start', 'period_end'] as const).map((key, i) => <label key={key} className="audit-field">{i ? '期间结束' : '期间开始'}<Input type="date" value={value[key]} onChange={e => change(key, e.target.value)} /></label>)}</div>
    <label className="audit-field">审计目标<Input.TextArea value={value.objective} maxLength={4000} onChange={e => change('objective', e.target.value)} /></label>
    <label className="audit-field">业务口径与规则<Input.TextArea value={value.rules} rows={5} maxLength={8000} placeholder="填写核对口径、阈值，或说明需要参考的制度资料。" onChange={e => change('rules', e.target.value)} /></label>
  </Modal>
}
