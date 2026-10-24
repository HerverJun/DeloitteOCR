import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Drawer, Input, Select, Space, Typography } from 'antd'
import { agentRequest, agentWrite, requestId, type PreferenceBusiness, type PreferenceValues, type PreferenceView, type PreferenceSnapshot } from './api'

const businesses: { value: PreferenceBusiness; label: string }[] = [
  { value: 'project', label: '项目通用' }, { value: 'screen_excel', label: '批量图片转 Excel' },
  { value: 'invoice_reconciliation', label: '票据与台账核对' }, { value: 'payment_reconciliation', label: '台账核对' },
  { value: 'classification', label: '文件分类' }, { value: 'evidence_workpaper', label: '证据与底稿' },
]
const labels: Record<string, string> = { output_layout: '文件组织', value_mode: '原值格式', include_pending_review: '包含待复核文件', require_human_review: '人工复核', strategy_id: '已验证策略', worksheet: '工作表', key_columns: '业务键列', amount_column: '金额列', amount_unit: '金额单位', decimal_places: '小数位数' }
function display(key: string, value: unknown) {
  if (key === 'output_layout') return '每图一个 Excel'
  if (key === 'value_mode') return value === 'text' ? '保留原文文本' : '仅人工确认并明确选列后使用数值'
  if (key === 'strategy_id') return '平台已验证的日常屏拍策略'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return Array.isArray(value) ? value.join('、') : String(value)
}

export function AgentPreferences({ open, close, workspace, sessionId }: { open: boolean; close: () => void; workspace: string | null; sessionId: string | null }) {
  const [business, setBusiness] = useState<PreferenceBusiness>('screen_excel')
  const [draft, setDraft] = useState<PreferenceValues>({}), [notice, setNotice] = useState('')
  const attempt = useRef<{ scope: string; body: string; id: string } | null>(null)
  const currentScope = useRef(''); currentScope.current = `${workspace}:${business}`
  const client = useQueryClient()
  const profile = useQuery({ queryKey: ['agent-preferences', workspace, business], enabled: open && !!workspace,
    queryFn: () => agentRequest<PreferenceView>(`/workspaces/${encodeURIComponent(workspace!)}/agent/preferences?business=${business}`) })
  const snapshots = useQuery({ queryKey: ['agent-preference-snapshots', workspace, sessionId], enabled: open && !!sessionId,
    queryFn: () => agentRequest<{ snapshots: PreferenceSnapshot[] }>(`/agent/sessions/${encodeURIComponent(sessionId!)}/preferences/snapshots`) })
  useEffect(() => { setDraft({}); setNotice(''); attempt.current = null }, [workspace, business])
  const save = useMutation({ mutationFn: async ({ values, revoke }: { values: PreferenceValues; revoke: string[] }) => {
    if (!workspace || !profile.data) throw new Error('请先读取当前偏好版本')
    const command = { business, expected_revision: profile.data.profile.revision, values, revoke_keys: revoke,
      source: { mode: 'user_form', quote: revoke.length ? `在工作偏好中明确撤回：${revoke.map(key => labels[key] || key).join('、')}` : `在工作偏好中明确保存默认：${Object.entries(values).map(([key, value]) => `${labels[key] || key}=${display(key, value)}`).join('；')}` } }
    const scope = `${workspace}:${business}`, body = JSON.stringify(command)
    if (attempt.current && attempt.current.scope === scope && attempt.current.body !== body) throw new Error('原偏好保存回包待核实，请先重试原内容或刷新查看当前版本')
    if (!attempt.current || attempt.current.scope !== scope) attempt.current = { scope, body, id: requestId() }
    const response = await agentWrite(`/workspaces/${encodeURIComponent(workspace)}/agent/preferences`, { ...command, request_id: attempt.current.id })
    return { response, scope, workspace }
  }, onSuccess: value => { void client.invalidateQueries({ queryKey: ['agent-preferences', value.workspace] }); if (value.scope === currentScope.current) { attempt.current = null; setDraft({}); setNotice('已保存。默认仅影响后续任务，当前固定快照保持原版本。') } } })
  const activeSnapshots = (snapshots.data?.snapshots || []).filter(item => item.business === business).slice(0, 1)
  const set = (key: keyof PreferenceValues, value: string | boolean | string[] | number | undefined) => setDraft(old => {
    const next = { ...old }; if (value === undefined) delete next[key]; else Object.assign(next, { [key]: value }); return next
  })
  return <Drawer title="工作偏好" open={open} onClose={close} width={560}>
    <Typography.Paragraph>项目通用默认与业务默认共同作用；业务默认优先，单次要求优先于默认。保存或撤回只影响后续任务，当前任务沿用固定快照。</Typography.Paragraph>
    {!workspace ? <Alert type="info" title="先选择项目，再保存工作偏好" /> : <>
      <Select aria-label="偏好作用域" value={business} onChange={value => { save.reset(); setBusiness(value) }} options={businesses} style={{ width: '100%' }} />
      {profile.isPending && <p>正在读取偏好…</p>}
      {profile.data && <>
        <Typography.Title level={4}>当前默认 · 版本 {profile.data.profile.revision}</Typography.Title>
        {!Object.keys(profile.data.effective_sources).length && <p>尚未保存默认，使用平台既有策略。</p>}
        {Object.entries(profile.data.effective_sources).map(([key, entry]) => <div key={key} className="agent-preference-entry">
          <Space wrap><Typography.Text strong>{labels[key] || key}：{display(key, entry.value)}</Typography.Text>
            {entry.business === business && <Button size="small" disabled={save.isPending} onClick={() => save.mutate({ values: {}, revoke: [key] })}>撤回{labels[key] || key}</Button>}</Space>
          <Typography.Paragraph type="secondary">{entry.business === 'project' ? '项目通用' : '业务默认'} · 版本 {entry.revision} · 来源：{entry.source.quote}</Typography.Paragraph>
        </div>)}
        <Typography.Title level={4}>修改后续任务默认</Typography.Title>
        <Space orientation="vertical" style={{ width: '100%' }}>
          {(business === 'screen_excel' || business === 'project') && <>
            <Select aria-label="默认文件组织" placeholder="文件组织：不修改" value={draft.output_layout} allowClear onChange={value => set('output_layout', value)} options={[{ value: 'per_image', label: '每图一个 Excel' }]} style={{ width: '100%' }} />
            <Select aria-label="默认原值格式" placeholder="原值格式：不修改" value={draft.value_mode} allowClear onChange={value => set('value_mode', value)} options={[{ value: 'text', label: '保留原文文本' }, { value: 'confirmed_numeric', label: '仅确认并明确选列后使用数值' }]} style={{ width: '100%' }} />
            <Select aria-label="默认待复核导出" placeholder="待复核文件：不修改" value={draft.include_pending_review === undefined ? undefined : String(draft.include_pending_review)} allowClear onChange={value => set('include_pending_review', value === undefined ? undefined : value === 'true')} options={[{ value: 'true', label: '下载中包含待复核文件' }, { value: 'false', label: '下载中排除待复核文件' }]} style={{ width: '100%' }} />
          </>}
          {(business === 'payment_reconciliation' || business === 'invoice_reconciliation') && <>
            <Input aria-label="默认工作表" placeholder="工作表：不修改" value={draft.worksheet || ''} onChange={event => set('worksheet', event.target.value || undefined)} maxLength={120} />
            <Input aria-label="默认金额列" placeholder="金额列：不修改" value={draft.amount_column || ''} onChange={event => set('amount_column', event.target.value || undefined)} maxLength={120} />
            <Input aria-label="默认金额单位" placeholder="金额单位：不修改" value={draft.amount_unit || ''} onChange={event => set('amount_unit', event.target.value || undefined)} maxLength={80} />
            <Input aria-label="默认业务键列" placeholder="业务键列：逗号分隔；留空不修改" value={draft.key_columns?.join('，') || ''} onChange={event => set('key_columns', event.target.value ? event.target.value.split(/[,，]/).map(value => value.trim()).filter(Boolean) : undefined)} maxLength={1000} />
            <Select aria-label="默认小数位数" placeholder="小数位数：不修改" value={draft.decimal_places} allowClear onChange={value => set('decimal_places', value)} options={Array.from({ length: 13 }, (_, value) => ({ value, label: `${value} 位小数` }))} style={{ width: '100%' }} />
          </>}
          <Button type="primary" disabled={!Object.keys(draft).length} loading={save.isPending} onClick={() => save.mutate({ values: draft, revoke: [] })}>保存后续任务默认</Button>
        </Space>
        <Typography.Paragraph type="secondary">数值格式偏好不指定列、不代签人工确认。每次导出仍需明确已核对的图片版本、表和列。</Typography.Paragraph>
      </>}
      <Typography.Title level={4}>本会话最近固定快照</Typography.Title>
      {activeSnapshots.length ? activeSnapshots.map(item => <div key={item.sha256}>
        <p>任务 {item.run_id} · 项目默认 {item.project_revision} / 业务默认 {item.profile_revision}</p>
        {Object.entries(item.values).map(([key, value]) => <p key={key}>{labels[key] || key}：{display(key, value)}{key in item.overrides ? '（单次要求）' : ''}</p>)}
        <details><summary>固定身份与来源</summary><p>快照 {item.sha256}</p>{Object.entries(item.sources).map(([key, entry]) => <p key={key}>{labels[key] || key}：{entry.source.quote} · 版本 {entry.revision}</p>)}</details>
      </div>) : <p>{sessionId ? '此业务尚无固定任务快照。' : '选择已有任务后可查看其固定快照。'}</p>}
      {notice && <Alert type="success" title={notice} />}
      {[profile.error, snapshots.error, save.error].filter(Boolean).map((error, index) => <Alert key={index} type="error" title={(error as Error).message} />)}
      <Button onClick={() => { attempt.current = null; save.reset(); void profile.refetch(); void snapshots.refetch() }}>刷新当前版本</Button>
    </>}
  </Drawer>
}
