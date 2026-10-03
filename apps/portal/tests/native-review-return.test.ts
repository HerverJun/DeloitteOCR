import { expect, test } from 'vitest'
import { nativeReviewReturn } from '../src/features/catalog/nativeReviewReturn'

test('native review return retains the exact item, filter and page in the original workspace', () => {
  const path = '/batches?workspace=wReview&batch=bReview&item=iLast&filter=deferred&page=4&todo=tReview#evidence'
  expect(nativeReviewReturn(path, 'wReview')).toBe(path)
})

test.each([
  '//other.invalid/batches?workspace=wReview',
  '/batches?workspace=wOther&batch=bOther',
  '/apps?workspace=wReview',
  '/\\other.invalid/batches?workspace=wReview',
])('rejects a return outside the review workspace: %s', value => {
  expect(nativeReviewReturn(value, 'wReview')).toBeNull()
})
