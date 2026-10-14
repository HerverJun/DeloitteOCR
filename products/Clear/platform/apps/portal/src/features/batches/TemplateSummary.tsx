import { Tag, Typography } from 'antd'
import type { Template } from './api'
const { Text, Paragraph } = Typography
type Role = { formats?: string[]; columns?: { header: string; kinds?: string[] }[]; steps?: { operator: string; parameters: Record<string, unknown> }[]; import_options?: Record<string, unknown> }
type Policy = { key_pairs?: string[][]; compare_pairs?: string[][]; amount_pairs?: string[][]; amount_tolerance?: string; type_policy?: string; null_policy?: string; trim_text?: boolean; casefold_text?: boolean }
const names: Record<string, string> = { trim: '去除首尾空白', cast: '类型转换', map: '值映射', rename: '列改名', filter: '筛选', duplicates: '识别重复', integer: '整数', decimal: '十进制', text: '文本', date: '日期', boolean: '真假值', null: '空值', unknown_formula: '公式结果未知' }
const pairs = (value?: string[][]) => value?.length ? value.map(pair => `${pair[0]} ↔ ${pair[1]}`).join('；') : '未配置'
function stepText(step: NonNullable<Role['steps']>[number]) {
  const p = step.parameters
  const columns = p.columns as string[] | undefined
  const column = typeof p.column === 'string' ? p.column : columns?.join('、') || ''
  const detail = step.operator === 'cast' ? names[String(p.target)] || String(p.target) : step.operator === 'rename' ? `改为 ${String(p.header)}` : step.operator === 'map'
    ? Object.entries((p.values || {}) as Record<string, unknown>).map(([from, to]) => `${from} → ${String(to)}`).join('；') : step.operator === 'filter' ? `${String(p.predicate)} ${p.value === undefined ? '' : String(p.value)}` : ''
  return `${names[step.operator] || step.operator} · ${column}${detail ? ' · ' + detail : ''}`
}
export function TemplateSummary({ template }: { template: Template }) {
  const roles = template.config.roles as Record<string, Role> | undefined
  const policy = template.config.comparison as Policy | undefined
  return <section className="batch-template-summary" aria-label="可读模板摘要"><Text strong>已确认规则摘要 · v{template.version}</Text>
    <Paragraph type="secondary">模板 {template.template_id} · 版本 {template.version} · 配置 SHA256 {template.config_hash}</Paragraph>
    {template.scenario === 'data_compare' ? <>{(['left', 'right'] as const).map(side => <div key={side}><Paragraph><Text strong>{side === 'left' ? '左表角色' : '右表角色'}：</Text>
      {roles?.[side]?.formats?.join(' / ').toUpperCase() || '尚无格式配置'}{' · '}{roles?.[side]?.columns?.map(column => `${column.header}（${column.kinds?.map(kind => names[kind] || kind).join(' / ') || '未观察到非空值'}）`).join('、') || '尚无明确列名'}</Paragraph>
      <Paragraph><Text strong>清洗步骤：</Text>{roles?.[side]?.steps?.length ? roles[side].steps!.map(stepText).join('；') : '无清洗步骤'}</Paragraph></div>)}
      <Paragraph><Text strong>业务匹配键：</Text>{pairs(policy?.key_pairs)}</Paragraph><Paragraph><Text strong>左右列映射：</Text>{pairs(policy?.compare_pairs)}</Paragraph>
      <Paragraph><Text strong>金额列与容差：</Text>{pairs(policy?.amount_pairs)} · 容差 {policy?.amount_tolerance ?? '0'}</Paragraph>
      <Paragraph><Text strong>类型政策：</Text>{policy?.type_policy === 'text' ? '按文本比较' : '严格区分类型'}{' · '}<Text strong>空值政策：</Text>{policy?.null_policy === 'equal' ? '空值视为相同' : '空值不匹配'}</Paragraph>
      <Paragraph><Text strong>文本处理：</Text>{policy?.trim_text ? '去首尾空白' : '保留首尾空白'} · {policy?.casefold_text ? '忽略大小写' : '区分大小写'}</Paragraph></> :
      <><Paragraph><Text strong>原生项目关联：</Text>{String(template.config.link_id || '未配置')}</Paragraph>
        {template.scenario === 'classification' ? <Paragraph><Text strong>固定模型 / 设备：</Text>{String(template.config.model_id || '未配置')} / {String(template.config.device || 'cpu')} <Tag>人工确认单独记录</Tag></Paragraph> :
          <Paragraph><Text strong>识别引擎：</Text>{(template.config.engines as string[] | undefined)?.join('、') || '未配置'}</Paragraph>}</>}
    <Paragraph type="secondary">引擎版本：{typeof template.config.engine_version === 'string' ? template.config.engine_version : '当前模板未记录引擎版本'}</Paragraph>
  </section>
}
