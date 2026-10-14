import { useEffect, useState } from 'react'
import { Alert, Button, Image, Space, Tag, Typography } from 'antd'
import type { Asset, EvidenceField, EvidenceValue, ItemEvidence, SourceLocator } from '../../../../../packages/contracts/types.generated'
import { API, request } from '../batches/api'

const { Title, Text, Paragraph } = Typography
const literalStyle = { whiteSpace: 'pre-wrap' as const, overflowWrap: 'anywhere' as const }
const roleNames: Record<EvidenceValue['role'], string> = { left: '左侧值', right: '右侧值', original: '原始值', adopted: '已采用值', manual: '人工修订值' }
const kindNames: Record<EvidenceValue['kind'], string> = { null: '空值', text: '文本', decimal: '十进制', integer: '整数', boolean: '布尔值', date: '日期', error: '错误', unknown_formula: '公式结果未知', unknown: '值未确定' }
const ruleNames: Record<EvidenceField['rule_kind'], string> = { exact: '严格相等比对', text: '文本比对', amount: '金额容差比对', native_revision: '原生修订', unmatched: '单侧未匹配', ambiguous: '匹配关系待判定', technical: '执行或技术问题', unknown: '规则未确定' }
const outcomeNames: Record<EvidenceField['outcome'], string> = { equal: '一致', different: '存在差异', within_tolerance: '在容差内', outside_tolerance: '超出容差', unmatched: '未匹配', ambiguous: '需判定匹配关系', adopted: '已采用修订', unadopted: '尚未采用修订', unknown: '尚未判定' }
const roleOrder: EvidenceValue['role'][] = ['left', 'right', 'original', 'manual', 'adopted']

function literal(value: EvidenceValue['raw']) {
  return value === null ? '空值（null）' : typeof value === 'boolean' ? String(value) : value === '' ? '空文本（长度 0）' : value
}

function location(source: SourceLocator) {
  const parts: string[] = []
  if (source.sheet_name) parts.push(`工作表「${source.sheet_name}」`)
  if (source.table_number != null) parts.push(`表 ${source.table_number}`)
  if (source.row_number != null) parts.push(`行 ${source.row_number}`)
  if (source.column_number != null) parts.push(`列 ${source.column_number}`)
  if (source.page_number != null) parts.push(`页 ${source.page_number}`)
  if (source.box) parts.push(`区域 [${source.box.join(', ')}]${source.coordinate_space === 'image_pixels' ? ' 像素' : source.coordinate_space === 'normalized' ? ' 归一化坐标' : ''}`)
  if (source.image_version_id) parts.push(`图像版本 ${source.image_version_id}`)
  return parts.join(' · ') || '整份资料'
}

function CellValue({ value }: { value: EvidenceValue }) {
  const unknownFormula = value.kind === 'unknown_formula'
  return <div style={{ minWidth: 0, padding: '12px 0' }}>
    <Space wrap><Text strong>{roleNames[value.role]}</Text><Tag>{kindNames[value.kind]}</Tag></Space>
    <Paragraph style={{ ...literalStyle, margin: '8px 0' }}>{unknownFormula ? '公式结果未知' : value.kind === 'unknown' && value.value === null ? '尚未确定' : literal(value.value)}</Paragraph>
    {(unknownFormula || value.raw !== value.value) && <Paragraph type="secondary" style={literalStyle}>
      {unknownFormula ? '原公式：' : '原始字面值：'}{literal(value.raw)}
    </Paragraph>}
    {value.source && <Paragraph type="secondary" style={{ ...literalStyle, marginBottom: 4 }}>来源定位：{location(value.source)}</Paragraph>}
    {value.table_revision && <Paragraph type="secondary" style={{ ...literalStyle, marginBottom: 0 }}>表版本：{value.table_revision}</Paragraph>}
  </div>
}

function ReadableRules({ evidence }: { evidence: ItemEvidence }) {
  let config: Record<string, unknown> | null = null
  let prefix = ''
  try {
    const start = evidence.rule_summary.indexOf('{')
    const value = JSON.parse(start > 0 ? evidence.rule_summary.slice(start) : evidence.rule_summary)
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      config = value
      if (start > 0) prefix = evidence.rule_summary.slice(0, start).trim()
    }
  } catch { /* A readable server summary is already complete text. */ }
  if (!config) return <Paragraph style={literalStyle}>{evidence.rule_summary || '此证据没有附加规则说明。'}</Paragraph>
  const policy = config.comparison && typeof config.comparison === 'object' && !Array.isArray(config.comparison)
    ? config.comparison as Record<string, unknown> : config
  const pairs = (value: unknown) => Array.isArray(value) ? value.filter(pair => Array.isArray(pair) && pair.length === 2 && pair.every(item => typeof item === 'string')).map(pair => pair.join(' ↔ ')).join('；') : ''
  const entries: string[] = []
  if (prefix) entries.push(prefix.replace(/[:：]$/, ''))
  const keys = pairs(policy.key_pairs), compared = pairs(policy.compare_pairs), amounts = pairs(policy.amount_pairs)
  if (keys) entries.push(`固定匹配键：${keys}`)
  if (compared) entries.push(`固定左右列映射：${compared}`)
  if (amounts) entries.push(`金额列映射：${amounts}`)
  if (typeof policy.amount_tolerance === 'string') entries.push(`固定金额容差：${policy.amount_tolerance}`)
  if (policy.type_policy === 'strict' || policy.type_policy === 'text') entries.push(`类型政策：${policy.type_policy === 'strict' ? '严格区分类型' : '按文本比对'}`)
  if (policy.null_policy === 'equal' || policy.null_policy === 'never_match') entries.push(`空值政策：${policy.null_policy === 'equal' ? '空值视为相同' : '空值不匹配'}`)
  if (typeof policy.trim_text === 'boolean') entries.push(policy.trim_text ? '文本去除首尾空白' : '文本保留首尾空白')
  if (typeof policy.casefold_text === 'boolean') entries.push(policy.casefold_text ? '文本忽略大小写' : '文本区分大小写')
  if (typeof config.model_id === 'string') entries.push(`固定分类模型：${config.model_id}`)
  if (config.device === 'cpu' || config.device === 'cuda') entries.push(`固定设备：${config.device === 'cpu' ? 'CPU' : 'GPU'}`)
  if (Array.isArray(config.engines) && config.engines.every(value => typeof value === 'string')) entries.push(`固定识别引擎：${config.engines.join('、')}`)
  if (typeof config.link_id === 'string') entries.push(`原生项目关联：${config.link_id}`)
  return entries.length ? <div>{entries.map((text, index) => <Paragraph key={index} style={{ ...literalStyle, marginBottom: 6 }}>{text}</Paragraph>)}</div>
    : <Paragraph type="secondary">此证据使用固定模板规则，完整规则已保留在技术详情。</Paragraph>
}

type SourceRead = { key: string; state: 'checking' | 'ready' | 'mismatch' | 'unavailable'; asset?: Asset }

function VerifiedSource({ source, workspaceId, ordinal }: { source: SourceLocator; workspaceId: string; ordinal: number }) {
  const key = JSON.stringify([workspaceId, source.asset_id, source.asset_revision, source.blob_hash])
  const [read, setRead] = useState<SourceRead>({ key, state: 'checking' })
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setRead({ key, state: 'checking' })
    void request<Asset>(`/workspaces/${encodeURIComponent(workspaceId)}/assets/${encodeURIComponent(source.asset_id)}`, { signal: controller.signal })
      .then(asset => {
        if (controller.signal.aborted) return
        const same = asset.asset_id === source.asset_id && asset.workspace_id === workspaceId
          && asset.revision === source.asset_revision && asset.blob_hash === source.blob_hash
          && typeof asset.media_type === 'string' && /^[A-Za-z0-9.+-]+\/[A-Za-z0-9.+-]+$/.test(asset.media_type)
        setRead({ key, state: same ? 'ready' : 'mismatch', asset: same ? asset : undefined })
      }).catch(() => { if (!controller.signal.aborted) setRead({ key, state: 'unavailable' }) })
    return () => controller.abort()
  }, [key, workspaceId, source.asset_id, source.asset_revision, source.blob_hash, attempt])
  const current: SourceRead = read.key === key ? read : { key, state: 'checking' }
  const asset = current.asset
  const url = `${API}/workspaces/${encodeURIComponent(workspaceId)}/assets/${encodeURIComponent(source.asset_id)}/content?revision=${source.asset_revision}`
  const image = asset?.media_type.startsWith('image/')
  const precision = { exact: '精确定位', approximate: '近似定位', page_only: '仅页级定位', unknown: '定位精度未知' }[source.precision]
  return <section aria-label={`来源 ${ordinal}`} style={{ padding: '12px 0', minWidth: 0 }}>
    <Paragraph style={{ ...literalStyle, marginBottom: 6 }}><Text strong>来源 {ordinal}：</Text>{asset?.original_name || source.asset_id} · 资料版本 {source.asset_revision}</Paragraph>
    <Paragraph type="secondary" style={literalStyle}>{location(source)} · {precision}</Paragraph>
    {current.state === 'checking' && <Paragraph type="secondary">正在核对原件版本、内容身份和类型…</Paragraph>}
    {current.state === 'mismatch' && <Alert type="warning" title="资料身份、版本或内容已变化，不能将当前原件显示为此证据来源。" />}
    {current.state === 'unavailable' && <Alert type="warning" title="原件暂不可读取，固定证据值仍已保留。" action={<Button size="small" onClick={() => setAttempt(value => value + 1)}>重新核对原件</Button>} />}
    {asset && <>
      <Paragraph><a href={url} target="_blank" rel="noreferrer">{image ? '打开原图文件' : asset.media_type === 'application/pdf' ? '查看原 PDF 文档' : '查看原件文件'}</a></Paragraph>
      {image && <Image src={url} alt={`证据原图：${asset.original_name}`} width="100%" style={{ maxHeight: 320, objectFit: 'contain' }}
        preview={{ mask: '点击查看大图' }} />}
    </>}
  </section>
}

export function ItemEvidenceView({ evidence }: { evidence: ItemEvidence }) {
  const sources = evidence.sources.slice(0, 10)
  return <section aria-label="事项证据" style={{ minWidth: 0 }}>
    <Title level={3}>复核证据</Title>
    <Paragraph style={literalStyle}>固定模板 {evidence.template_id} · 模板版本 {evidence.template_version} · 批次复核版本 {evidence.batch_revision}</Paragraph>
    {(evidence.native_version_id !== null || evidence.native_revision !== null || evidence.adoption_operation_id !== null) && <Paragraph style={literalStyle}>
      {evidence.native_version_id !== null && <>原件版本：{evidence.native_version_id}。 </>}
      {evidence.native_revision !== null && <>{evidence.adoption_operation_id !== null ? '已采用原生修订' : '原生修订'}：{evidence.native_revision}。 </>}
      {evidence.adoption_operation_id !== null && <>采用操作：{evidence.adoption_operation_id}。</>}
    </Paragraph>}
    {evidence.scenario !== 'data_compare' && !evidence.native_context_available && <Paragraph type="secondary">当前原生上下文不可读取，以下固定原始值和已采用值仍可核对。</Paragraph>}
    <Title level={4}>固定规则</Title>
    <ReadableRules evidence={evidence} />
    {evidence.fields.length ? evidence.fields.map((field, index) => <section key={`${index}:${field.name}`} aria-label={`字段 ${field.name}`} style={{ borderTop: '1px solid #e5e5e5', padding: '16px 0' }}>
      <Space wrap><Text strong>{field.name}</Text><Tag>{ruleNames[field.rule_kind]}</Tag><Tag>{outcomeNames[field.outcome]}</Tag></Space>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 220px), 1fr))', gap: 20 }}>
        {[...field.values].sort((left, right) => roleOrder.indexOf(left.role) - roleOrder.indexOf(right.role)).map((value, valueIndex) => <CellValue key={`${value.role}:${valueIndex}`} value={value} />)}
      </div>
      {(field.delta != null || field.tolerance != null || field.within_tolerance != null) && <Space wrap>
        {field.delta != null && <Text>服务端差额：{field.delta}</Text>}
        {field.tolerance != null && <Text>服务端容差：{field.tolerance}</Text>}
        {field.within_tolerance != null && <Text>服务端容差判断：{field.within_tolerance ? '在容差内' : '超出容差'}</Text>}
      </Space>}
    </section>) : <Paragraph type="secondary">此事项没有已确定的结果值，请根据固定规则和原件核实技术问题。</Paragraph>}
    <Title level={4}>原件与定位</Title>
    <Paragraph type="secondary">来源共 {evidence.source_count} 处，本页显示 {sources.length} 处。</Paragraph>
    {sources.map((source, index) => <VerifiedSource key={`${evidence.workspace_id}:${source.asset_id}:${source.asset_revision}:${source.blob_hash}:${index}`} source={source} workspaceId={evidence.workspace_id} ordinal={index + 1} />)}
    <details style={{ marginTop: 16 }}><summary>技术详情 · 固定证据身份与完整数据</summary>
      <pre style={{ ...literalStyle, maxHeight: 400, overflow: 'auto' }}>{JSON.stringify(evidence, null, 2)}</pre>
    </details>
  </section>
}
