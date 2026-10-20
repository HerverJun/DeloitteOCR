import { Alert, Card, Progress, Tag, Typography } from 'antd'
import type { ContextBudget, ModelCapabilitySource } from './api'

const componentLabels: Record<string, string> = {
  system: '系统说明', environment: '执行环境', skills: '技能指导', tools: '工具契约', user: '用户消息',
  facts: '任务与来源事实', tool_results: '工具结果', assistant: 'Agent 历史', overhead: '协议开销',
}
const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '未提供'
export const capacitySourceLabel = (source?: ModelCapabilitySource) => ({ explicit_declaration: '人工声明', endpoint_metadata: '服务元数据', identity_matched_catalog: '身份匹配的模型目录', unknown: '尚无容量声明' })[source || 'unknown']
export const compactionNotice = (result: { queued?: boolean; compacted?: boolean }) => result.queued === false
  ? result.compacted ? '上下文已整理，完整历史与来源保留。' : '当前没有需要整理的较早历史。'
  : '已安排在下一次模型请求前整理上下文。'
const method = (value?: string) => {
  if (!value) return '本地估计'
  if (value.includes('byte')) return `UTF-8 字节保守估计（${value}）`
  if (value.includes('token') || value.includes('tiktoken')) return `分词器估计（${value}）`
  return `本地估计（${value}）`
}

export function ContextBudgetContent({ budget, exceeded = false }: { budget: ContextBudget; exceeded?: boolean }) {
  const profile = budget.model_profile
  const margin = budget.safety_margin_tokens ?? profile?.safety_margin_tokens
  const total = typeof budget.input_tokens_estimate === 'number' && typeof budget.output_reserve === 'number'
    ? budget.input_tokens_estimate + budget.output_reserve + (margin || 0) : undefined
  const available = budget.available_input_tokens ?? profile?.available_input_tokens
  const pressure = budget.pressure_ratio ?? (available && typeof budget.input_tokens_estimate === 'number' ? budget.input_tokens_estimate / available : undefined)
  const entries = Object.entries(budget.component_tokens || {}).filter(([, value]) => typeof value === 'number')
  const overflow = typeof budget.overflow_component === 'string' ? budget.overflow_component : undefined
  const largest = overflow ? entries.find(([key]) => key === overflow) : [...entries].sort((a, b) => b[1] - a[1])[0]
  return <div className="agent-context-budget" aria-label="模型请求上下文预算">
    <div className="agent-context-summary"><Typography.Text strong>{exceeded ? '本地上下文检查未通过' : '本次请求上下文'}</Typography.Text>
      <Typography.Text>输入估计 {number(budget.input_tokens_estimate)} tokens · 输出预留 {number(budget.output_reserve)} tokens</Typography.Text>
      {profile ? <><Typography.Text type="secondary">模型容量：{profile.context_window ? `${number(profile.context_window)} tokens` : '未知'} · {capacitySourceLabel(profile.capacity_sources.context_window)}</Typography.Text>
        <Typography.Text type="secondary">本地上限 {number(profile.runtime_context_cap)} · 有效窗口 {number(profile.effective_context_window)} tokens</Typography.Text>
        <Typography.Text type="secondary">合计 {number(total)} tokens（含安全余量 {number(margin)}） · 可用输入 {number(available)} tokens</Typography.Text></>
        : typeof budget.context_cap === 'number' && <Typography.Text type="secondary">合计 {number(total)} / 本地上限 {number(budget.context_cap)} tokens</Typography.Text>}
      {typeof pressure === 'number' && Number.isFinite(pressure) && <div className="agent-context-pressure" aria-label="输入预算占用"><Progress percent={Math.min(100, Math.max(0, Math.round(pressure * 100)))} size="small" status={exceeded || pressure > 1 ? 'exception' : 'normal'} /><Typography.Text type="secondary">{pressure >= .8 ? '已达到自动整理阈值' : '达到 80% 时自动整理'}{budget.compressed ? ' · 本次已整理' : ''}</Typography.Text></div>}
    </div>
    {exceeded && largest && <Alert type="warning" title={`${overflow ? '超限组成' : '占用最多'}：${componentLabels[largest[0]] || largest[0]}，约 ${number(largest[1])} tokens`} description="这是请求组成的本地预算检查，普通短消息也可能因工具或技能内容过多而无法发送。" />}
    <details><summary>查看预算组成与工具加载</summary>
      <Typography.Paragraph type="secondary">{method(budget.estimation_method || (typeof budget.method === 'string' ? budget.method : undefined))}；估计值用于请求前预算，服务返回的实际用量另列。</Typography.Paragraph>
      <dl className="agent-context-components">{entries.map(([key, value]) => <div key={key}><dt>{componentLabels[key] || key}</dt><dd>{number(value)} tokens</dd></div>)}
        <div><dt>输出预留</dt><dd>{number(budget.output_reserve)} tokens</dd></div>
        {typeof margin === 'number' && <div><dt>安全余量</dt><dd>{number(margin)} tokens</dd></div>}
      </dl>
      <Typography.Paragraph type="secondary">最终请求 {number(budget.payload_bytes)} 字节{budget.compressed ? '；已压缩发送副本，原始记录保留。' : '。'}</Typography.Paragraph>
      <Typography.Paragraph>本轮可调用 {budget.selected_tools?.length || 0} 个工具{budget.reclaimed_tools?.length ? `，收回 ${budget.reclaimed_tools.length} 个无关契约` : ''}。</Typography.Paragraph>
      <div className="agent-context-tools">{budget.selected_tools?.map(name => <Tag key={name}>{name}</Tag>)}</div>
    </details>
  </div>
}

const compressionPhases: Record<string, string> = { queued: '已安排上下文整理', start: '正在整理上下文', summary: '正在摘要较早历史', end: '上下文已整理', error: '上下文整理未完成' }
export function ContextCompactionContent({ payload }: { payload: Record<string, unknown> }) {
  const phase = String(payload.phase || ''), before = payload.before_tokens, after = payload.after_tokens
  return <div className="agent-compaction-status" role="status"><Tag color={phase === 'error' ? 'warning' : phase === 'end' ? 'success' : 'processing'}>{compressionPhases[phase] || '上下文整理'}</Tag>
    {typeof payload.message === 'string' && <Typography.Text>{payload.message}</Typography.Text>}
    {typeof before === 'number' && typeof after === 'number' && <Typography.Text type="secondary">输入估计 {number(before)} → {number(after)} tokens</Typography.Text>}
    <Typography.Text type="secondary">完整历史与来源保留，可按需回查。</Typography.Text>
    {phase === 'error' && typeof payload.error === 'string' && <Typography.Text type="warning">{payload.error}</Typography.Text>}
  </div>
}

export function DeliveryCheckContent({ payload }: { payload: Record<string, unknown> }) {
  const phase = String(payload.phase || ''), checks = Array.isArray(payload.checks) ? payload.checks.filter((check): check is Record<string, unknown> => !!check && typeof check === 'object') : []
  const staged = phase === 'staged' || phase === 'passed' && payload.finalized === false
  return <section className="agent-delivery-check" aria-label="交付检查"><Typography.Text strong>{staged ? '本批成果已核验，等待最终交付' : phase === 'passed' ? '交付检查通过' : phase === 'failed' ? '交付检查未通过' : '正在检查交付成果'}</Typography.Text>
    {typeof payload.message === 'string' && <Typography.Paragraph type={phase === 'failed' ? 'warning' : 'secondary'}>{payload.message}</Typography.Paragraph>}
    {!!checks.length && <ul>{checks.map((check, index) => <li key={index}><Tag color={check.status === 'passed' ? 'success' : check.status === 'failed' ? 'warning' : 'default'}>{check.status === 'passed' ? '通过' : check.status === 'failed' ? '未通过' : '待检查'}</Tag>{String(check.name || '检查项')}{typeof check.message === 'string' ? `：${check.message}` : ''}</li>)}</ul>}
  </section>
}

export function ModelRequestContent({ payload }: { payload: Record<string, unknown> }) {
  const budget = (payload.context_budget || payload.context_metrics) as ContextBudget | undefined
  if (!budget) return <Typography.Paragraph type="secondary">正在向 {String(payload.model || '主控模型')} 请求回应…</Typography.Paragraph>
  return <Card size="small" className="agent-step"><Typography.Paragraph type="secondary">主控请求 · 配置模型 {String(payload.configured_model || payload.model || '')}</Typography.Paragraph><ContextBudgetContent budget={budget} />
    {!!payload.injection && <EnvironmentContent payload={payload.injection as Record<string, unknown>} />}</Card>
}

export function ModelUsageContent({ payload }: { payload: Record<string, unknown> }) {
  return <Typography.Paragraph type="secondary" className="agent-usage" aria-label="服务返回实际用量">服务返回实际用量：输入 {number(payload.input_tokens)} tokens · 输出 {number(payload.output_tokens)} tokens
    {typeof payload.returned_model === 'string' && ` · 服务返回模型 ${payload.returned_model}`}</Typography.Paragraph>
}

export function EnvironmentContent({ payload }: { payload: Record<string, unknown> }) {
  // Only display registered environment facts. Never dump process environment or credential-bearing data.
  const environment = payload.environment && typeof payload.environment === 'object' ? payload.environment as Record<string, unknown> : payload
  const controller = environment.controller && typeof environment.controller === 'object' ? environment.controller as Record<string, unknown> : {}
  const fields: [string, unknown][] = [['配置模型', controller.configured_model || environment.configured_model || environment.model],
    ['接口协议', controller.protocol || environment.protocol], ['连接版本', controller.connection_revision || environment.connection_revision],
    ['业务执行环境', environment.execution_environment || environment.operating_system || environment.platform],
    ['环境版本', payload.environment_version || payload.version || environment.version], ['任务事实版本', payload.facts_version]]
  const injectedFields = Array.isArray(payload.environment_fields) ? payload.environment_fields.filter((value): value is string => typeof value === 'string') : []
  const skills = Array.isArray(payload.skills) ? payload.skills.filter((value): value is Record<string, unknown> => !!value && typeof value === 'object') : []
  return <details className="agent-environment"><summary>已注入平台环境与任务事实</summary>
    {fields.filter(([, value]) => typeof value === 'string' || typeof value === 'number').map(([label, value]) => <Typography.Paragraph key={label}>{label}：{String(value)}</Typography.Paragraph>)}
    {!!injectedFields.length && <Typography.Paragraph>已注入字段：{injectedFields.join('、')}</Typography.Paragraph>}
    {!!skills.length && <Typography.Paragraph>技能版本：{skills.map(skill => `${String(skill.name || '')} · ${String(skill.version || '').slice(0, 12)}${skill.source_changed ? '（源文件已有变化，使用固定快照）' : ''}`).join('；')}</Typography.Paragraph>}
    <Typography.Paragraph type="secondary">主控通过平台注册工具执行，OCR 与分类由专业服务处理。模型请求记录保留本轮注入版本。</Typography.Paragraph>
  </details>
}
