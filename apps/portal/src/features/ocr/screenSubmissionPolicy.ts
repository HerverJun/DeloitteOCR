/** New UI submissions explicitly opt into the measured bounded precision route. */
export function screenSubmissionPolicy(count: number, mode: 'auto' | 'accuracy') {
  if (!Number.isInteger(count) || count < 1 || count > 1000) throw new Error('请选择 1–1000 张图片后开始')
  if (mode === 'auto') return { mode: 'auto' as const }
  return {
    primary_engine: 'glm', fallback_engines: ['hunyuan'], max_upgrades: 1,
    mode: 'accuracy' as const, upgrade_trigger: 'failure_only', layout_evidence: true,
    require_human_review: true, budget_seconds: 360, request_timeout_seconds: 180,
    engine_route: 'business_language_v1', technical_fallback_reserve_seconds: 90,
    batch_budget_seconds: Math.max(36000, count * 360),
  }
}
