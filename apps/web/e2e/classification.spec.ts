import { expect, test, type BrowserContext, type Page, type TestInfo } from '@playwright/test';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

let cancellationJobId: string | undefined;
test.afterEach(async ({ request }) => {
  if (!cancellationJobId) return;
  const job = await (await request.get(`/api/v1/jobs/${cancellationJobId}`)).json();
  if (['queued', 'running', 'pausing', 'paused'].includes(job.state)) {
    await request.post(`/api/v1/jobs/${cancellationJobId}/actions`, {
      headers: { 'X-Platform-Request': '1' }, data: { action: 'cancel' },
    });
  }
});

test('real GPU classification, model evaluation, review and exports', async ({ page, request }) => {
  test.setTimeout(90000);
  const projects = await (await request.get('/api/v1/projects')).json();
  const demo = projects.find((project: { name: string }) => project.name === '流程验证 · 合成图形');
  expect(demo).toBeTruthy();
  await page.addInitScript(id => localStorage.setItem('prism-project', id), demo.id);
  const models = await (await request.get(`/api/v1/projects/${demo.id}/models`)).json();
  const model = models.find((entry: { name: string }) => entry.name.startsWith('resnet18'));
  expect(model).toBeTruthy();
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('menuitem', { name: '模型库' }).click();
  await page.getByRole('button', { name: '评估详情' }).first().click();
  await expect(page.getByRole('heading', { name: '混淆矩阵' })).toBeVisible();
  await expect(page.getByText('CUDA GPU', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /^关\s*闭$/ }).click();
  await page.getByRole('menuitem', { name: '分类与复核' }).click();
  await page.getByRole('button', { name: /新建分类任务/ }).click();
  await page.locator('.ant-select').filter({ has: page.getByRole('combobox', { name: '模型版本' }) }).locator('.ant-select-selector').click();
  await page.getByTitle(model.name, { exact: true }).last().click();
  await expect(page.locator('.ant-select-dropdown:visible')).toHaveCount(0);
  await page.locator('.ant-select').filter({ has: page.getByRole('combobox', { name: '图片范围' }) }).locator('.ant-select-selector').click();
  await page.getByText('项目内全部图片', { exact: true }).last().click();
  const submitted = page.waitForResponse(response => response.url().endsWith('/batch-jobs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '开始分类', exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  const { job_id: jobId } = await response.json();
  await expect.poll(async () => (await (await request.get(`/api/v1/jobs/${jobId}`)).json()).state, { timeout: 60000 }).toBe('succeeded');
  const jobRow = page.getByRole('row').filter({ hasText: jobId.slice(-8) });
  await jobRow.getByRole('button', { name: /^详\s*情$/ }).click();
  await expect(page.locator('.result-card')).toHaveCount(12);
  await page.locator('.result-card').first().getByRole('button', { name: /查看并确认/ }).click();
  await expect(page.locator('.review-image img')).toBeVisible();
  const annotation = page.waitForResponse(response => response.url().endsWith('/annotations') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '确认并保存', exact: true }).click();
  expect((await annotation).status()).toBe(200);
  await expect(page.getByRole('dialog')).not.toBeVisible();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: /CSV/ }).click();
  const download = await downloadPromise;
  const csvPath = await download.path();
  const csv = readFileSync(csvPath!, 'utf8');
  expect(csv.trim().split(/\r?\n/)).toHaveLength(61);
  expect(csv).toContain('needs_review');
  const jsonl = await (await request.get(`/api/v1/jobs/${jobId}/export?format=jsonl`)).text();
  const records = jsonl.trim().split('\n').map(line => JSON.parse(line));
  expect(records).toHaveLength(60);
  expect(records.every(row => row.model_version_id === model.id && row.status === 'needs_review')).toBeTruthy();
  await page.screenshot({ path: '../../reports/screenshots/classification-results.png', fullPage: true });

  // A paused job makes cancellation deterministic even with a fast GPU.
  const headers = { 'X-Platform-Request': '1' };
  const toCancel = await (await request.post(`/api/v1/projects/${demo.id}/batch-jobs`, {
    headers, data: { model_id: model.id, device: 'cuda', scope: 'all', batch_size: 1 },
  })).json();
  cancellationJobId = toCancel.job_id;
  expect((await request.post(`/api/v1/jobs/${toCancel.job_id}/actions`, { headers, data: { action: 'pause' } })).ok()).toBeTruthy();
  await expect.poll(async () => (await (await request.get(`/api/v1/jobs/${toCancel.job_id}`)).json()).state).toBe('paused');
  const pausedRow = page.getByRole('row').filter({ hasText: toCancel.job_id.slice(-8) });
  await pausedRow.getByRole('button', { name: /取消/ }).click();
  await expect(page.getByText('取消此任务？', { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect.poll(async () => (await (await request.get(`/api/v1/jobs/${toCancel.job_id}`)).json()).state).toBe('cancelled');
  expect(errors).toEqual([]);
});

type DispositionPayload = {
  item_ids: string[]; state: string; label_id: string | null; note: string;
  expected_revisions: Record<string, number>; request_id: string; operation_id: string;
  instance_id: string; actor: { id: string; display_name: string; source: string }; intent: string;
};

// This fixture drives the real built UI. Backend persistence, source-byte checks
// and platform adoption are verified separately by native/integration tests.
async function classificationProtocol(context: BrowserContext) {
  const project = {
    id: 'project_ui_v2', name: 'UI 协议复核夹具', description: '受控分类处置协议，无模型运行',
    labels: ['A', 'B'].map(letter => ({ label_id: `class_${letter.toLowerCase()}`, display_name: `类别 ${letter}`, definition: '' })),
    active_model_id: null, model_count: 0, asset_count: 2, asset_counts: [], job_counts: [],
  };
  const job = { id: 'job_ui_v2', kind: 'inference', state: 'succeeded', stage: '夹具结果已准备',
    total: 2, done: 2, failed: 0, created_at: '2026-10-02T00:00:00Z', error: null, result: null };
  const image = '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="320"><rect width="480" height="320" fill="#d7e5bd"/><text x="24" y="80" font-size="32">UI protocol fixture</text></svg>';
  const rows = ['第一张.svg', '第二张.svg'].map((name, index) => ({
    id: `item_${index + 1}`, asset_id: `asset_${index + 1}`, name,
    path: `F:/OCR/ui-protocol-fixture/${name}`, state: 'succeeded', confirmed_label: null as string | null,
    human_label_id: null as string | null, disposition_state: 'unreviewed', disposition_revision: 0,
    disposition_note: null as string | null, error: null,
    prediction: { label_id: 'class_a', display_name: '类别 A', score: 0.91, status: 'needs_review' },
  }));
  const requests: { page: number; body: DispositionPayload; outcome: string }[] = [];
  const reads = new Map<Page, number>();
  const receipts = new Map<string, { body: string; receipt: Record<string, unknown> }>();
  const unexpected: string[] = [];
  let loseNextResponse = false;
  let healthReads = 0;
  await context.addInitScript(id => localStorage.setItem('prism-project', id), project.id);
  await context.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (request.method() === 'GET') {
      if (path === '/api/v1/projects') return json([project]);
      if (path === `/api/v1/projects/${project.id}`) return json(project);
      if (path === `/api/v1/projects/${project.id}/jobs`) return json([job]);
      if (path === `/api/v1/projects/${project.id}/models`) return json([]);
      if (path === '/api/v1/runtime') return json({ python: 'CPU protocol fixture', data_dir: 'F:/OCR/ui-protocol-fixture',
        free_bytes: 1000000000, packages: {}, gpu: null });
      if (path === '/api/v1/health') { healthReads++; return json({ instance_id: 'uiFixtureInstance' }); }
      if (path === `/api/v1/jobs/${job.id}/results`) {
        const page = request.frame().page();
        reads.set(page, (reads.get(page) || 0) + 1);
        const visible = rows.filter(row => url.searchParams.get('status') !== 'needs_review' || row.disposition_state !== 'confirmed');
        const counts = Object.fromEntries(['unreviewed', 'confirmed', 'deferred', 'reopened'].map(state => [state, rows.filter(row => row.disposition_state === state).length]));
        return json({ items: visible, total: visible.length, disposition_counts: counts });
      }
      if (/^\/api\/v1\/assets\/asset_[12]\/(thumbnail|image)$/.test(path)) {
        return route.fulfill({ contentType: 'image/svg+xml', body: image });
      }
    }
    if (request.method() === 'POST' && path === `/api/v1/jobs/${job.id}/dispositions`) {
      const body = request.postDataJSON() as DispositionPayload;
      const event = { page: context.pages().indexOf(request.frame().page()), body, outcome: 'pending' };
      requests.push(event);
      if (request.headers()['x-platform-request'] !== '1' || body.instance_id !== 'uiFixtureInstance'
          || !body.request_id || !body.operation_id || body.actor?.source !== 'local' || body.intent !== 'manual') {
        unexpected.push('Invalid v2 UI disposition payload');
        event.outcome = 'invalid'; return json({ detail: 'UI 协议字段不完整' }, 422);
      }
      const prior = receipts.get(body.request_id);
      if (prior) {
        event.outcome = prior.body === JSON.stringify(body) ? 'replayed' : 'identity_conflict';
        return prior.body === JSON.stringify(body) ? json(prior.receipt) : json({ detail: '请求身份已用于不同内容' }, 409);
      }
      const selected = rows.filter(row => body.item_ids.includes(row.id));
      if (!selected.length || selected.some(row => body.expected_revisions[row.id] !== row.disposition_revision)) {
        event.outcome = 'revision_conflict'; return json({ detail: '处置记录已变化，请刷新后重试' }, 409);
      }
      const items = selected.map(row => {
        const previous = { revision: row.disposition_revision, human_label_id: row.human_label_id, state: row.disposition_state };
        row.disposition_revision++; row.disposition_state = body.state;
        row.human_label_id = body.label_id; row.disposition_note = body.note;
        if (body.state === 'confirmed') row.confirmed_label = body.label_id;
        return { item_id: row.id, asset_id: row.asset_id, path: row.path,
          original_sha256: createHash('sha256').update(image).digest('hex'), prediction: row.prediction,
          human_label_id: row.human_label_id, revision: row.disposition_revision, previous };
      });
      const receipt = { operation_id: body.operation_id, request_id: body.request_id,
        payload_hash: createHash('sha256').update(JSON.stringify(body)).digest('hex'), state: 'committed',
        project_id: project.id, instance_id: body.instance_id, job_id: job.id, updated: items.length,
        disposition_state: body.state, human_label_id: body.label_id,
        revisions: Object.fromEntries(items.map(item => [item.item_id, item.revision])), items,
        actor: body.actor, note: body.note, intent: body.intent, created: '2026-10-02T00:00:00Z' };
      receipts.set(body.request_id, { body: JSON.stringify(body), receipt });
      if (loseNextResponse) { loseNextResponse = false; event.outcome = 'committed_response_lost'; return route.abort('connectionreset'); }
      event.outcome = 'committed'; return json(receipt);
    }
    unexpected.push(`${request.method()} ${path}`);
    return json({ detail: 'Unexpected protocol fixture request' }, 500);
  });
  return { rows, requests, unexpected, resultsReads: (page: Page) => reads.get(page) || 0,
    get healthReads() { return healthReads; }, loseResponse() { loseNextResponse = true; },
    async evidence(testInfo: TestInfo) {
      await testInfo.attach('v2-protocol-events', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
        qualification: 'built UI events with controlled API protocol fixture; no native backend/model/GPU',
        requests, rows, healthReads, unexpected,
      }, null, 2)) });
    } };
}

async function openV2Results(page: Page) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'UI 协议复核夹具', exact: true })).toBeVisible();
  await page.getByRole('menuitem', { name: '分类与复核' }).click();
  await page.getByRole('button', { name: /^详\s*情$/ }).click();
  await page.locator('.ant-select').filter({ has: page.getByRole('combobox', { name: '筛选分类结果', exact: true }) }).locator('.ant-select-selector').click();
  await page.getByTitle('全部结果', { exact: true }).last().click();
  await expect(page.locator('.result-card')).toHaveCount(2);
  await expect(page.getByText('处置身份：本机分类操作者（本机来源，不代表组织认证）。响应丢失时重复同一操作会核对原请求。', { exact: true })).toBeVisible();
}

async function openV2Item(page: Page, name = '第一张.svg') {
  await page.locator('.result-card').filter({ hasText: name }).getByRole('button', { name: '查看并处置' }).click();
  await expect(page.getByRole('dialog').getByRole('img', { name, exact: true })).toBeVisible();
}

async function chooseV2Label(page: Page, label: string) {
  await page.getByRole('dialog').locator('.ant-select').filter({ has: page.getByRole('combobox', { name: '人工确认类别', exact: true }) }).locator('.ant-select-selector').click();
  await page.getByTitle(label, { exact: true }).last().click();
}

async function pendingV2Requests(page: Page): Promise<DispositionPayload[]> {
  return page.evaluate(() => Object.values(JSON.parse(sessionStorage.getItem('classification-dispositions:project_ui_v2:job_ui_v2') || '{}')));
}

test.describe('v2 UI protocol fixture', () => {
  test('lost committed response retains complete identity and revisions across remount, then saves next item', async ({ context, page }, testInfo) => {
    const fixture = await classificationProtocol(context);
    try {
      await openV2Results(page); await openV2Item(page);
      await chooseV2Label(page, '类别 B');
      await page.getByRole('textbox', { name: '人工处置说明' }).fill('核对原图后采用 B');
      fixture.loseResponse();
      await page.getByRole('button', { name: '保存并下一项', exact: true }).click();
      await expect(page.getByText('Failed to fetch', { exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '保存并下一项', exact: true })).toBeEnabled();
      expect(fixture.requests).toHaveLength(1);
      const first = structuredClone(fixture.requests[0].body);
      expect(first.expected_revisions).toEqual({ item_1: 0 });
      expect(first.actor).toEqual({ id: 'local-classification-user', display_name: '本机分类操作者', source: 'local' });
      expect(await pendingV2Requests(page)).toEqual([first]);
      expect(fixture.rows[0].disposition_revision).toBe(1);

      // Reopening now sees revision 1, but the same intent must replay its
      // original full revision-0 request from session storage.
      await openV2Results(page); await openV2Item(page);
      await expect(page.getByRole('textbox', { name: '人工处置说明' })).toHaveValue(first.note);
      await page.getByRole('button', { name: '保存并下一项', exact: true }).click();
      await expect(page.getByRole('dialog').getByRole('img', { name: '第二张.svg', exact: true })).toBeVisible();
      expect(fixture.requests).toHaveLength(2);
      expect(fixture.requests[1].body).toEqual(first);
      expect(fixture.requests[1].outcome).toBe('replayed');
      expect(fixture.healthReads).toBe(1);
      expect(await pendingV2Requests(page)).toEqual([]);
      expect(fixture.rows[0].disposition_revision).toBe(1);
      expect(fixture.rows[0].prediction.label_id).toBe('class_a');

      await page.getByRole('textbox', { name: '人工处置说明' }).fill('第二张独立确认');
      await page.getByRole('button', { name: '保存并下一项', exact: true }).click();
      await expect(page.getByRole('dialog')).not.toBeVisible();
      expect(fixture.requests[2].body.item_ids).toEqual(['item_2']);
      expect(fixture.requests[2].body.expected_revisions).toEqual({ item_2: 0 });
      expect(fixture.requests[2].body.operation_id).not.toBe(first.operation_id);
      expect(fixture.requests[2].body.request_id).not.toBe(first.request_id);
      expect(fixture.unexpected).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath('saved-next.png'), fullPage: true });
    } finally { await fixture.evidence(testInfo); }
  });

  test('409 preserves entered explanation and waits for explicit refresh without automatic write', async ({ context, page }, testInfo) => {
    const fixture = await classificationProtocol(context);
    try {
      await openV2Results(page); await openV2Item(page);
      const note = '冲突时保留我输入的说明';
      await page.getByRole('textbox', { name: '人工处置说明' }).fill(note);
      const reads = fixture.resultsReads(page);
      Object.assign(fixture.rows[0], { disposition_revision: 1, disposition_state: 'confirmed',
        human_label_id: 'class_b', confirmed_label: 'class_b', disposition_note: '另一窗口已确认 B' });
      await page.getByRole('button', { name: /保存并下一项$/ }).click();
      await expect(page.getByText('处置记录已变化，请刷新后重试', { exact: true })).toBeVisible();
      await expect(page.getByRole('textbox', { name: '人工处置说明' })).toHaveValue(note);
      await expect(page.getByRole('dialog').locator('.ant-select-selection-item')).toHaveText('类别 A');
      await expect(page.getByRole('button', { name: /保存并下一项$/ })).toBeEnabled();
      expect(fixture.requests).toHaveLength(1);
      expect(fixture.requests[0].outcome).toBe('revision_conflict');
      expect(fixture.resultsReads(page)).toBe(reads);
      expect(fixture.rows[0].human_label_id).toBe('class_b');
      expect(await pendingV2Requests(page)).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath('409-retained-explanation.png'), fullPage: true });

      await page.getByRole('dialog').getByRole('button', { name: /^关\s*闭$/ }).click();
      await page.getByRole('button', { name: '刷新结果并重新核对', exact: true }).click();
      await expect.poll(() => fixture.resultsReads(page)).toBe(reads + 1);
      expect(fixture.requests).toHaveLength(1);
      await openV2Item(page);
      await expect(page.getByRole('dialog').locator('.ant-select-selection-item')).toHaveText('类别 B');
      await expect(page.getByRole('textbox', { name: '人工处置说明' })).toHaveValue('另一窗口已确认 B');
      await page.getByRole('textbox', { name: '人工处置说明' }).fill('刷新核对后明确保存 B');
      await page.getByRole('button', { name: /保存并下一项$/ }).click();
      await expect(page.getByRole('dialog').getByRole('img', { name: '第二张.svg', exact: true })).toBeVisible();
      expect(fixture.requests).toHaveLength(2);
      expect(fixture.requests[1].body.expected_revisions).toEqual({ item_1: 1 });
      expect(fixture.requests[1].body.operation_id).not.toBe(fixture.requests[0].body.operation_id);
      expect(fixture.requests[1].body.request_id).not.toBe(fixture.requests[0].body.request_id);
      expect(fixture.rows[0].disposition_revision).toBe(2);
      expect(fixture.unexpected).toEqual([]);
    } finally { await fixture.evidence(testInfo); }
  });

  test('two windows reject the stale revision and retain the newer human B against old suggestion A', async ({ context, page }, testInfo) => {
    const fixture = await classificationProtocol(context);
    const second = await context.newPage();
    try {
      await openV2Results(page); await openV2Results(second);
      await openV2Item(page); await openV2Item(second);
      await chooseV2Label(page, '类别 B');
      await page.getByRole('textbox', { name: '人工处置说明' }).fill('窗口一人工确认 B');
      await second.getByRole('textbox', { name: '人工处置说明' }).fill('窗口二仍见旧建议 A');
      const staleReads = fixture.resultsReads(second);
      await page.getByRole('button', { name: '保存并下一项', exact: true }).click();
      await expect(page.getByRole('dialog').getByRole('img', { name: '第二张.svg', exact: true })).toBeVisible();
      await second.getByRole('button', { name: '保存并下一项', exact: true }).click();
      await expect(second.getByText('处置记录已变化，请刷新后重试', { exact: true })).toBeVisible();
      await expect(second.getByRole('textbox', { name: '人工处置说明' })).toHaveValue('窗口二仍见旧建议 A');
      await expect(second.getByRole('button', { name: '保存并下一项', exact: true })).toBeEnabled();
      expect(fixture.requests).toHaveLength(2);
      expect(fixture.requests.map(event => event.body.expected_revisions)).toEqual([{ item_1: 0 }, { item_1: 0 }]);
      expect(fixture.requests.map(event => event.outcome)).toEqual(['committed', 'revision_conflict']);
      expect(fixture.requests[0].body.request_id).not.toBe(fixture.requests[1].body.request_id);
      expect(fixture.resultsReads(second)).toBe(staleReads);
      expect(fixture.rows[0].human_label_id).toBe('class_b');
      expect(fixture.rows[0].disposition_note).toBe('窗口一人工确认 B');
      expect(fixture.rows[0].disposition_revision).toBe(1);
      expect(fixture.rows[0].prediction.label_id).toBe('class_a');
      expect(await pendingV2Requests(second)).toEqual([]);
      expect(fixture.unexpected).toEqual([]);
      await second.screenshot({ path: testInfo.outputPath('two-window-stale-conflict.png'), fullPage: true });
    } finally { await fixture.evidence(testInfo); await second.close(); }
  });
});
