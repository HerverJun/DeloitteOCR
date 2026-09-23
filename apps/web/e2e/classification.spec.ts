import { expect, test } from '@playwright/test';
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
