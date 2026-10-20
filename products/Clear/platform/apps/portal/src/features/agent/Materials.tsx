import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Empty, Input, Modal, Tabs, Typography } from 'antd'
import { UploadQueue } from '../batches/UploadQueue'
import { agentRequest, type AuditProfile } from './api'

type Asset = { asset_id: string; original_name: string; size_bytes: number; revision: number }
export function Materials({ open, workspace, selected, close, save }: { open: boolean; workspace: string | null; selected: string[]; close: () => void; save: (ids: string[]) => Promise<void> }) {
  const [ids, setIds] = useState(selected), [busy, setBusy] = useState(false), [error, setError] = useState('')
  const assets = useQuery({ queryKey: ['assets', workspace, 'agent-picker'], enabled: open && !!workspace,
    queryFn: async () => { const all: Asset[] = []; let offset = 0; let total: number
      do { const page = await agentRequest<{ items: Asset[]; total: number }>(`/workspaces/${workspace}/assets?offset=${offset}&limit=100`); all.push(...page.items); offset += page.items.length; total = page.total; if (!page.items.length) break } while (offset < total)
      return all
    } })
  useEffect(() => { if (open) { setIds(selected); setError('') } }, [open])
  return <Modal title="添加文件与资料" open={open} onCancel={close} width={760} okText="加入任务" cancelText="返回对话" okButtonProps={{ disabled: !workspace || busy, loading: busy }}
    onOk={() => { setBusy(true); void save(ids).then(close).catch(e => setError(e.message)).finally(() => setBusy(false)) }}>
    <Typography.Paragraph type="secondary">选择当前任务使用的文件与资料。后续可以继续补充，已有结果保留原始来源。</Typography.Paragraph>
    {(error || assets.error) && <Alert type="error" title={error || assets.error?.message} />}
    {!workspace ? <Typography.Paragraph>正在准备项目…</Typography.Paragraph> : <Tabs items={[
      { key: 'existing', label: `项目资料 · 已选 ${ids.length}`, children: assets.data?.length ? <div className="audit-asset-picker">{assets.data.map(asset => <Checkbox key={asset.asset_id} checked={ids.includes(asset.asset_id)} onChange={e => setIds(old => e.target.checked ? [...old, asset.asset_id] : old.filter(id => id !== asset.asset_id))}>
        <span>{asset.original_name}</span><small>v{asset.revision} · {(asset.size_bytes / 1024).toFixed(1)} KB</small></Checkbox>)}</div> : <Empty description="上传资料后即可在当前任务使用" /> },
      { key: 'upload', label: '上传文件', children: <UploadQueue key={workspace} workspaceId={workspace} compact onBusy={setBusy} onUploaded={id => setIds(old => old.includes(id) ? old : [...old, id])} /> },
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
