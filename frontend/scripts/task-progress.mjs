import { chromium, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Run after npm run build. API fixtures keep this check away from user projects
// and GPU workers; the browser exercises the real built application and CSS.
const root = fileURLToPath(new URL('../dist/', import.meta.url));
const output = fileURLToPath(new URL('../../build/task-progress/', import.meta.url));
await fs.mkdir(output, { recursive: true });
const server = http.createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    const file = path.resolve(root, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!file.startsWith(root)) throw Error('Invalid path');
    const content = await fs.readFile(file);
    response.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream');
    response.end(content);
  } catch {
    response.writeHead(404).end();
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const project = { id: 'progress-test', name: '任务进度验证', created: '', updated: '' };
const images = ['当前图片.png', '其他图片.png'].map((name, index) => ({
  id: `image-${index}`, name, project_id: project.id,
  active_version: `version-${index}`, selected_result: null,
}));
const versions = images.map(image => ({ id: image.active_version, image_id: image.id,
  parent_id: null, width: 640, height: 480, operations: '[]', created: '' }));
const task = (id, image, status) => ({ id, image_id: image.id, version_id: image.active_version,
  engine: 'ppocr', status, phase: status === 'running' ? '正在识别文字' : '等待识别',
  error: null, result_id: null, created: '' });
const tasks = [task('current-running', images[0], 'running'), task('current-queued', images[0], 'queued'),
  ...Array.from({ length: 125 }, (_, index) => task(`other-${index}`, images[1], 'queued'))];
const checks = [], errors = [], unexpectedRequests = [];
let browser;
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true,
    args: ['--disable-gpu', '--disable-gpu-compositing'] });
  for (const viewport of [
    { width: 1920, height: 1080 }, { width: 1366, height: 768 },
    { width: 1024, height: 600 }, { width: 768, height: 768 },
    { width: 390, height: 844 },
  ]) {
    const context = await browser.newContext({ viewport });
    await context.addInitScript(() => localStorage.setItem('ocr-ui-sidebar', 'false'));
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (route.request().method() !== 'GET') {
        unexpectedRequests.push(`${route.request().method()} ${url.pathname}`);
        return route.abort();
      }
      if (url.pathname === '/api/state') return route.fulfill({ json: { projects: [project], engines: {
        ppocr: { name: 'PP-OCRv6', capabilities: { tables: false, confidence: true } },
      } } });
      if (url.pathname === '/api/projects/progress-test') return route.fulfill({ json: {
        project, images, versions, tasks, queue: { healthy: true, loaded: true, task_id: tasks[0].id, engine: 'ppocr' },
      } });
      if (url.pathname === '/api/fusion/policies') return route.fulfill({ json: { policies: {} } });
      if (/^\/api\/versions\/version-[01]\/(image|thumbnail)$/.test(url.pathname)) return route.fulfill({
        contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"><rect width="640" height="480" fill="white"/><text x="100" y="120">OCR progress test</text></svg>',
      });
      unexpectedRequests.push(url.pathname);
      return route.abort();
    });
    try {
      await page.goto(base);
      const progress = page.getByRole('button', { name: '查看任务进度', exact: true });
      const queue = page.getByRole('region', { name: '任务队列', exact: true });
      const current = page.locator('[data-task-id="current-running"]');
      const confirmLocated = async () => {
        await expect(queue).toBeInViewport({ ratio: 1 });
        await expect(current).toBeFocused();
        await expect(current).toBeInViewport({ ratio: 0.95 });
        const placement = await page.evaluate(() => {
          const row = document.querySelector('[data-task-id="current-running"]').getBoundingClientRect();
          const list = document.querySelector('.task-rows').getBoundingClientRect();
          return { inside: row.top >= list.top - 1 && row.bottom <= list.bottom + 1,
            workspaceScroll: document.querySelector('.workspace-grid').scrollTop,
            bodyOverflow: document.documentElement.scrollWidth > innerWidth };
        });
        expect(placement).toEqual({ inside: true, workspaceScroll: 0, bodyOverflow: false });
      };

      // The queue can already be open from starting recognition. Its first 60
      // entries may all belong to another image; every progress click must act.
      await page.locator('.workspace-queue-toggle').click();
      await expect(queue.locator('.task-row')).toHaveCount(60);
      await expect(current).toHaveCount(0);
      await progress.click();
      await confirmLocated();
      await expect(progress).toHaveAttribute('aria-expanded', 'true');
      await expect(progress).toHaveAttribute('aria-controls', 'task-queue');

      await page.locator('.task-rows').evaluate(element => { element.scrollTop = 0; });
      await progress.click();
      await confirmLocated();
      await page.screenshot({ path: path.join(output, `located-${viewport.width}-${viewport.height}.png`) });

      await page.getByRole('button', { name: '收起任务队列', exact: true }).click();
      await expect(queue).toHaveCount(0);
      await expect(progress).toHaveAttribute('aria-expanded', 'false');
      await progress.focus();
      await page.keyboard.press('Enter');
      await confirmLocated();
      await expect(page.getByRole('button', { name: '展开资料栏', exact: true })).toBeVisible();

      // With no running task on this image, progress goes to its queued task.
      tasks[0].status = 'succeeded';
      await page.reload();
      await progress.click();
      await expect(page.locator('[data-task-id="current-queued"]')).toBeFocused();
      tasks[0].status = 'running';

      checks.push({ viewport, passed: true, checks: ['already open', 'beyond first 60',
        'repeat click after scrolling away', 'close and reopen by keyboard', 'sidebar hidden', 'queued task fallback'] });
      console.log(`PASS task progress ${viewport.width}x${viewport.height}`);
    } catch (error) {
      await page.screenshot({ path: path.join(output, `failure-${viewport.width}-${viewport.height}.png`) });
      throw error;
    } finally {
      await context.close();
    }
  }
  expect(errors).toEqual([]);
  expect(unexpectedRequests).toEqual([]);
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ checks, errors, unexpectedRequests }, null, 2));
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
