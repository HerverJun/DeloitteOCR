import { useState } from "react";
import { Button, Field, Input } from "@fluentui/react-components";
import { api } from "./api";
import { agentRequestId } from "./agentApi";
import type { AgentRun } from "./agentTypes";

const fields = [
  ["model_requests_per_run", "模型请求", "model_requests", 12],
  ["tool_calls_per_run", "工具调用", "tool_calls", 40],
  ["pages_per_run", "处理页次", "pages", 1000],
  ["engine_jobs_per_run", "引擎任务", "engine_jobs", 400],
  ["tokens_per_run", "Token", "tokens_charged", 64000],
] as const;

export function AgentBudget({ run, onUpdated, action, busy }: {
  run: AgentRun; onUpdated: (run: AgentRun) => void; action: (work: () => Promise<void>) => Promise<void>; busy: boolean;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const active = !["completed", "cancelled", "failed"].includes(run.status);
  const save = () => action(async () => {
    const limits: Record<string, number> = {};
    for (const [key, , , initial] of fields) {
      if (!draft[key]) continue;
      const value = Number(draft[key]);
      if (!Number.isSafeInteger(value) || value <= (run.limits[key] ?? initial)) throw new Error("新预算必须是大于当前上限的整数");
      limits[key] = value;
    }
    if (!Object.keys(limits).length) return;
    onUpdated(await api(`/agent/runs/${run.id}/budget`, "POST", { client_request_id: agentRequestId(), generation: run.generation, limits }));
    setDraft({});
  });
  return <details><summary>本轮预算与用量</summary>
    {fields.map(([key, label, usage, initial]) => <Field key={key} label={`${label}：${Number(run.usage[usage] ?? 0)} / ${run.limits[key] ?? initial}`}>
      {active && <Input type="number" aria-label={`${label}新上限`} placeholder="输入更高的上限" value={draft[key] ?? ""} onChange={(_, d) => setDraft(old => ({ ...old, [key]: d.value }))} />}
    </Field>)}
    <p>Token 实际确认 {Number(run.usage.tokens_actual ?? 0)}，累计预估 {Number(run.usage.tokens_estimated ?? 0)}，尚未确认 {Number(run.usage.tokens_unconfirmed ?? 0)}。费用未知。</p>
    {active && <Button size="small" disabled={busy} onClick={() => void save()}>增加本轮预算</Button>}
  </details>;
}
