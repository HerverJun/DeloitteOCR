import { useState } from "react";
import { Button } from "@fluentui/react-components";
import { screenDiagnosticLocation, type ScreenDiagnostic, type ScreenDiagnostics } from "./screenBatchWorkflow";

export function ScreenDiagnosticPanel({ data, index, select, action, busy }: { data: ScreenDiagnostics; index: number; select: (index: number) => void; action: (name: string) => void; busy: boolean }) {
  const [offset, setOffset] = useState(0);
  const item = data.items[index] || data.items[0];
  return <section className="screen-diagnostics" aria-label="具体异常与处理依据">
    <h4>具体疑点与处理依据</h4>
    <p>共 {data.summary.issues} 项 · {data.summary.groups} 组 · 单格 {data.summary.cell} / 表区 {data.summary.table} / 全图 {data.summary.image}。全图待核对不代表已定位错误。</p>
    <p className="screen-batch-small">剩余处理预算：{data.budget.remaining_seconds === null ? "尚未核实" : `${Math.ceil(data.budget.remaining_seconds)} 秒`}；不代表推理耗时估计。</p>
    <div className="screen-diagnostic-list">{data.items.slice(offset, offset + 8).map((value, local) => <Button key={value.id} size="small" aria-pressed={value.id === item?.id} onClick={() => select(offset + local)}>{screenDiagnosticLocation(value).reason} · {value.reason}</Button>)}</div>
    {data.items.length > 8 && <div className="screen-batch-pages"><Button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 8))}>疑点上一页</Button><span>{offset + 1}–{Math.min(offset + 8, data.items.length)} / {data.items.length}</span><Button disabled={offset + 8 >= data.items.length} onClick={() => setOffset(offset + 8)}>疑点下一页</Button></div>}
    {item && <><strong>{screenDiagnosticLocation(item).reason} · {item.reason}</strong>
      <details><summary>原图与独立证据（{item.evidence.length}）</summary><p>定位依据：{item.location.basis || "尚未核实"}</p>{item.location.box && <p>原图范围：{item.location.box.join("，")}</p>}
        {item.evidence.length ? item.evidence.map((value, i) => <pre key={i}>{JSON.stringify(value, null, 2)}</pre>) : <p>尚无独立文字证据，请核对原图。</p>}</details>
      <div className="screen-diagnostic-actions">{item.applicable_actions.map(option => {
        const supported = ["review_original", "structure_compare", "precision_subbatch", "request_better_source"].includes(option.action), enabled = option.enabled && supported;
        const nativeReason = option.reason === "remaining_budget_insufficient" ? "剩余预算不足" : option.reason === "capability_not_verified" ? "能力尚未核实" : option.reason || "当前不可用";
        const reason = supported ? nativeReason : `${nativeReason}；此入口暂未提供此操作`;
        return <span key={option.action}><Button size="small" disabled={busy || !enabled} onClick={() => action(option.action)}>{option.label}</Button>{!enabled && <small>（{reason}）</small>}</span>;
      })}</div></>}
  </section>;
}
