import { chromium, expect as playwrightExpect } from '../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

const out = path.resolve(process.argv[2]);
const expect = playwrightExpect.configure({ timeout: 15000 });
const seed = JSON.parse(await fs.readFile(path.join(out, 'seed.json'), 'utf8'));
const checks = [], errors = [];
const api = async (url, method = 'GET', body) => {
  const r = await fetch(seed.base + '/api' + url, { method, headers: { Authorization: 'Bearer ' + seed.token,
    'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) throw Error(await r.text());
  return r.json();
};
const record = name => { checks.push(name); console.log(name); };
const browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--disable-gpu', '--disable-gpu-compositing'] });
const context = await browser.newContext({ viewport: { width: 1366, height: 900 } });
const page = await context.newPage();
page.on('pageerror', error => errors.push(String(error)));
const review = page.getByRole('region', { name: '多模态二轮审校', exact: true });
const dialog = page.getByRole('dialog', { name: '配置外部 API' });
try {
  await page.goto(seed.base + '/#token=' + seed.token);
  await expect(page.getByLabel('当前识别结果', { exact: true })).toHaveValue(seed.result);
  await page.getByRole('tab', { name: '视觉审校', exact: true }).click();
  expect((await api('/audit/external-fixture')).requests).toBe(0);
  await review.getByRole('button', { name: '配置外部 API', exact: true }).click();
  await expect(dialog.getByLabel('协议', { exact: true })).toBeEnabled();
  await dialog.getByLabel('API URL', { exact: true }).fill(seed.provider);
  await dialog.getByLabel('API Key', { exact: true }).fill(seed.key);
  await expect(dialog.getByLabel('API Key', { exact: true })).toHaveAttribute('type', 'password');
  await dialog.getByRole('button', { name: '获取模型', exact: true }).click();
  await expect(dialog.getByText(/已获取 2 个模型/)).toBeVisible();
  const combo = dialog.getByRole('combobox', { name: '模型 ID', exact: true });
  await combo.fill('vision-b');
  await combo.press('ArrowDown');
  await page.getByRole('option', { name: 'vision-b', exact: true }).click();
  await combo.fill('vision-a');
  await combo.press('ArrowDown');
  await page.getByRole('option', { name: 'vision-a', exact: true }).click();
  await page.screenshot({ path: path.join(out, '01-configure.png'), animations: 'disabled' });
  await dialog.getByRole('button', { name: '测试并保存', exact: true }).click();
  await expect(dialog).toHaveCount(0, { timeout: 15000 });
  await expect(review.getByLabel('审校模型', { exact: true })).toHaveValue(/^external:/);
  await expect(review.getByText(/发起审校会发送页面上下文/)).toBeVisible();
  record('OpenAI connection, searchable models, visual test, and auto-selection');

  await review.getByRole('button', { name: '复核本页', exact: true }).click();
  await expect(review.getByText('Amount 001.05', { exact: true })).toBeVisible({ timeout: 15000 });
  expect((await api(`/results/${seed.result}`)).edited.text).toBe('Amount 001.00');
  await review.getByRole('button', { name: '采用这条修改', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, '02-proposal.png'), animations: 'disabled' });
  await review.getByRole('button', { name: '采用这条修改', exact: true }).click();
  await expect.poll(async () => (await api(`/results/${seed.result}`)).edited.text).toBe('Amount 001.05');
  expect((await api(`/results/${seed.result}`)).original.text).toBe('Amount 001.00');
  record('Review-only mode generates a proposal and requires human adoption');

  await review.getByRole('button', { name: '配置外部 API', exact: true }).click();
  await expect(dialog.getByLabel('API Key', { exact: true })).toHaveValue('');
  await expect(dialog.getByText('已保存，留空保留现有 Key。', { exact: true })).toBeVisible();
  const saved = await api('/multimodal/external');
  await api('/audit/external-fixture', 'POST', { mode: 'status:401' });
  await dialog.getByRole('button', { name: '测试并保存', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('401');
  expect((await api('/multimodal/external')).revision).toBe(saved.revision);
  record('Masked stored key and failed connection test preserve prior configuration');

  await api('/audit/external-fixture', 'POST', { mode: 'normal' });
  await dialog.getByLabel('协议', { exact: true }).selectOption('anthropic');
  await expect(dialog.getByRole('button', { name: '获取模型', exact: true })).toBeDisabled();
  await dialog.getByLabel('API Key', { exact: true }).fill(seed.key);
  await dialog.getByRole('button', { name: '获取模型', exact: true }).click();
  await expect(dialog.getByText(/已获取 2 个模型/)).toBeVisible();
  await dialog.getByRole('combobox', { name: '模型 ID', exact: true }).fill('manual-vision-id');
  await dialog.getByRole('button', { name: '测试并保存', exact: true }).click();
  await expect(dialog).toHaveCount(0, { timeout: 15000 });
  expect((await api('/multimodal/external')).protocol).toBe('anthropic');
  await review.getByRole('button', { name: '复核本页', exact: true }).click();
  await expect.poll(async () => (await api(`/results/${seed.result}/multimodal`)).requests.filter(t => t.status === 'succeeded').length, { timeout: 15000 }).toBe(2);
  record('Anthropic pagination, fresh key requirement, manual model ID and review');

  const beforeReload = (await api('/audit/external-fixture')).requests;
  await page.reload();
  await page.getByRole('tab', { name: '视觉审校', exact: true }).click();
  await expect(review.getByLabel('审校模型', { exact: true })).toHaveValue(/^external:/);
  expect((await api('/audit/external-fixture')).requests).toBe(beforeReload);
  await review.getByRole('button', { name: '配置外部 API', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '清除连接', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: '清除连接', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect((await api('/multimodal/external')).configured).toBe(false);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain(seed.key);
  record('Reload makes no provider calls; clearing connection removes it and browser storage contains no key');
  expect(errors).toEqual([]);
  await fs.writeFile(path.join(out, 'receipt.json'), JSON.stringify({ passed: true, checks, browserErrors: errors,
    cloudApiTested: false, provider: 'local protocol fixture' }, null, 2));
} catch (error) {
  await page.screenshot({ path: path.join(out, 'failure.png'), animations: 'disabled' });
  await fs.writeFile(path.join(out, 'failure.txt'), String(error) + '\n' + await page.locator('body').innerText());
  await fs.writeFile(path.join(out, 'failure-dom.html'), await page.content());
  console.log('Dialogs:', await page.getByRole('dialog').count(), await page.getByRole('dialog').allTextContents());
  throw error;
} finally {
  await context.close();
  await browser.close();
}
