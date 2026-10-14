import { expect, test } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { readFileSync } from 'node:fs';

const projectName = `E2E_${Date.now()}`;
let projectId: string | undefined;

test.beforeEach(async ({ page, request }) => {
  const projects = await (await request.get('/api/v1/projects')).json();
  const demo = projects.find((p: { name: string }) => p.name === '流程验证 · 合成图形');
  await page.addInitScript(id => { if (!localStorage.getItem('prism-project')) localStorage.setItem('prism-project', id); }, demo.id);
});

test.afterAll(() => {
  if (!projectId) return;
  const repo = path.resolve('../..');
  const script = `
import sys
from pathlib import Path
from image_platform.settings import Settings
from image_platform.store import Store
s=Store(Settings.from_env())
project=s.project(sys.argv[1])
assert project['name'].startswith('E2E_')
with s.connect(write=True) as c:
 assets=list(c.execute('SELECT id,path FROM assets WHERE project_id=?',(project['id'],)))
 for a in assets:
  c.execute('DELETE FROM annotations WHERE asset_id=?',(a['id'],))
 c.execute('DELETE FROM assets WHERE project_id=?',(project['id'],))
 c.execute('DELETE FROM projects WHERE id=?',(project['id'],))
for a in assets:
 p=Path(a['path']).resolve()
 assert p.is_relative_to((s.settings.data_dir/'uploads'/project['id']).resolve())
 p.unlink(missing_ok=True)
`;
  const result = spawnSync(process.env.ICP_PYTHON || 'python', ['-c', script, projectId], {
    cwd: repo, env: { ...process.env, PYTHONPATH: path.join(repo, 'src') }, encoding: 'utf8',
  });
  expect(result.status, result.stderr).toBe(0);
});

test('overview and responsive navigation use real project data', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '流程验证 · 合成图形' })).toBeVisible();
  await expect(page.getByText('60', { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: '../../reports/screenshots/overview-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.sidebar')).toHaveCSS('width', '72px');
  await expect(page.getByRole('heading', { name: '流程验证 · 合成图形' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: '../../reports/screenshots/overview-mobile.png', fullPage: true });
});

test('asset search and gallery show real images', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('menuitem', { name: '图像与标注' }).click();
  await expect(page.locator('.asset-card')).toHaveCount(24);
  await page.getByRole('searchbox', { name: '搜索图片' }).fill('triangle');
  await page.getByRole('searchbox', { name: '搜索图片' }).press('Enter');
  await expect(page.locator('.asset-card')).toHaveCount(20);
  const image = page.locator('.asset-card img').first();
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  await page.screenshot({ path: '../../reports/screenshots/assets.png', fullPage: true });
});

test('create project, upload, label and persist after reload', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /新建项目/ }).click();
  await page.getByRole('textbox', { name: /项目名称/ }).fill(projectName);
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  projectId = (await (await created).json()).id;
  await expect(page.getByRole('heading', { name: '图像与标注', exact: true })).toBeVisible();
  await page.getByLabel('上传图片文件').setInputFiles({
    name: '浏览器验收.png', mimeType: 'image/png',
    buffer: readFileSync(path.resolve('e2e/fixtures/sample.png')),
  });
  await expect(page.locator('.asset-card')).toHaveCount(1);
  await page.getByRole('checkbox', { name: '选择 浏览器验收.png' }).check();
  await page.getByRole('combobox', { name: '设置选中图片类别' }).click();
  await page.getByText('类别 A', { exact: true }).last().click();
  await page.getByRole('button', { name: /保存标注/ }).click();
  await expect(page.locator('.asset-caption').getByText('类别 A', { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole('menuitem', { name: '图像与标注' }).click();
  await expect(page.locator('.asset-caption').getByText('类别 A', { exact: true })).toBeVisible();
});

test('training, registry, jobs and runtime surfaces render without JS errors', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  for (const name of ['训练与实验', '模型库', '分类与复核', '运行环境']) {
    await page.getByRole('menuitem', { name }).click();
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  }
  await expect(page.getByText('依赖版本', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
