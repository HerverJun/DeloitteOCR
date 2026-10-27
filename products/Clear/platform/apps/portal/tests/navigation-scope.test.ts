import { expect, test } from 'vitest'
import { navigationHref } from '../src/clear-ui/navigation'

test.each([
  ['/apps/forensics/aigc', '/apps/audio', 'analysis=aigcJob&snapshot=fixed&sha256=hash&revision=3'],
  ['/apps/ocr/documents', '/apps/ocr/start', 'document=pdfRun&asset=pdfAsset&pane=source&revision=2'],
  ['/apps/forensics', '/apps/comparisons', 'inspection=report&pane=heatmap'],
  ['/apps/data', '/apps/audio', 'session=dataSession&table=table4&offset=40'],
  ['/apps/comparisons', '/settings', 'comparison=cmpOne&comparison_revision=5'],
])('switching %s to %s keeps project/task scope without source result identities', (from, to, privateQuery) => {
  expect(navigationHref(to, `?workspace=project&task_return=auditTask&${privateQuery}`, '#source-pane', from))
    .toBe(`${to}?workspace=project&task_return=auditTask`)
})

test('a tool result cannot create an Agent source attachment or override its return task', () => {
  expect(navigationHref('/agent', '?workspace=project&task_return=auditTask&document=oldDocument&analysis=oldAnalysis&revision=4', '#preview', '/apps/audio'))
    .toBe('/agent?workspace=project&session=auditTask')
  expect(navigationHref('/apps/audio', '?workspace=project&session=auditTask&continue=oldRequest&assets=oldAttachment', '', '/agent'))
    .toBe('/apps/audio?workspace=project&task_return=auditTask')
})

test('same-page navigation retains a fixed citation and pane while dropping automatic continuation', () => {
  expect(navigationHref('/apps/audio', '?workspace=project&analysis=audioJob&snapshot=fixed&sha256=hash&revision=3&continue=oldRequest', '#waveform', '/apps/audio'))
    .toBe('/apps/audio?workspace=project&analysis=audioJob&snapshot=fixed&sha256=hash&revision=3#waveform')
})
