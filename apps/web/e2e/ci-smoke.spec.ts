import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';

test('clean install serves a new project, upload and annotation', async ({ page, request }) => {
  const projectName = `CI_${Date.now()}`;
  await page.goto('/');
  await page.getByRole('button', { name: /创建第一个项目/ }).click();
  await page.getByRole('textbox', { name: '项目名称' }).fill(projectName);
  await page.getByRole('textbox', { name: '类别定义' }).fill('a,类别 A\nb,类别 B');
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  const project = await (await created).json();
  await expect(page.getByRole('heading', { name: '图像与标注' })).toBeVisible();
  await page.getByLabel('上传图片文件').setInputFiles({
    name: 'sample.png', mimeType: 'image/png', buffer: readFileSync(path.resolve('e2e/fixtures/sample.png')),
  });
  await expect(page.locator('.asset-card')).toHaveCount(1);
  await page.getByRole('checkbox', { name: '选择 sample.png' }).check();
  await page.getByRole('combobox', { name: '设置选中图片类别' }).click();
  await page.getByText('类别 A', { exact: true }).last().click();
  await page.getByRole('button', { name: /保存标注/ }).click();
  const assets = await (await request.get(`/api/v1/projects/${project.id}/assets`)).json();
  expect(assets.items).toHaveLength(1);
  expect(assets.items[0].label_id).toBe('a');
  await page.reload();
  await page.getByRole('menuitem', { name: '图像与标注' }).click();
  await expect(page.locator('.asset-caption').getByText('类别 A', { exact: true })).toBeVisible();
});
