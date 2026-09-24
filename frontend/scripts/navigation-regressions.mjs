import { chromium, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const baseline = process.argv.includes('--baseline');
const root = fileURLToPath(new URL('../dist/', import.meta.url));
const output = fileURLToPath(new URL('../../build/navigation-audit/', import.meta.url));
await fs.mkdir(output, { recursive: true });
const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    const file = path.resolve(root, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!file.startsWith(root)) throw Error('Invalid path');
    res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream');
    res.end(await fs.readFile(file));
  } catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const project = { id: 'navigation-test', name: '导航回归', created: '', updated: '' };
const doc = { id: 'doc', project_id: project.id, name: '导航验证.pdf', kind: 'pdf', page_count: 2, status: 'ready' };
const images = [1, 2].map(n => ({ id: `image-${n}`, name: `导航验证 · ${n}`, project_id: project.id,
  active_version: `version-${n}`, selected_result: `result-${n}`, document_id: doc.id, document_kind: 'pdf', page_number: n }));
const versions = images.map(image => ({ id: image.active_version, image_id: image.id, parent_id: null,
  width: 640, height: 480, operations: '[]', created: '' }));
const pages = images.map(image => ({ id: `page-${image.page_number}`, document_id: doc.id,
  page_number: image.page_number, image_id: image.id, active_version: image.active_version, status: 'processed', render_dpi: 150 }));
const polygon = [[80, 100], [400, 100], [400, 150], [80, 150]];
const table = { rows: 1, columns: 2, cells: [0, 1].map(column => ({ row: 0, column, row_span: 1,
  column_span: 1, text: `单元格 ${column}`, confidence: 0.7, polygon })) };
const results = Object.fromEntries(images.map(image => [image.selected_result, { id: image.selected_result,
  task_id: `task-${image.page_number}`, revision: 0, cursor: 0, can_undo: false, can_redo: false,
  edited: { text: `第 ${image.page_number} 页正文`, tables: [table] },
  original: { origin: 'document', engine: 'ppocr', text: `第 ${image.page_number} 页正文`, tables: [table],
    blocks: [{ kind: 'text', text: '待核对文字', confidence: 0.5, polygon }], image: { width: 640, height: 480 },
    project_image_version: image.active_version, load_seconds: 0 } }]));
const tasks = images.map(image => ({ id: `task-${image.page_number}`, image_id: image.id,
  version_id: image.active_version, result_id: image.selected_result, engine: 'ppocr', status: 'succeeded', phase: '已完成', error: null, created: '' }));
const structures = ['a', 'b', 'c'].map((letter, index) => ({ id: `structure-${letter}`, basis: 'fixture', revision: 0,
  version_id: 'version-1', state: index === 2 ? 'accepted' : 'pending', kind: 'replace_table',
  table_indices: [0], current_tables: [table], proposed_tables: [table], differences: [], conflicts: [],
  provider: `provider-${letter}`, can_apply: index < 2, polygon }));
const proposals = ['a', 'b'].map(letter => ({ id: `multimodal-${letter}`, task_id: 'review-task', target_id: letter,
  target: { kind: 'text', start: 0, end: 3 }, before: `原文 ${letter}`, after: `建议 ${letter}`, decision: 'replace',
  reason: '测试建议', state: 'pending', evidence: { level: 'region', polygon, version_id: 'version-1', reason: '测试区域' } }));
const reviewTasks = [...structures, ...proposals].map(item => ({ id: `queue-${item.id}`, page_id: 'page-1', page_number: 1,
  image_id: 'image-1', result_id: 'result-1', revision: 0, kind: item.id.startsWith('structure') ? 'structure' : 'multimodal',
  reason: item.id, category: 'text', state: item.state, proposal_ids: [item.id], proposal_id: item.id, target: { kind: 'page' } }));
const checks = [], errors = [], unexpected = [];
let browser;
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--disable-gpu', '--disable-gpu-compositing'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addInitScript(() => localStorage.setItem('ocr-ui-sidebar', 'true'));
  const page = await context.newPage();
  page.setDefaultTimeout(1800);
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), endpoint = url.pathname;
    let json;
    if (endpoint === '/api/state') json = { projects: [project], engines: { ppocr: { name: 'PP-OCRv6', capabilities: { tables: false, confidence: true } } } };
    else if (endpoint === '/api/projects/navigation-test') json = { project, images, versions, tasks, documents: [doc], queue: { healthy: true, loaded: false } };
    else if (endpoint === '/api/fusion/policies') json = { policies: {} };
    else if (endpoint === '/api/documents/doc/pages') { const start = Number(url.searchParams.get('offset') || 0); json = { pages: pages.slice(start, start + Number(url.searchParams.get('limit') || 30)) }; }
    else if (endpoint === '/api/documents/doc/review-queue') {
      const available = reviewTasks.filter(t => url.searchParams.get('state') === 'all' || t.state === 'pending');
      const offset = Number(url.searchParams.get('offset') || 0);
      json = { total: available.length, tasks: available.slice(offset, offset + 10), summary: { pages: 2, unprocessed_pages: 0, candidate_pages: 1 }, timing: { active_ms: 0 } };
    } else if (/^\/api\/results\/result-[12]$/.test(endpoint)) json = results[endpoint.split('/').at(-1)];
    else if (endpoint.endsWith('/structure') || endpoint.endsWith('/structure/check')) json = { revision: 0, adopted: true, candidates: [{ id: 'c', provider: 'test', tables: 1 }], proposals: structures };
    else if (endpoint.endsWith('/geometry/location')) json = { evidence: [] };
    else if (endpoint.endsWith('/document-conflicts')) json = { conflicts: [] };
    else if (endpoint === '/api/multimodal/models') json = { models: [], default_model: null };
    else if (endpoint.endsWith('/multimodal')) json = { revision: 0, requests: [], proposals, counts: { pending: 2 } };
    else if (/^\/api\/versions\/version-[12]\/(image|thumbnail)$/.test(endpoint)) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"><rect width="640" height="480" fill="white"/><text x="80" y="130">OCR navigation test</text></svg>' });
    else { unexpected.push(`${route.request().method()} ${endpoint}`); return route.abort(); }
    await route.fulfill({ json });
  });
  const review = page.getByRole('region', { name: '文档复核队列', exact: true });
  const open = id => review.getByRole('button', { name: new RegExp(id) }).click();
  const fresh = async (width = 1440, height = 1000) => {
    await page.setViewportSize({ width, height });
    await page.goto(base);
    await expect(page.getByLabel('当前识别结果')).toHaveValue('result-1');
    await expect(review.getByRole('button', { name: /structure-a/ })).toBeVisible();
  };
  const check = async (name, work) => {
    try { await work(); checks.push({ name, passed: true }); console.log('PASS ' + name); }
    catch (error) {
      checks.push({ name, passed: false, error: error.message }); console.log('FAIL ' + name);
      await page.screenshot({ path: path.join(output, `${baseline ? 'before' : 'after'}-${checks.length}.png`) });
    }
  };
  await check('next review advances without requiring a decision', async () => {
    await fresh(); await review.getByRole('button', { name: '下一处', exact: true }).click();
    await expect(page.getByLabel('结构建议', { exact: true })).toHaveValue('structure-a');
    await review.getByRole('button', { name: '下一处', exact: true }).click();
    await expect(page.getByLabel('结构建议', { exact: true })).toHaveValue('structure-b');
  });
  await check('repeat structure navigation restores the requested suggestion', async () => {
    await fresh(); await open('structure-b');
    await expect(page.getByLabel('结构建议', { exact: true })).toHaveValue('structure-b');
    await page.getByLabel('结构建议', { exact: true }).selectOption('structure-a');
    await open('structure-b');
    await expect(page.getByLabel('结构建议', { exact: true })).toHaveValue('structure-b');
  });
  await check('history navigation reveals a resolved structure suggestion', async () => {
    await fresh(); await page.getByLabel('文档复核筛选').selectOption('all'); await open('structure-c');
    await expect(page.getByLabel('结构建议', { exact: true })).toHaveValue('structure-c');
    await expect(page.getByLabel('显示已处理', { exact: true })).toBeChecked();
  });
  await check('repeat multimodal navigation restores the requested suggestion', async () => {
    await fresh(); await open('multimodal-b');
    await expect(page.getByLabel('选择审校建议', { exact: true })).toHaveValue('multimodal-b');
    await page.getByLabel('选择审校建议', { exact: true }).selectOption('multimodal-a');
    await open('multimodal-b');
    await expect(page.getByLabel('选择审校建议', { exact: true })).toHaveValue('multimodal-b');
  });
  await check('locate reveals the original image from an expanded editor', async () => {
    await fresh(); await open('structure-a');
    await page.getByRole('button', { name: '展开校对区', exact: true }).click();
    await page.getByRole('button', { name: '定位原图', exact: true }).click();
    await expect(page.getByRole('region', { name: '图片工作区', exact: true })).toBeVisible();
  });
  await check('locate reveals the original image in a short viewport', async () => {
    await fresh(1024, 600); await open('multimodal-a');
    await page.getByRole('button', { name: '收起资料栏', exact: true }).click();
    await page.getByRole('button', { name: '定位原图', exact: true }).click();
    await expect(page.getByRole('region', { name: '图片工作区', exact: true })).toBeVisible();
  });
  await check('manual binding reveals the image and its binding controls', async () => {
    await fresh(1024, 600); await page.getByRole('button', { name: '收起资料栏', exact: true }).click();
    await page.locator('.editable-grid textarea').first().focus();
    await page.getByRole('button', { name: '人工框选绑定', exact: true }).click();
    await expect(page.getByRole('region', { name: '图片工作区', exact: true })).toBeVisible();
    await expect(page.locator('.selection-bar')).toContainText('人工定位');
    await page.locator('.selection-bar').getByRole('button', { name: '取消', exact: true }).click();
    await page.getByRole('button', { name: '校对', exact: true }).click();
    await expect(page.locator('.editable-grid textarea').first()).toHaveValue('单元格 0');
  });
  await check('viewing a task result switches back from the original image', async () => {
    await fresh(1024, 600); await page.getByRole('button', { name: '收起资料栏', exact: true }).click();
    await page.getByRole('button', { name: '原图', exact: true }).click();
    await page.locator('.workspace-queue-toggle').click();
    await page.locator('.task-row').filter({ hasText: '导航验证 · 2' }).getByRole('button', { name: '查看', exact: true }).click();
    await expect(page.getByRole('region', { name: '识别与校对结果', exact: true })).toBeVisible();
    await expect(page.getByLabel('当前识别结果')).toHaveValue('result-2');
  });
  await check('next review crosses pages, wraps, and resumes after an item disappears', async () => {
    const extras = Array.from({ length: 8 }, (_, index) => ({ ...reviewTasks[0], id: `extra-${index}`, reason: `extra-${index}` }));
    reviewTasks.push(...extras);
    try {
      await fresh();
      await review.getByRole('button', { name: /extra-5/ }).click();
      await review.getByRole('button', { name: '下一处', exact: true }).click();
      await expect(review.getByRole('button', { name: /extra-6/ })).toHaveAttribute('aria-current', 'true');
      await review.getByRole('button', { name: '下一处', exact: true }).click();
      await expect(review.getByRole('button', { name: /extra-7/ })).toHaveAttribute('aria-current', 'true');
      await review.getByRole('button', { name: '下一处', exact: true }).click();
      await expect(review.getByRole('button', { name: /structure-a/ })).toHaveAttribute('aria-current', 'true');
      reviewTasks[0].state = 'accepted';
      await expect(review.getByRole('button', { name: /structure-a/ })).toHaveCount(0, { timeout: 6000 });
      await review.getByRole('button', { name: '下一处', exact: true }).click();
      await expect(review.getByRole('button', { name: /structure-b/ })).toHaveAttribute('aria-current', 'true');
    } finally { reviewTasks.splice(5); reviewTasks[0].state = 'pending'; }
  });
  await check('background location updates do not switch away from editing', async () => {
    await fresh(1024, 600); await page.getByRole('button', { name: '收起资料栏', exact: true }).click();
    const locationResponse = page.waitForResponse(response => response.url().endsWith('/geometry/location'));
    await page.locator('.editable-grid textarea').last().focus();
    await locationResponse;
    await expect(page.getByRole('region', { name: '识别与校对结果', exact: true })).toBeVisible();
    await expect(page.getByRole('region', { name: '图片工作区', exact: true })).toHaveCount(0);
  });
  await check('repeated locate and review navigation switch to the requested pane', async () => {
    await fresh(1024, 600); await open('multimodal-b');
    await page.getByRole('button', { name: '定位原图', exact: true }).click();
    await expect(page.getByRole('region', { name: '图片工作区', exact: true })).toBeVisible();
    await open('multimodal-b');
    await expect(page.getByLabel('选择审校建议', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '定位原图', exact: true }).click();
    await expect(page.getByRole('region', { name: '图片工作区', exact: true })).toBeVisible();
    await page.screenshot({ path: path.join(output, 'visible-original.png') });
  });
  await check('low-confidence text navigation reveals the original image', async () => {
    await fresh(1024, 600); await page.getByRole('button', { name: '收起资料栏', exact: true }).click();
    await page.getByRole('tab', { name: '文字', exact: true }).click();
    await page.locator('.text-blocks summary').click();
    await page.getByRole('button', { name: '下一处待核对', exact: true }).click();
    await expect(page.getByRole('region', { name: '图片工作区', exact: true })).toBeVisible();
    await expect(page.locator('.image-overlay polygon').first()).toHaveAttribute('stroke', '#386a12');
  });
  await check('refreshing handled suggestions does not replay old navigation', async () => {
    await fresh(); await open('structure-b');
    structures[1].state = 'accepted';
    try {
      await page.getByRole('button', { name: '检查已有候选', exact: true }).click();
      await expect(page.getByLabel('结构建议', { exact: true })).toHaveValue('structure-a');
      await expect(page.getByLabel('显示已处理', { exact: true })).not.toBeChecked();
    } finally { structures[1].state = 'pending'; }
  });
  await context.close();
  await fs.writeFile(path.join(output, baseline ? 'before.json' : 'after.json'), JSON.stringify({ scope: 'Built frontend, mocked APIs; no user project mutations or GPU inference', checks, errors, unexpected }, null, 2));
  expect(errors).toEqual([]); expect(unexpected).toEqual([]);
  if (!baseline) expect(checks.filter(c => !c.passed)).toEqual([]);
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
