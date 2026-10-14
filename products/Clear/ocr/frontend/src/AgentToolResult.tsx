import { Button } from "@fluentui/react-components";
import type { AgentDestination, AgentEvidence, AgentResult } from "./agentTypes";

type Item = Record<string, unknown>;
const items = (value: unknown): Item[] => Array.isArray(value) ? value.filter((v): v is Item => !!v && typeof v === "object") : [];
const states: Record<string, string> = { queued: "排队", running: "运行", waiting_gpu: "等待识别", paused: "暂停", interrupted: "中断", succeeded: "完成", failed: "失败", cancelled: "取消" };

export function AgentToolResult({ result, navigate }: { result: AgentResult; navigate: (ref: AgentEvidence, destination?: AgentDestination) => void }) {
  const evidence = result.evidence_refs[0];
  const issues = items(result.data.issues), proposals = items(result.data.proposals), reviews = items(result.data.reviews);
  return <>
    {!!issues.length && <details open><summary>核对线索（{String(result.data.issue_count ?? issues.length)}）</summary><ul className="agent-result-list">{issues.map((issue, index) => {
      const target = issue.target as { table?: number; row?: number; column?: number } | undefined;
      return <li key={index}><p>{String(issue.message ?? issue.kind)}</p>
        {issue.original !== undefined && <p>原值：{String(issue.original)}</p>}
        {issue.expected_sum !== undefined && <p>计算合计：{String(issue.expected_sum)} · 差额：{String(issue.difference)} · 舍入容差：{String(issue.rounding_tolerance)}</p>}
        {issue.unit != null && <small>单位：{String(issue.unit)}</small>}
        {evidence && target && [target.table, target.row, target.column].every(Number.isInteger) && <Button size="small" onClick={() => navigate(evidence, { kind: "cell", table: target.table!, row: target.row!, column: target.column! })}>定位核对单元格</Button>}
      </li>;
    })}</ul><small>核对线索不会自动修改原文。</small></details>}
    {!!proposals.length && <details open><summary>结构建议（{proposals.length}）</summary><ul className="agent-result-list">{proposals.map(proposal => <li key={String(proposal.proposal_id)}><span>{String(proposal.kind)} · {String(proposal.state)}</span>{evidence && <Button size="small" onClick={() => navigate(evidence, { kind: "structure", id: String(proposal.proposal_id) })}>查看结构建议</Button>}</li>)}</ul></details>}
    {reviews.map(review => {
      const reference = result.evidence_refs.find(ref => ref.ref_id === review.evidence_ref_id);
      return <details open key={String(review.job_id)}><summary>视觉审校建议</summary><p>{String(review.summary || "等待审校结果")}</p><ul className="agent-result-list">{items(review.proposals).map(proposal => <li key={String(proposal.id)}><span>{String(proposal.status)} · {String(proposal.state)}</span>{reference && <Button size="small" onClick={() => navigate(reference, { kind: "multimodal", id: String(proposal.id) })}>查看视觉建议</Button>}</li>)}</ul><small>采用、拒绝与撤销均在原审校面板中操作。</small></details>;
    })}
    {!!result.job_refs.length && <details><summary>后台任务（{String(result.data.job_count ?? result.job_refs.length)}）</summary><ul className="agent-result-list">{result.job_refs.map(job => <li key={job.job_id}><span>{states[job.state] || job.state} · {job.ownership === "created" ? "本轮创建" : "复用任务"}</span><code>{job.job_id}</code></li>)}</ul>{Number(result.data.job_count) > result.job_refs.length && <small>此处显示前 {result.job_refs.length} 项；完整任务请在工作台任务列表查看。</small>}</details>}
  </>;
}
