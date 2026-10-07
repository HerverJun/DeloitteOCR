export type AgentEvent = {
  seq: number; run_id: string | null; kind: string; payload: Record<string, unknown>; created: string
}
export type AgentPhase = 'preparing' | 'waiting_model' | 'receiving_response' | 'validating_calls' | 'executing_tool' | 'waiting_job' | 'waiting_user' | 'finalizing' | 'completed' | 'failed' | 'stopped' | 'manual' | 'needs_verification' | string
export type AgentActivity = { version: 1; generation: number; phase: AgentPhase; request_id?: string; display_id?: string; [key: string]: unknown }
export type ResponsePreview = { version: 1; generation: number; request_id: string; display_id: string; revision: number; mode: 'snapshot' | 'append' | 'replace'; offset: number; content: string; source: 'text' | 'answer' | 'delivery'; provisional: true; truncated?: boolean }
export type ResponseState = { version: 1; generation: number; request_id: string; display_id: string; revision: number; status: 'generated' | 'confirmed' | 'interrupted'; content?: string; references?: AgentRef[]; reason?: string }
export type AgentRun = { id: string; status: string; error: string | null }
export type AgentSession = { id: string; workspace_id: string; name: string; selected_assets: string[]; initial_refs?: AgentRef[]; runs?: AgentRun[] }
export type AuditPlanItem = { key: string; title: string; purpose: string; status: string; depends_on: string[]; result_refs: AgentRef[] }
export type AuditTask = {
  id: string; session_id: string; workspace_id: string; title: string; goal: string; revision: number
  scope_revision: number; status: string; manual: boolean; pending_scope: unknown; selected_assets: string[]
  initial_refs: AgentRef[]; plan: AuditPlanItem[]; runs: AgentRun[]; result_refs: AgentRef[]; artifact_refs: AgentRef[]; job_refs: AgentRef[]
  last_manual_change?: { summary: string; references: AgentRef[] }
}
export type AuditProfile = { revision: number; audit_object: string; period_start: string; period_end: string; objective: string; rules: string }
export type Controller = {
  configured: boolean; available: boolean; reason?: string; protocol?: string; base_url?: string; model?: string
  token_parameter?: string; context_cap?: number; max_output_tokens?: number; total_seconds?: number
  streaming?: boolean; preflight?: { context_budget?: ContextBudget; context_metrics?: ContextBudget; [key: string]: unknown }
  capabilities?: { production_preflight?: { context_budget?: ContextBudget; context_metrics?: ContextBudget; [key: string]: unknown }; streaming?: StreamingCapability; [key: string]: unknown }
}
export type StreamingCapability = { requested: boolean; tested: boolean; status: string; probe_rounds: number; business_data_sent: false; detected_at?: string; mode: 'sse' | 'json' | 'disabled' | 'unknown'; text?: { supported: boolean; preview_observed: boolean }; answer?: { supported: boolean; preview_observed: boolean } }
export type ContextBudget = {
  estimation_method?: string; payload_bytes?: number; input_tokens_estimate?: number; output_reserve?: number
  context_cap?: number; component_tokens?: Record<string, number>; selected_tools?: string[]; reclaimed_tools?: string[]
  compressed?: boolean; [key: string]: unknown
}
export type AgentSkillResource = { path: string; kind: string; sha256: string; bytes: number }
export type AgentSkill = {
  name: string; description: string; source: string; version: string; compatibility: string; compatible: boolean; reason: string
  allowed_tools: string[] | null; resources: AgentSkillResource[]; active: boolean; active_version: string | null; source_changed: boolean
}
export type AgentSkillsPage = { items: AgentSkill[]; active: AgentSkill[]; total: number; next_offset: number | null; rejected: { source: string; reason: string }[] }
export type AgentSkillContent = {
  name: string; version: string; path: string; sha256: string; content: string; encoding: 'utf-8' | 'base64'
  total_chars: number; next_offset: number | null; kind: string; source_changed: boolean; unsupported?: false
}
export type AgentSkillUnsupportedResource = { name: string; version: string; path: string; kind: 'script'; unsupported: true; reason: string }
export type AgentSkillResourceResponse = AgentSkillContent | AgentSkillUnsupportedResource
export const agentBase = '/api/platform/v1'
export const requestId = () => `r${crypto.randomUUID().replaceAll('-', '')}`
export class AgentRequestError extends Error { constructor(message: string, public readonly status: number) { super(message); this.name = 'AgentRequestError' } }
export async function agentRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${agentBase}${path}`, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new AgentRequestError(typeof body.detail === 'string' ? body.detail : `操作未完成（${response.status}）`, response.status)
  }
  return response.json() as Promise<T>
}
export function agentWrite<T>(path: string, body: unknown, method = 'POST') {
  return agentRequest<T>(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}
import type { AgentRef } from '../../../../../packages/contracts/types.generated'
export type { AgentRef }
