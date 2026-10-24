import { useState } from 'react'
import { Button, Space, Tag, Typography } from 'antd'
import { diagnosticActionReason, diagnosticRange, type Diagnostic, type Diagnostics } from './screenDiagnosticWorkflow'

export function ScreenDiagnostics({ data, selected, select, action, busy }: { data: Diagnostics; selected?: string; select: (item: Diagnostic) => void; action: (item: Diagnostic, name: string) => void; busy: boolean }) {
  const [offset, setOffset] = useState(0)
  const summary = data.summary, item = data.items.find(value => value.id === selected) || data.items[0]
  return <section aria-label="具体异常与处理依据">
    <Typography.Title level={5}>具体疑点与处理依据</Typography.Title>
    <Typography.Paragraph>共 {summary.issues} 项 · {summary.groups} 组 · 单格 {summary.cell} / 表区 {summary.table} / 全图 {summary.image}。全图待复核不代表已定位错误。</Typography.Paragraph>
    <Typography.Paragraph type="secondary">剩余处理预算：{data.budget.remaining_seconds === null ? '尚未核实' : `${Math.ceil(data.budget.remaining_seconds)} 秒`}；候选和程序检查仍需人工核对。</Typography.Paragraph>
    <Space wrap>{data.items.slice(offset, offset + 8).map(value => <Button key={value.id} aria-pressed={item?.id === value.id} onClick={() => select(value)}>{diagnosticRange(value)} · {value.reason}</Button>)}</Space>
    {data.items.length > 8 && <Space><Button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 8))}>疑点上一页</Button><span>{offset + 1}–{Math.min(offset + 8, data.items.length)} / {data.items.length}</span><Button disabled={offset + 8 >= data.items.length} onClick={() => setOffset(offset + 8)}>疑点下一页</Button></Space>}
    {item && <div className="screen-diagnostic-detail"><p><Tag>{diagnosticRange(item)}</Tag>{item.reason}</p>
      <details><summary>原图与独立证据（{item.evidence.length}）</summary><p>定位依据：{item.location.basis || '未核实'}</p>
        {item.location.box && <p>原图范围：{item.location.box.join('，')}</p>}
        {item.evidence.length ? item.evidence.map((evidence, index) => <pre key={index} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(evidence, null, 2)}</pre>) : <p>尚无独立文字证据，请核对原图。</p>}</details>
      <Space wrap>{item.applicable_actions.map(option => {
        const supported = ['review_original', 'structure_compare', 'precision_subbatch', 'request_better_source'].includes(option.action)
        const enabled = option.enabled && supported
        return <span key={option.action}><Button disabled={busy || !enabled} onClick={() => action(item, option.action)}>{option.label}</Button>{!enabled && <Typography.Text type="secondary">（{supported ? diagnosticActionReason(option.reason) : `${diagnosticActionReason(option.reason)}；此入口暂未提供此操作`}）</Typography.Text>}</span>
      })}</Space>
    </div>}
  </section>
}
