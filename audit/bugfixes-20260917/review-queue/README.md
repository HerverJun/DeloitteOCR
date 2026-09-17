# R7 / R8 queue fixes

The document queue and structure panel now share candidate selection and proposal validity within a single SQLite read snapshot. Candidates must match the current result image version/hash and current PDF tool run, with the latest set selected per provider. Pending/deferred proposals additionally require the current revision and a live candidate set. A check counts as current only when its revision/version and candidate fingerprint still match and the table tool is ready, empty, or not applicable.

Historical accepted/kept/rejected decisions remain visible in the `all` filter, including after a tool upgrade or unrelated edit. Their `can_apply` value is always false; they do not suppress current fusion alerts. Direct structure decisions use the same current-candidate guard, while committed idempotent request replays retain their existing behavior.

Multimodal proposal and task queue IDs now incorporate their durable IDs. Repeated requests for the same target remain distinct, and text-target rebasing or task status changes do not change item identity. Pagination is stable for an unchanged queue.

Validation:

- `before.log`: the initial 8 new regressions ran against the original implementation; 6 failed and 2 passed.
- `targeted-regressions.log`: 81 tests passed (the initial 8 new cases plus 73 existing structure, multimodal store/integration and geometry tests).
- `after.log`: all 12 final new regressions passed, including four added checks for completed-history states, candidate fingerprint changes, active image versions and concurrent Store writes.
- Across the final suites, 85 distinct tests passed. No GPU inference, browser, full-suite run or commit was performed for this subtask.

Product files changed: `src/ocr_workbench/document_review.py` and `src/ocr_workbench/structure_store.py`. Formal regressions are in `tests/test_audit_review_queue.py`. Historical audit records were not changed.

Compatibility: queue IDs for individually identified suggestions/jobs change once on upgrade, but these IDs are computed presentation identities rather than persisted result/task IDs. Old candidate records and completed decisions remain stored. No schema migration is required.
