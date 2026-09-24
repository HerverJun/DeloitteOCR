import { chromium, expect as baseExpect } from '../../frontend/node_modules/@playwright/test/index.mjs';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

const out = path.resolve(process.argv[2]);
const seed = JSON.parse(await fs.readFile(path.join(out, 'seed.json'), 'utf8'));
const expect = baseExpect.configure({ timeout: 20000 });
const errors = [];
const browser = await chromium.launch({ channel: 'msedge', headless: true,
  args: ['--disable-gpu', '--disable-gpu-compositing'] });
let result = { status: 'failed', errors };
try {
  for (const [width, height, filename] of [[1366, 900, 'gpu-1366.png'], [1024, 768, 'gpu-1024.png']]) {
    const context = await browser.newContext({ viewport: { width, height }, acceptDownloads: true });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(seed.base + '/#token=' + seed.token);
    const launch = page.getByRole('button', { name: '文档助手', exact: true });
    await expect(launch).toBeVisible();
    await launch.click();
    const panel = page.getByRole('complementary', { name: '文档助手' });
    await expect(panel.getByRole('combobox', { name: '历史会话' })).toHaveValue(seed.session);
    await expect(panel.locator('.agent-run-state')).toContainText('已结束');
    await expect(panel.locator('.agent-run-state')).not.toContainText('有未覆盖内容');
    await panel.getByText('范围、审校与预算', { exact: true }).click();
    await expect(panel).toContainText('本轮 20 页 · 完整覆盖 20 · 等待 0 · 失败 0 · 未覆盖 0');
    const artifact = panel.getByText('20 份结果', { exact: false });
    await expect(artifact).toBeVisible();
    await expect(panel.getByRole('button', { name: '下载导出文件', exact: true })).toBeEnabled();
    await expect(panel).toContainText('已根据工具实际结果核对批次');
    await page.screenshot({ path: path.join(out, filename), animations: 'disabled' });
    if (width === 1366) {
      const downloadEvent = page.waitForEvent('download');
      await panel.getByRole('button', { name: '下载导出文件', exact: true }).click();
      const download = await downloadEvent;
      await download.saveAs(path.join(out, 'download.xlsx'));
    }
    await context.close();
  }
  const buffer = await fs.readFile(path.join(out, 'download.xlsx'));
  const hash = createHash('sha256').update(buffer).digest('hex');
  expect(hash).toBe(seed.expected_sha256);
  expect(errors).toEqual([]);
  result = { status: 'pass', viewports: ['1366x900', '1024x768'],
    session: seed.session, run: seed.run, artifact: seed.artifact,
    downloaded_bytes: buffer.length, download_sha256: hash, errors };
} catch (error) {
  result.error = String(error);
  throw error;
} finally {
  await fs.writeFile(path.join(out, 'browser-result.json'), JSON.stringify(result, null, 2));
  await browser.close();
}
