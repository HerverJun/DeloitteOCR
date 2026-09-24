import { api } from "./api";
import type { AgentEvidence } from "./agentTypes";

export async function resolveAgentEvidence(reference: AgentEvidence, projectId: string) {
  if (reference.project_id !== projectId) throw Error("证据属于另一项目，请切换后查看。");
  return api<{ reference: AgentEvidence; image_id: string }>(`/agent/evidence/${encodeURIComponent(reference.ref_id)}?project_id=${encodeURIComponent(projectId)}`);
}
