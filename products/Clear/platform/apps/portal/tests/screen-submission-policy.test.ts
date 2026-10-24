import { expect, test } from 'vitest'
import { screenSubmissionPolicy } from '../src/features/ocr/screenSubmissionPolicy'

test.each([[40, 36000], [200, 72000], [240, 86400], [1000, 360000]])('new precision submission pins the original %i-image scope budget', (count, budget) => {
  expect(screenSubmissionPolicy(count, 'accuracy')).toMatchObject({
    engine_route: 'business_language_v1', primary_engine: 'glm', fallback_engines: ['hunyuan'],
    budget_seconds: 360, batch_budget_seconds: budget, request_timeout_seconds: 180,
    technical_fallback_reserve_seconds: 90, require_human_review: true, upgrade_trigger: 'failure_only',
  })
})
test('ordinary policy stays omitted except for its original mode', () => {
  expect(screenSubmissionPolicy(240, 'auto')).toEqual({ mode: 'auto' })
})
test.each([0, 1001, 1.5, NaN])('invalid fixed scope %s is rejected before admission', count => {
  expect(() => screenSubmissionPolicy(count, 'accuracy')).toThrow('1–1000')
})
