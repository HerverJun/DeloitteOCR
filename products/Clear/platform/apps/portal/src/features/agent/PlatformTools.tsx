import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Drawer, Empty, Input, Radio, Space, Spin, Tag, Typography } from 'antd'
import { agentRequest, type DeliveryPolicy, type DeliveryPolicyResolution } from './api'
import './platform-tools.css'

export type PlatformCapability = {
  id: string; name: string; aliases?: string[]; binding: 'bound' | 'pending'; state: 'ready' | 'preparable' | 'unavailable'
  tool_names: string[]; dependencies?: string[]; blockers?: string[]
}
export type PlatformTool = {
  name: string; description?: string; capability?: string; aliases?: string[]; effect?: string
  availability?: string | { state?: string; reason?: string; missing?: string[] }
}
export type PlatformCapabilities = { count?: number; tools: PlatformTool[]; capabilities?: PlatformCapability[] }
type FaceReference = { reference_id: string; label: string; source?: { original_name?: string }; model_hash?: string; version?: number | string; revision?: number | string }

const readinessLabels: Record<string, string> = { ready: '可调用', preparable: '可准备', unavailable: '当前不可用' }
const effectLabels: Record<string, string> = { read: '读取', write: '创建或修改', control: '控制作业', prepare: '准备资源', destructive: '删除或修改' }
const availability = (tool: PlatformTool) => typeof tool.availability === 'string' ? { state: tool.availability } : tool.availability || {}
export function filterCapabilities(capabilities: PlatformCapability[], tools: PlatformTool[], keyword: string) {
  const terms = keyword.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  return capabilities.filter(capability => {
    const members = tools.filter(tool => capability.tool_names.includes(tool.name))
    const haystack = [capability.name, capability.id, ...(capability.aliases || []), ...members.flatMap(tool => [tool.name, tool.description || '', ...(tool.aliases || [])])].join(' ').toLocaleLowerCase()
    return terms.every(term => haystack.includes(term))
  })
}

export function PlatformTools({ open, close, workspace, referenceIds, saveReferences, pendingScope = false, busy = false, scopeKey, deliveryPolicy = 'auto', policyResolution, saveDeliveryPolicy }: {
  open: boolean; close: () => void; workspace: string | null; referenceIds: string[]; saveReferences: (ids: string[]) => Promise<void>; pendingScope?: boolean; busy?: boolean; scopeKey?: string
  deliveryPolicy?: DeliveryPolicy; policyResolution?: DeliveryPolicyResolution; saveDeliveryPolicy?: (policy: DeliveryPolicy) => Promise<void>
}) {
  const [keyword, setKeyword] = useState(''), [selected, setSelected] = useState(referenceIds), [saving, setSaving] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [edited, setEdited] = useState(false)
  const [policy, setPolicy] = useState(deliveryPolicy), [policyEdited, setPolicyEdited] = useState(false), [policySaving, setPolicySaving] = useState(false), [policyError, setPolicyError] = useState(''), [policyNotice, setPolicyNotice] = useState('')
  const listing = useQuery({ queryKey: ['agent-platform-capabilities', workspace], enabled: open && !!workspace,
    queryFn: () => agentRequest<PlatformCapabilities>(`/workspaces/${encodeURIComponent(workspace!)}/agent/capabilities`), refetchInterval: open ? 10000 : false })
  const references = useQuery({ queryKey: ['face-references', workspace], enabled: open && !!workspace,
    queryFn: () => agentRequest<{ items: FaceReference[] }>(`/workspaces/${encodeURIComponent(workspace!)}/multimodal/face-references`) })
  useEffect(() => { if (open) { setSelected(referenceIds); setEdited(false); setError(''); setNotice(''); setPolicy(deliveryPolicy); setPolicyEdited(false); setPolicyError(''); setPolicyNotice('') } }, [open, workspace, scopeKey])
  useEffect(() => { if (open && !policyEdited) setPolicy(deliveryPolicy) }, [deliveryPolicy, open, policyEdited])
  const referenceStamp = referenceIds.join('\n')
  useEffect(() => { if (open && !edited) setSelected(referenceIds) }, [referenceStamp, open, edited])
  const editSelection = (ids: string[]) => { setSelected(ids); setEdited(true); setNotice('') }
  const tools = listing.data?.tools || []
  const capabilities = useMemo(() => filterCapabilities(listing.data?.capabilities || [], tools, keyword), [listing.data, keyword])
  const missing = selected.filter(id => references.data && !references.data.items.some(reference => reference.reference_id === id))
  const changed = [...selected].sort().join('\n') !== [...referenceIds].sort().join('\n')
  return <Drawer title="平台工具" open={open} onClose={close} size={680}>
    <div className="agent-platform-tools">
      <Typography.Paragraph>Agent 根据业务目标发现并组合这里已绑定的工具。目录展示实际接入与准备状态，查看目录不会启动分析或下载模型。</Typography.Paragraph>
      {!workspace && <Alert type="info" title="选择项目后查看平台工具与参考授权。" />}
      <section className="agent-platform-reference-scope" aria-label="任务交付策略"><Typography.Title level={5}>本任务的交付要求</Typography.Title>
        <Radio.Group aria-label="选择交付策略" value={policy} disabled={busy || policySaving} onChange={event => { setPolicy(event.target.value); setPolicyEdited(true); setPolicyNotice('') }}>
          <Space orientation="vertical"><Radio value="auto">按用户目标判断</Radio><Radio value="draft">仅交付草稿</Radio><Radio value="reviewed">正式交付前须人工核准</Radio></Space>
        </Radio.Group>
        <Typography.Paragraph type="secondary">按用户目标生成并交付文件，包括正式报告。只有明确要求或选择人工核准时才等待；未复核状态如实保留。</Typography.Paragraph>
        {deliveryPolicy === 'auto' && policyResolution && <Typography.Paragraph>{policyResolution.require_review ? '当前用户要求：正式交付前须核准。' : '当前无需人工核准，可直接生成并交付文件。'}{policyResolution.evidence && <small>依据：{policyResolution.evidence}</small>}</Typography.Paragraph>}
        {policyError && <Alert type="error" title={policyError} />}{policyNotice && <Alert type="success" title={policyNotice} />}
        <Button disabled={!workspace || !saveDeliveryPolicy || policy === deliveryPolicy || busy || policySaving} loading={policySaving} onClick={() => {
          setPolicySaving(true); setPolicyError(''); setPolicyNotice(''); void saveDeliveryPolicy!(policy).then(() => { setPolicyEdited(false); setPolicyNotice('交付要求已保存。') }).catch(caught => setPolicyError(caught.message)).finally(() => setPolicySaving(false))
        }}>保存交付要求</Button>
      </section>
      <div className="agent-platform-tools-toolbar"><Input.Search aria-label="搜索平台工具" value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="按能力、中文别名或操作名称查找" allowClear />
        <Button disabled={!workspace} onClick={() => void listing.refetch()}>刷新状态</Button></div>
      {listing.isFetching && <Spin size="small" aria-label="正在读取工具目录" />}
      {listing.error && <Alert type="error" title="工具目录暂不可读取" description={listing.error.message} />}
      {listing.data && <Typography.Paragraph type="secondary">已登记 {listing.data.count ?? tools.length} 项操作 · {listing.data.capabilities?.filter(capability => capability.binding === 'bound').length || 0} 项能力已绑定</Typography.Paragraph>}
      {listing.data && !listing.data.capabilities && <Alert type="warning" title="当前服务尚未提供能力接入清单" description="工具状态不能据此判定为已全部接入，请升级服务后重新读取。" />}
      {capabilities.map(capability => <section className="agent-platform-capability" key={capability.id} aria-label={`平台能力 ${capability.name}`}>
        <div className="agent-platform-capability-heading"><Typography.Text strong>{capability.name}</Typography.Text><Space size={4} wrap>
          <Tag color={capability.binding === 'bound' ? 'success' : 'warning'}>{capability.binding === 'bound' ? '已绑定' : '待接入'}</Tag>
          <Tag color={capability.state === 'ready' && capability.binding === 'bound' ? 'success' : capability.state === 'unavailable' ? 'warning' : 'default'}>{readinessLabels[capability.state] || '状态未确认'}</Tag></Space></div>
        {!!capability.aliases?.length && <p className="agent-platform-secondary">{capability.aliases.join(' · ')}</p>}
        {!!capability.dependencies?.length && <p className="agent-platform-secondary">依赖：{capability.dependencies.join('、')}</p>}
        {!!capability.blockers?.length && <ul className="agent-platform-blockers">{capability.blockers.map(reason => <li key={reason}>{reason}</li>)}</ul>}
        {capability.binding === 'pending' && <p className="agent-platform-blockers">尚无可执行绑定，Agent 暂不能调用此能力。</p>}
        {!!capability.tool_names.length && <details><summary>查看 {capability.tool_names.length} 项操作</summary><ul className="agent-platform-operation-list">{capability.tool_names.map(name => {
          const tool = tools.find(item => item.name === name), state = tool ? availability(tool) : {}
          return <li key={name}><div><code>{name}</code>{state.state && <Tag>{readinessLabels[state.state] || state.state}</Tag>}{tool?.effect && <small>{effectLabels[tool.effect] || tool.effect}</small>}</div>
            {tool?.description && <p>{tool.description}</p>}{state.reason && <p className="agent-platform-blockers">{state.reason}</p>}</li>
        })}</ul></details>}
      </section>)}
      {listing.data?.capabilities && !capabilities.length && <Empty description="没有符合条件的能力" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
      <section className="agent-platform-reference-scope" aria-label="人脸参考授权范围"><Typography.Title level={5}>本任务的人脸参考范围</Typography.Title>
        <Typography.Paragraph type="secondary">只有这里明确选择的参考可供 Agent 检索。未选择时不授权参考库检索；照片检测与两图比对仍可使用任务资料。</Typography.Paragraph>
        {pendingScope && <Alert type="info" title="授权范围将在下一处理边界应用。" />}
        {(error || references.error) && <Alert type="error" title={error || references.error?.message} />}
        {notice && <Alert type="success" title={notice} />}
        {!!missing.length && <Alert type="warning" title="部分已选参考已不可读取" description="保留原授权记录；如需移除，可取消下方选择后保存。" />}
        <div className="agent-platform-reference-list">{references.data?.items.map(reference => <Checkbox key={reference.reference_id} checked={selected.includes(reference.reference_id)} disabled={busy || saving}
          onChange={event => editSelection(event.target.checked ? [...selected, reference.reference_id] : selected.filter(id => id !== reference.reference_id))}>
          <span>{reference.label}</span><small>{reference.source?.original_name || '来源未提供'}{(reference.revision ?? reference.version) !== undefined ? ` · v${reference.revision ?? reference.version}` : ''}</small></Checkbox>)}
          {missing.map(id => <Checkbox key={id} checked disabled={busy || saving} onChange={() => editSelection(selected.filter(value => value !== id))}><span>不可读取参考 {id}</span></Checkbox>)}</div>
        {references.data && !references.data.items.length && !missing.length && <Typography.Paragraph type="secondary">当前项目尚未登记人脸参考。</Typography.Paragraph>}
        <Space wrap><Button type="primary" disabled={!workspace || !changed || busy || saving || references.isPending || references.isError} loading={saving} onClick={() => {
          setSaving(true); setError(''); setNotice(''); void saveReferences(selected).then(() => { setEdited(false); setNotice('参考范围已保存；后续作业仅使用明确授权的参考。') }).catch(caught => setError(caught.message)).finally(() => setSaving(false))
        }}>保存参考范围</Button><Button disabled={!selected.length || busy || saving} onClick={() => editSelection([])}>清空选择</Button><Typography.Text type="secondary">已选择 {selected.length} 项</Typography.Text></Space>
      </section>
    </div>
  </Drawer>
}
