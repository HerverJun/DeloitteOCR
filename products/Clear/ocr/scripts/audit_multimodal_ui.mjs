import { chromium, expect } from '../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

const out = path.resolve(process.argv[2]);
const seed = JSON.parse(await fs.readFile(path.join(out, 'seed.json'), 'utf8'));
const checks = [], errors = [], downloads = [];
const api = async (url, method = 'GET', body) => {
  const response = await fetch(seed.base + '/api' + url, { method, headers: { Authorization: 'Bearer ' + seed.token, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw Error(await response.text());
  return response.json();
};
const record = name => { checks.push({ name, passed: true }); console.log(name); };
const view = () => api(`/results/${seed.result}/multimodal`);
const browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--disable-gpu', '--disable-gpu-compositing'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
const page = await context.newPage();
page.on('pageerror', error => errors.push(String(error)));
const review = page.getByRole('region', { name: '多模态二轮审校', exact: true });
const openReview = () => page.getByRole('tab', { name: '视觉审校', exact: true }).click();
const waitCount = async count => expect.poll(async () => (await view()).requests.filter(item => item.status === 'succeeded').length, { timeout: 15000 }).toBe(count);
try {
  await page.goto(seed.base + '/#token=' + seed.token);
  await expect(page.getByLabel('当前识别结果', { exact: true })).toHaveValue(seed.result);
  await openReview();
  await expect(review.getByRole('button', { name: '复核本页', exact: true })).toBeEnabled();
  await expect(review.getByRole('button', { name: '复核选中内容', exact: true })).toBeDisabled();
  await review.getByText('查看不可用模型', { exact: true }).click();
  await expect(review.getByText('不可用模型（验收夹具）：验收夹具：模型资源尚未就绪', { exact: true })).toBeVisible();
  await api('/audit/reviewer', 'POST', { available: false });
  await review.getByRole('button', { name: '刷新模型', exact: true }).click();
  await expect(review.getByRole('button', { name: '复核本页', exact: true })).toBeDisabled();
  await page.screenshot({ path: path.join(out, '01-unavailable-model.png'), fullPage: true, animations: 'disabled' });
  await api('/audit/reviewer', 'POST', { available: true });
  await review.getByRole('button', { name: '刷新模型', exact: true }).click();
  await expect(review.getByRole('button', { name: '复核本页', exact: true })).toBeEnabled();
  record('available/unavailable model readiness is explicit and prevents unsupported submissions');

  await page.getByRole('tab', { name: '文字', exact: true }).click();
  const textbox = page.getByRole('textbox', { name: '校对文字', exact: true });
  await textbox.focus();
  await textbox.evaluate(element => { const start = element.value.indexOf('Amount 001.00'); element.setSelectionRange(start, start + 'Amount 001.00'.length); });
  await page.evaluate(() => document.dispatchEvent(new Event('selectionchange')));
  await expect(page.getByText(/^已选中：文字第/)).toBeVisible();
  await openReview();
  await expect(review.getByRole('button', { name: '复核选中内容', exact: true })).toBeEnabled();
  await review.getByRole('button', { name: '复核选中内容', exact: true }).click();
  await waitCount(1);
  await expect(review.getByText('Amount 001.05', { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(out, '02-text-correction.png'), fullPage: true, animations: 'disabled' });
  const decisionBodies = [];
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().includes('/multimodal/') && request.url().endsWith('/decision')) decisionBodies.push(request.postDataJSON());
  });
  await page.route(`**/api/results/${seed.result}/multimodal/*/decision`, async route => {
    await route.fetch();
    await route.abort('failed');
  }, { times: 1 });
  await review.getByRole('button', { name: '采用这条修改', exact: true }).click();
  await expect.poll(async () => (await api(`/results/${seed.result}`)).edited.text).toContain('Amount 001.05');
  await expect(review.getByRole('button', { name: '重试上次请求', exact: true })).toBeVisible();
  await review.getByRole('button', { name: '重试上次请求', exact: true }).click();
  await expect(review.getByRole('button', { name: '重试上次请求', exact: true })).toHaveCount(0);
  await expect(page.locator('.toast.error')).toHaveCount(0);
  expect(decisionBodies.length).toBe(2);
  expect(decisionBodies[1]).toEqual(decisionBodies[0]);
  expect((await api(`/results/${seed.result}`)).revision).toBe(1);
  expect((await api(`/results/${seed.result}`)).original.text).toContain('Amount 001.00');
  record('text selection survives tab navigation; literal acceptance edits only the durable draft');
  record('lost decision response replays the exact request ID/body without a duplicate revision');

  await page.getByRole('tab', { name: /^表格/ }).click();
  await page.getByRole('textbox', { name: '第 2 行第 2 列', exact: true }).click();
  await openReview();
  await expect(review.getByText('当前选中：表 1 · 第 2 行 / 第 2 列', { exact: true })).toBeVisible();
  await review.getByRole('button', { name: '复核选中内容', exact: true }).click();
  await waitCount(2);
  await expect(review.getByText('001.05', { exact: true })).toBeVisible();
  await review.getByRole('button', { name: '采用这条修改', exact: true }).click();
  await expect.poll(async () => (await api(`/results/${seed.result}`)).edited.tables[0].cells.at(-1).text).toBe('001.05');
  record('cell review uses the selected table coordinate and preserves leading-zero strings');

  await review.getByRole('button', { name: '复核本页', exact: true }).click();
  await waitCount(3);
  let state = await view();
  const fullTask = state.requests.find(item => item.scope === 'page');
  expect(fullTask).toBeTruthy();
  const correction = state.proposals.find(item => item.task_id === fullTask.task_id && item.before === 'Name Revenve');
  await review.getByLabel('显示', { exact: true }).selectOption('all');
  await review.getByLabel('选择审校建议', { exact: true }).selectOption(correction.id);
  await expect(review.getByText('Name Revenue', { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(out, '03-page-suggestions.png'), fullPage: true, animations: 'disabled' });
  await review.locator('.multimodal-comparison').evaluate(element => element.scrollIntoView({ block: 'start' }));
  await page.screenshot({ path: path.join(out, '03b-evidence-and-decision.png'), fullPage: true, animations: 'disabled' });
  await review.getByRole('button', { name: '采用这条修改', exact: true }).click();
  await expect.poll(async () => (await api(`/results/${seed.result}`)).edited.text).toContain('Name Revenue');
  state = await view();
  const keep = state.proposals.find(item => item.task_id === fullTask.task_id && item.decision === 'keep' && item.state === 'pending');
  await review.getByLabel('选择审校建议', { exact: true }).selectOption(keep.id);
  await review.getByRole('button', { name: '记录保留原文', exact: true }).click();
  await expect.poll(async () => (await view()).proposals.find(item => item.id === keep.id)?.state).toBe('accepted');
  const uncertain = (await view()).proposals.find(item => item.task_id === fullTask.task_id && item.decision === 'uncertain');
  await review.getByLabel('选择审校建议', { exact: true }).selectOption(uncertain.id);
  await expect(review.getByRole('button', { name: '采用这条修改', exact: true })).toHaveCount(0);
  await review.getByRole('button', { name: '人工标记存疑', exact: true }).click();
  await expect.poll(async () => (await view()).proposals.find(item => item.id === uncertain.id)?.state).toBe('question');
  expect((await api(`/projects/${seed.project}`)).images[0].review_status).not.toBe('confirmed');
  expect(await page.getByLabel('当前识别结果', { exact: true }).locator('option').count()).toBe(1);
  record('page review supports sequential replace/keep/uncertain decisions without auto-confirming the page or creating OCR candidates');

  await page.getByRole('button', { name: '撤销', exact: true }).click();
  await expect.poll(async () => (await view()).proposals.every(item => item.state === 'stale')).toBe(true);
  await review.getByLabel('显示', { exact: true }).selectOption('all');
  await review.getByLabel('选择审校建议', { exact: true }).selectOption(correction.id);
  await expect(review.getByText('内容或图片已变化，这条建议无法采用。请根据当前内容重新发起审校。', { exact: true })).toBeVisible();
  await expect(review.getByRole('button', { name: '采用这条修改', exact: true })).toBeDisabled();
  await page.screenshot({ path: path.join(out, '04-stale-proposal.png'), fullPage: true, animations: 'disabled' });
  record('editor undo invalidates old visual proposals and disables stale acceptance');

  for (const format of ['json', 'md', 'xlsx']) {
    await review.getByLabel('校验清单', { exact: true }).selectOption(format);
    const event = page.waitForEvent('download');
    await review.getByRole('button', { name: '导出校验清单', exact: true }).click();
    const file = await event;
    const target = path.join(out, 'report.' + format);
    await file.saveAs(target);
    expect((await fs.stat(target)).size).toBeGreaterThan(200);
    downloads.push({ format, filename: file.suggestedFilename(), saved: target });
  }
  const report = JSON.parse(await fs.readFile(path.join(out, 'report.json'), 'utf8'));
  expect(report.result_id).toBe(seed.result);
  expect(report.automatic_adoption).toBe(false);
  expect(report.proposals.some(item => item.state === 'stale')).toBe(true);
  record('authenticated JSON, Markdown and XLSX downloads contain traceable review records');

  await api('/audit/reviewer', 'POST', { hold: true });
  await review.getByRole('button', { name: '复核本页', exact: true }).click();
  await expect(review.getByRole('button', { name: '取消审校', exact: true })).toBeVisible();
  await review.getByRole('button', { name: '取消审校', exact: true }).click();
  await expect.poll(async () => (await view()).requests.some(item => item.status === 'cancelled')).toBe(true);
  await api('/audit/reviewer', 'POST', { hold: false });
  const taskDetails = review.locator('.multimodal-tasks');
  await expect(taskDetails).toContainText('已取消');
  if ((await taskDetails.getAttribute('open')) === null) await taskDetails.locator('summary').click();
  await review.getByRole('button', { name: '重试任务', exact: true }).click();
  await waitCount(4);
  await expect(review.getByRole('button', { name: '取消审校', exact: true })).toHaveCount(0);
  record('queued review cancellation and original-snapshot retry complete without spawning GPU inference');

  await review.getByLabel('显示', { exact: true }).selectOption('pending');
  await page.setViewportSize({ width: 1100, height: 760 });
  await review.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, '05-small-laptop.png'), fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.setViewportSize({ width: 860, height: 740 });
  await page.screenshot({ path: path.join(out, '06-narrow-layout.png'), fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  record('small-laptop and narrow viewport have no horizontal document overflow');
  expect(errors).toEqual([]);
} catch (error) {
  errors.push(error.stack || String(error));
  await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true, animations: 'disabled' }).catch(() => {});
  process.exitCode = 1;
} finally {
  await browser.close();
  await fs.writeFile(path.join(out, 'ui-report.json'), JSON.stringify({ passed: errors.length === 0, checks, errors, downloads, scope: seed.scope, browser_closed: true }, null, 2));
}
