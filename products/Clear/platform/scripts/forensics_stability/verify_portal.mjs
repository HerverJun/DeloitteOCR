// External CPU-rendered acceptance against a local candidate Portal/API.
// This file does not launch a browser unless --execute is explicitly supplied.
// All project data, profiles, downloads, caches and evidence remain under the F: run root.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL, fileURLToPath } from 'node:url';

const argv = Object.fromEntries(process.argv.slice(2).reduce((items, argument, index, all) => {
  if (argument.startsWith('--')) items.push([argument.slice(2).replaceAll('-', '_'), all[index + 1]?.startsWith('--') || !all[index + 1] ? true : all[index + 1]]);
  return items;
}, []));
const runRoot = 'F:/OCR/output/forensics-stability-20261014';
const normalized = value => path.resolve(String(value)).replaceAll('\\', '/');
function requireF(value, label, mustExist = true) {
  const full = normalized(value);
  assert.match(full, /^F:\//i, label + ' must be on local F:');
  if (mustExist) assert.match(fs.realpathSync.native(full).replaceAll('\\', '/'), /^F:\//i, label + ' resolves outside F:');
  for (let entry = full; ; entry = path.dirname(entry)) {
    if (fs.existsSync(entry)) assert.ok(!fs.lstatSync(entry).isSymbolicLink(), label + ' contains a redirected directory: ' + entry);
    if (entry === path.dirname(entry)) break;
  }
  return full;
}
const seed = argv.seed ? JSON.parse(fs.readFileSync(requireF(argv.seed, 'seed'), 'utf8')) : {};
const options = { playwright_module: 'F:/OCR/frontend/node_modules/@playwright/test/index.mjs', ...seed, ...argv };
if (!options.execute) {
  console.log(JSON.stringify({ prepared: true, browser_started: false, required: ['--execute', '--base-url http://127.0.0.1:PORT',
    '--server-data-root F:/.../candidate-data', '--browser-executable PATH'],
    task_inputs: ['--workspace-id ID with --classic-id ID --deep-id ID --exif6-id ID',
      'or --source-exif6 F:/.../orientation-6.jpg to create a dedicated workspace, upload and real tasks'],
    optional: ['--seed F:/.../seed.json', '--output F:/OCR/output/forensics-stability-20261014/evidence/portal-UNIQUE'],
    fault_injection: 'Legacy and mismatch cases modify browser GET responses only; saved server evidence is never modified.' }, null, 2));
  process.exit(0);
}
assert.ok(options.base_url && options.server_data_root && options.browser_executable, 'Explicit candidate URL, data root and browser executable are required');
const base = new URL(options.base_url);
assert.ok(base.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(base.hostname), 'Only a local candidate server is allowed');
const origin = base.origin;
const dataRoot = requireF(options.server_data_root, 'candidate server data root');
assert.ok(dataRoot.toLowerCase().startsWith(runRoot.toLowerCase() + '/'), 'Candidate data must stay within this task root');
const output = requireF(options.output || runRoot + '/evidence/portal-' + Date.now(), 'output', false);
assert.ok(output.toLowerCase().startsWith(runRoot.toLowerCase() + '/'), 'Evidence must stay within this task root');
assert.ok(!fs.existsSync(output), 'Use a fresh evidence directory');
const playwrightModule = requireF(options.playwright_module, 'Playwright module');
const browserExecutable = normalized(options.browser_executable);
assert.ok(fs.statSync(browserExecutable).isFile(), 'Existing browser executable required; no browser is downloaded');
if (options.source_exif6) requireF(options.source_exif6, 'EXIF 6 source');
for (const directory of ['', '/profile', '/downloads', '/screenshots', '/browser-cache', '/crash', '/temp',
  '/appdata', '/localappdata', '/xdg-cache', '/xdg-config', '/xdg-data']) {
  const destination = requireF(output + directory, 'browser output', false);
  fs.mkdirSync(destination, { recursive: true });
}
for (const name of ['TEMP', 'TMP', 'TMPDIR']) process.env[name] = output + '/temp';
Object.assign(process.env, { APPDATA: output + '/appdata', LOCALAPPDATA: output + '/localappdata',
  XDG_CACHE_HOME: output + '/xdg-cache', XDG_CONFIG_HOME: output + '/xdg-config', XDG_DATA_HOME: output + '/xdg-data',
  PLAYWRIGHT_BROWSERS_PATH: runRoot + '/cache/ms-playwright', CHROME_USER_DATA_DIR: output + '/profile',
  DO_NOT_TRACK: '1' });
const save = (name, data) => fs.writeFileSync(output + '/' + name, JSON.stringify(data, null, 2) + '\n');
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const report = { passed: false, browser_closed: false, started_at: new Date().toISOString(),
  harness: { path: normalized(fileURLToPath(import.meta.url)), sha256: digest(fs.readFileSync(fileURLToPath(import.meta.url))) },
  candidate: { origin, data_root: dataRoot }, paths: { output, playwright_module: playwrightModule, browser_executable: browserExecutable },
  checks: [], screenshots: [], api: [], page_errors: [], fault_injections: [] };
const record = (name, details = {}) => { report.checks.push({ name, passed: true, ...details }); console.log('PASS: ' + name); };
const requestId = label => label + crypto.randomUUID().replaceAll('-', '');
async function api(suffix, { method = 'GET', body, raw } = {}) {
  const address = origin + '/api/platform/v1' + suffix;
  const response = await fetch(address, { method, headers: { Origin: origin,
    ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: raw || (body ? JSON.stringify(body) : undefined) });
  report.api.push({ method, url: address, status: response.status });
  assert.ok(response.ok, method + ' ' + suffix + ': ' + response.status + ' ' + await (response.ok ? Promise.resolve('') : response.text()));
  return response.json();
}
async function waitTask(suffix, label) {
  const deadline = Date.now() + Number(options.timeout_ms || 1800000);
  while (Date.now() < deadline) {
    const item = await api(suffix);
    if (!['queued', 'running', 'cancelling'].includes(item.status)) {
      assert.ok(['succeeded', 'partial'].includes(item.status), label + ' did not succeed: ' + JSON.stringify(item));
      return item;
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error(label + ' timed out');
}
let context, page;
try {
  const { chromium, expect: rawExpect } = await import(pathToFileURL(playwrightModule).href);
  const expect = rawExpect.configure({ timeout: 45000 });
  let workspace = options.workspace_id;
  let classicId = options.classic_id, deepId = options.deep_id, exif6Id = options.exif6_id;
  if (!workspace) {
    const created = await api('/workspaces', { method: 'POST', body: { schema_version: '1.0',
      request_id: requestId('visualWorkspace'), name: '图像取证稳定性 · 浏览器工程验收' } });
    workspace = created.workspace_id;
  }
  assert.ok(workspace, 'Workspace identity is required');
  const ws = '/workspaces/' + encodeURIComponent(workspace);
  if (!classicId || !exif6Id) {
    assert.ok(options.source_exif6, 'Missing existing jobs: provide --source-exif6 for real upload and execution');
    const bytes = fs.readFileSync(options.source_exif6);
    const asset = await api(ws + '/assets/uploads?name=' + encodeURIComponent('forensics-visual-exif6.jpg'), { method: 'POST', raw: bytes });
    assert.equal(asset.blob_hash, digest(bytes), 'The upload must preserve original JPEG bytes');
    const source = { asset_id: asset.asset_id, asset_revision: asset.revision, blob_hash: asset.blob_hash };
    save('uploaded-source.json', { original_path: options.source_exif6, uploaded: asset, sha256: digest(bytes) });
    if (!classicId) {
      classicId = (await api(ws + '/forensics', { method: 'POST', body: { schema_version: '1.0',
        request_id: requestId('visualClassic'), ...source, tools: ['noise', 'frequency', 'jpeg', 'jpeg_grid'] } })).inspection_id;
    }
    if (!exif6Id) {
      exif6Id = (await api(ws + '/multimodal/jobs', { method: 'POST', body: { request_id: requestId('visualCat'),
        kind: 'catnet', inputs: [source], options: { action: 'analyze', device: 'cpu' } } })).job_id;
    }
  }
  deepId ||= exif6Id;
  assert.ok(classicId && deepId && exif6Id, 'Classic, deep and EXIF 6 tasks are required');
  const classic = await waitTask(ws + '/forensics/' + classicId, 'classic');
  const deep = await waitTask(ws + '/multimodal/jobs/' + deepId, 'deep');
  const exif6 = deepId === exif6Id ? deep : await waitTask(ws + '/multimodal/jobs/' + exif6Id, 'EXIF 6 deep');
  assert.equal(exif6.result.processing.geometry.exif_orientation, 6, 'EXIF 6 scenario must use a genuine EXIF 6 result');
  assert.notEqual(exif6.result.width, exif6.result.height, 'Non-square source is required to expose a rotated-size mismatch');
  report.identities = { workspace_id: workspace, classic_id: classicId, deep_id: deepId, exif6_id: exif6Id,
    classic_report_hash: classic.report_hash, deep_result_hash: deep.result_hash, exif6_result_hash: exif6.result_hash };
  save('source-tasks.json', { classic, deep, exif6 });
  context = await chromium.launchPersistentContext(output + '/profile', {
    executablePath: browserExecutable, headless: true, viewport: { width: 1366, height: 900 },
    acceptDownloads: true, downloadsPath: output + '/downloads',
    args: ['--disable-gpu', '--disable-gpu-compositing', '--disable-breakpad', '--disable-crash-reporter',
      '--disable-background-networking', '--no-first-run', '--no-default-browser-check',
      '--disk-cache-dir=' + output + '/browser-cache', '--crash-dumps-dir=' + output + '/crash'],
    env: { ...process.env },
  });
  page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(45000);
  page.on('pageerror', error => report.page_errors.push(String(error)));
  const viewer = () => page.getByRole('region', { name: '图像取证对照查看器', exact: true });
  const classicURL = origin + '/apps/forensics?workspace=' + encodeURIComponent(workspace) + '&inspection=' + encodeURIComponent(classicId);
  const deepURL = id => origin + '/apps/forensics/deep?workspace=' + encodeURIComponent(workspace) + '&analysis=' + encodeURIComponent(id);
  async function shot(name) {
    await viewer().scrollIntoViewIfNeeded();
    const target = output + '/screenshots/' + name + '.png';
    await viewer().screenshot({ path: target, animations: 'disabled' });
    report.screenshots.push({ name, path: target, sha256: digest(fs.readFileSync(target)) });
  }
  async function loaded() {
    await viewer().waitFor();
    await expect(viewer().getByRole('button', { name: '叠加', exact: true })).toBeEnabled();
    const sizes = await viewer().locator('.forensic-image-probe').evaluateAll(images =>
      images.map(image => ({ src: image.src, width: image.naturalWidth, height: image.naturalHeight, complete: image.complete })));
    assert.equal(sizes.length, 2);
    assert.ok(sizes.every(image => image.complete && image.width > 0 && image.height > 0));
    assert.equal(sizes[0].width, sizes[1].width);
    assert.equal(sizes[0].height, sizes[1].height);
    return sizes;
  }
  async function checkLayers(label, address, editable) {
    await page.goto(address);
    const sizes = await loaded();
    await viewer().getByRole('button', { name: '热图', exact: true }).click();
    await expect(viewer().locator('svg [data-layer="original"]')).toHaveCount(0);
    await expect(viewer().locator('svg [data-layer="heatmap"]')).toHaveCount(1);
    await viewer().getByRole('button', { name: '叠加', exact: true }).click();
    await expect(viewer().getByRole('slider', { name: '热图透明度', exact: true })).toHaveValue('50');
    await expect(viewer().locator('svg [data-layer="heatmap"]')).toHaveAttribute('opacity', '0.5');
    await expect(viewer().locator('svg [data-layer="original"]')).toHaveCount(1);
    assert.equal(await page.getByRole('button', { name: '添加可疑区域', exact: true }).count(), editable ? 1 : 0);
    await shot(label + '-50-percent-overlay');
    await viewer().getByRole('button', { name: '1:1', exact: true }).click();
    const one = await viewer().locator('svg').evaluate(svg => {
      const bounds = svg.getBoundingClientRect();
      return { bounds: [bounds.width, bounds.height], viewbox: [svg.viewBox.baseVal.width, svg.viewBox.baseVal.height] };
    });
    one.bounds.forEach((value, index) => assert.ok(Math.abs(value - one.viewbox[index]) <= 1, '1:1 must use CSS pixels without an inherited max-width'));
    const scroll = await viewer().locator('.forensic-viewer-viewport').evaluate(area => {
      const extent = { horizontal: area.scrollWidth - area.clientWidth, vertical: area.scrollHeight - area.clientHeight };
      area.scrollLeft = Math.max(0, extent.horizontal / 2); area.scrollTop = Math.max(0, extent.vertical / 2);
      return { extent, actual: [area.scrollLeft, area.scrollTop] };
    });
    if (scroll.extent.horizontal > 0 || scroll.extent.vertical > 0) assert.ok(scroll.actual.some(number => number > 0), '1:1 overflow must scroll');
    await shot(label + '-one-to-one-scroll');
    let zoomScroll = null;
    if (scroll.extent.horizontal <= 0 && scroll.extent.vertical <= 0) {
      // Small genuine sources fit at 1:1; verify overflow without claiming a 1:1 scroll occurred.
      for (let step = 0; step < 10; step++) {
        await viewer().getByRole('button', { name: '放大图像', exact: true }).click();
        zoomScroll = await viewer().locator('.forensic-viewer-viewport').evaluate(area => {
          area.scrollLeft = Math.max(0, (area.scrollWidth - area.clientWidth) / 2);
          area.scrollTop = Math.max(0, (area.scrollHeight - area.clientHeight) / 2);
          return { actual: [area.scrollLeft, area.scrollTop], canvas: [area.scrollWidth, area.scrollHeight] };
        });
        if (zoomScroll.actual.some(number => number > 0)) break;
      }
      assert.ok(zoomScroll?.actual.some(number => number > 0), 'Zoomed pixels must remain scrollable');
      await shot(label + '-zoom-scroll-small-source');
    }
    await viewer().getByRole('button', { name: '适应窗口', exact: true }).click();
    await viewer().getByRole('button', { name: '原图', exact: true }).click();
    record(label + ': real original/map/50% overlay/1:1', { sizes, one_to_one: one, scroll, zoom_scroll_for_small_source: zoomScroll, editable });
  }
  await checkLayers('classic', classicURL, false);
  await checkLayers('deep', deepURL(deepId), true);
  await checkLayers('deep-exif6', deepURL(exif6Id), true);
  const before = await api(ws + '/multimodal/jobs/' + exif6Id);
  const existing = before.review?.regions?.length || 0;
  assert.ok(existing < 200, 'EXIF 6 task has no remaining annotation capacity');
  const svg = viewer().locator('svg');
  const area = await svg.boundingBox();
  assert.ok(area && area.width && area.height);
  // Stored [.12,.18,.38,.43] becomes display [.57,.12,.82,.38] for EXIF 6.
  await page.mouse.move(area.x + .57 * area.width, area.y + .12 * area.height);
  await page.mouse.down();
  await page.mouse.move(area.x + .82 * area.width, area.y + .38 * area.height, { steps: 12 });
  await page.mouse.up();
  await expect(viewer().locator('svg [data-region]')).toHaveCount(existing + 1);
  const annotationNote = 'EXIF6 browser roundtrip ' + Date.now();
  await page.getByLabel('区域' + (existing + 1) + '说明', { exact: true }).fill(annotationNote);
  await page.getByRole('textbox', { name: '复核人', exact: true }).fill('图像取证工程验收');
  await page.getByPlaceholder('填写复核依据、修改说明或待查事项', { exact: true }).fill('仅验收坐标、保存和重开流程；不作为真实性判断。');
  const reviewReply = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/platform/v1' + ws + '/multimodal/jobs/' + exif6Id + '/review');
  await page.getByRole('button', { name: '保存复核与校正', exact: true }).click();
  const response = await reviewReply;
  assert.equal(response.status(), 200, await response.text());
  const sent = response.request().postDataJSON();
  const stored = sent.regions.find(region => region.note === annotationNote);
  assert.ok(stored, 'Drawing and note must enter the original review payload');
  stored.box.forEach((value, index) => assert.ok(Math.abs(value - [.12, .18, .38, .43][index]) < .006, 'Display drag must save inverse-transformed stored coordinates'));
  assert.equal(sent.expected_revision, before.revision);
  const persisted = await api(ws + '/multimodal/jobs/' + exif6Id);
  assert.equal(persisted.revision, before.revision + 1);
  assert.equal(persisted.result_hash, before.result_hash, 'Review must not rewrite model output identity');
  assert.equal(persisted.inputs[0].blob_hash, before.inputs[0].blob_hash, 'Review must not rewrite the source identity');
  await page.goto(deepURL(exif6Id));
  await loaded();
  await expect(page.getByLabel('区域' + (existing + 1) + '说明', { exact: true })).toHaveValue(annotationNote);
  const reopened = await viewer().locator('svg [data-region="' + existing + '"]').evaluate(rect => ({
    x: Number(rect.getAttribute('x')), y: Number(rect.getAttribute('y')), width: Number(rect.getAttribute('width')), height: Number(rect.getAttribute('height')),
    viewbox: [rect.ownerSVGElement.viewBox.baseVal.width, rect.ownerSVGElement.viewBox.baseVal.height],
  }));
  [reopened.x / reopened.viewbox[0], reopened.y / reopened.viewbox[1], (reopened.x + reopened.width) / reopened.viewbox[0],
    (reopened.y + reopened.height) / reopened.viewbox[1]].forEach((value, index) => assert.ok(Math.abs(value - [.57, .12, .82, .38][index]) < .006));
  await shot('deep-exif6-saved-reopened-region');
  save('exif6-roundtrip.json', { sent, persisted, reopened });
  record('EXIF 6 drag/save/reopen retains stored coordinate and source/result identities');

  const classicDetail = origin + '/api/platform/v1' + ws + '/forensics/' + classicId;
  await page.route(classicDetail, async route => {
    const original = await route.fetch(), body = await original.json();
    delete body.report.input.geometry;
    for (const item of body.report.evidence) { delete item.metrics.map_coordinate_system; delete item.metrics.coordinate_version; }
    await route.fulfill({ response: original, json: body });
  });
  await page.goto(classicURL);
  await expect(page.getByText(/旧版报告缺少可核验的坐标元数据/)).toBeVisible();
  await expect(viewer().getByRole('button', { name: '叠加', exact: true })).toBeDisabled();
  await viewer().getByRole('button', { name: '热图', exact: true }).click();
  await expect(viewer().locator('.forensic-independent-image')).toBeVisible();
  await shot('classic-legacy-independent-map');
  await page.unrouteAll({ behavior: 'wait' });
  report.fault_injections.push({ case: 'classic legacy', transport: 'browser GET JSON only', server_modified: false });
  record('legacy classic report retains preview and only permits independent map viewing');

  const exifDetail = origin + '/api/platform/v1' + ws + '/multimodal/jobs/' + exif6Id;
  await page.route(exifDetail, async route => {
    const original = await route.fetch(), body = await original.json();
    delete body.result.processing.geometry;
    delete body.result.processing.display_preview;
    delete body.result.processing.display_heatmap;
    await route.fulfill({ response: original, json: body });
  });
  await page.goto(deepURL(exif6Id));
  await expect(page.getByText(/保留历史原图与原保存坐标/)).toBeVisible();
  await expect(viewer().getByRole('button', { name: '叠加', exact: true })).toBeDisabled();
  await expect(viewer().locator('svg [data-layer="original"]')).toHaveAttribute('href', /\/preview\.png$/);
  await viewer().getByRole('button', { name: '热图', exact: true }).click();
  await expect(viewer().locator('.forensic-independent-image')).toBeVisible();
  await shot('deep-legacy-independent-map');
  await page.unrouteAll({ behavior: 'wait' });
  report.fault_injections.push({ case: 'deep legacy', transport: 'browser GET JSON only', server_modified: false });
  record('legacy deep result retains stored preview and disables unverified overlay');

  const displayMapURL = exifDetail + '/files/' + encodeURIComponent(exif6.result.processing.display_heatmap);
  const storedMapURL = exifDetail + '/files/heatmap.png';
  const rawMap = await fetch(storedMapURL);
  assert.ok(rawMap.ok);
  const rawMapBytes = Buffer.from(await rawMap.arrayBuffer());
  await page.route(displayMapURL, route => route.fulfill({ status: 200, contentType: 'image/png', body: rawMapBytes }));
  await page.goto(deepURL(exif6Id));
  await expect(page.getByText(/实际尺寸或坐标信息不一致/)).toBeVisible();
  await expect(viewer().getByRole('button', { name: '叠加', exact: true })).toBeDisabled();
  await viewer().getByRole('button', { name: '热图', exact: true }).click();
  await expect(viewer().locator('.forensic-independent-image')).toBeVisible();
  await expect(viewer().locator('svg')).toHaveCount(0);
  await shot('deep-dimension-mismatch-independent-map');
  await page.unrouteAll({ behavior: 'wait' });
  report.fault_injections.push({ case: 'map dimensions mismatch', substituted: 'real stored heatmap PNG in place of display heatmap',
    stored_map_sha256: digest(rawMapBytes), transport: 'browser GET PNG only', server_modified: false });
  record('map size mismatch never stretches or registers the mismatched heatmap');
  assert.deepEqual(report.page_errors, [], 'Browser runtime errors are not acceptable');
  report.passed = true;
} catch (error) {
  report.error = error.stack || String(error);
  console.error(report.error);
  if (page) await page.screenshot({ path: output + '/screenshots/failure.png', fullPage: false, animations: 'disabled' }).catch(() => {});
  process.exitCode = 1;
} finally {
  if (context) {
    try { await context.close(); report.browser_closed = true; }
    catch (error) { report.passed = false; report.close_error = String(error); process.exitCode = 1; }
  }
  report.finished_at = new Date().toISOString();
  save('portal-acceptance.json', report);
  console.log(JSON.stringify({ passed: report.passed, browser_closed: report.browser_closed, report: output + '/portal-acceptance.json' }));
}
