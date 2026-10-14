import { chromium, expect } from '../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

const out = path.resolve(process.argv[2]);
const seed = JSON.parse(await fs.readFile(path.join(out, 'seed.json'), 'utf8'));
const { A, B, TEXT, EMPTY } = seed.seeds;
const headers = {Authorization: 'Bearer ' + seed.token, 'Content-Type': 'application/json'};
const api = async (url, method = 'GET', body) => {
  const res = await fetch(seed.base + '/api' + url, {method, headers, body: body === undefined ? undefined : JSON.stringify(body)});
  if (!res.ok) throw Error(await res.text());
  return res.json();
};
const browser = await chromium.launch({channel: 'msedge', headless: true, args: ['--disable-gpu', '--disable-gpu-compositing']});
const checks = [], errors = [], layouts = [];
const record = name => {checks.push(name); console.log('PASS ' + name);};
const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
await context.addInitScript(project => localStorage.setItem('ocr-project', project), seed.project);
const page = await context.newPage();
page.setDefaultTimeout(12000);
page.on('pageerror', error => errors.push(error.message));
const button = name => page.getByRole('button', {name, exact: true});
const tab = name => page.getByRole('tab', {name: new RegExp('^' + name)});
const open = async item => {
  await button('打开 ' + item.photo.name).click();
  if (item.result) await expect(page.getByLabel('当前识别结果')).toHaveValue(item.result);
};
const saved = () => expect(page.locator('.save-status')).toHaveText('已保存');
try {
  if (!process.argv.includes("--layout-only")) {
  await page.goto(seed.base + '/#token=' + seed.token);
  await expect(page.getByLabel('当前识别结果')).toHaveValue(A.result);
  await tab('文字').click();
  const resultPath = '**/api/results/' + A.result;
  await page.route(resultPath, route => route.request().method() === 'PUT' ? route.fulfill({status: 409, json: {message: '模拟保存冲突'}}) : route.continue());
  const draft = 'DRAFT_MUST_SURVIVE_FAILED_RELOAD';
  await page.getByLabel('校对文字').fill(draft);
  await expect(page.locator('.save-status')).toHaveText('保存失败');
  await page.unroute(resultPath);
  await page.route(resultPath, route => route.request().method() === 'GET' ? route.fulfill({status: 503, json: {message: '模拟读取失败'}}) : route.continue());
  await button('放弃本次修改并重新加载').click();
  await button('放弃修改并加载').click();
  await expect(page.locator('.toast')).toContainText('模拟读取失败');
  await button('保留修改').click();
  await expect(page.getByLabel('校对文字')).toHaveValue(draft);
  expect(await page.evaluate(() => !window.dispatchEvent(new Event('beforeunload', {cancelable: true})))).toBe(true);
  await page.unroute(resultPath);
  await button('重试保存').click();
  await saved();
  expect((await api('/results/' + A.result)).edited.text).toBe(draft);
  await open(B); await open(A); await tab('文字').click();
  await expect(page.getByLabel('校对文字')).toHaveValue(draft);
  record('Failed save + failed reload retains draft, unload guard, retry PUT and navigation persistence');

  let selectionWrites = 0;
  page.on('request', r => {if (r.url().endsWith('/selection') && r.method() === 'PUT') selectionWrites++;});
  await page.getByLabel('当前识别结果').selectOption(A.alternate);
  await expect(page.getByLabel('校对文字')).toHaveValue('ALTERNATE_UNADOPTED');
  await page.locator('.workspace-queue-toggle').click();
  await page.locator('.task-row').filter({hasText: 'A.png'}).first().getByRole('button', {name: '查看', exact: true}).click();
  expect(selectionWrites).toBe(0);
  expect((await api('/projects/' + seed.project)).images.find(p => p.id === A.photo.id).selected_result).toBe(A.result);
  await button('收起任务队列').click();
  await button('采用此预览').click();
  await expect.poll(async () => (await api('/projects/' + seed.project)).images.find(p => p.id === A.photo.id).selected_result).toBe(A.alternate);
  expect(selectionWrites).toBe(1);
  record('Preview and queue View never adopt; explicit adoption changes persisted selection');

  await open(EMPTY);
  await page.getByLabel('选择 B.png', {exact: true}).check();
  await expect(button('导出结果')).toBeEnabled();
  await button('导出结果').click();
  await page.getByLabel('导出范围').selectOption('selected');
  await page.getByLabel('导出格式').selectOption('txt');
  const downloadWait = page.waitForEvent('download');
  await button('保存文件').click();
  const download = await downloadWait;
  await download.saveAs(path.join(out, 'selected-from-empty.txt'));
  expect(await fs.readFile(path.join(out, 'selected-from-empty.txt'), 'utf8')).toContain('000-00');
  record('Selected successful images export while current image has no result');

  const created = await api('/projects/' + seed.project + '/tasks', 'POST', {version_ids:[EMPTY.photo.active_version],engines:['ppocr']});
  await api('/projects/' + seed.project + '/queue/pause','POST',{task_ids:created.task_ids});
  await expect(page.getByRole('heading',{name:'任务等待继续'})).toBeVisible();
  await button('继续此任务').click();
  await expect(page.getByRole('heading',{name:'正在识别图片'})).toBeVisible();
  expect((await api('/projects/' + seed.project)).tasks.filter(t=>t.image_id===EMPTY.photo.id)).toHaveLength(1);
  await api('/projects/' + seed.project + '/queue/cancel','POST',{task_ids:created.task_ids});
  await expect(page.getByRole('heading',{name:'任务已取消'})).toBeVisible();
  record('Paused and cancelled states have direct actions; continue reuses the same task');

  await open(B); await tab('文字').click();
  await page.locator('.review-bar summary').click();
  const previous = await api('/results/' + B.result);
  await api('/results/' + B.result, 'PUT', {revision: previous.revision, edited: {...previous.edited, text: 'OTHER_WINDOW_UNSEEN'}});
  await button('确认此结果').click();
  await expect(page.locator('.toast')).toContainText('已变化');
  expect((await api('/projects/' + seed.project)).images.find(p => p.id === B.photo.id).review_status).toBe('pending');
  await open(EMPTY); await open(B); await tab('文字').click();
  await expect(page.getByLabel('校对文字')).toHaveValue('OTHER_WINDOW_UNSEEN');
  if (!(await button('标记有疑问').isVisible())) await page.locator('.review-bar summary').click();
  await button('确认此结果').click();
  await expect(page.locator('.review-bar')).toContainText('已确认');
  await page.getByLabel('校对文字').fill('EDIT_AFTER_CONFIRMATION');
  await saved();
  await expect.poll(async () => (await api('/projects/' + seed.project)).images.find(p => p.id === B.photo.id).review_status).toBe('pending');
  await button('标记有疑问').click();
  await expect(page.locator('.review-bar')).toContainText('有疑问');
  await button('确认此结果').click();
  await expect(page.locator('.review-bar')).toContainText('已确认');
  await page.reload();
  await open(B);
  await expect(page.locator('.review-bar')).toContainText('已确认');
  record('Review rejects unseen revisions, survives reload, and expires when edited');

  await tab('表格').click();
  const cell = (r,c) => page.getByLabel(`第 ${r} 行，第 ${c} 列`, {exact: true});
  // Browser labels are read from the grid so keyboard behavior is exercised on real cells.
  const gridCells = page.locator('.editable-grid tbody textarea');
  await gridCells.nth(9).focus();
  await page.keyboard.press('Shift+ArrowRight');
  await page.keyboard.press('Shift+ArrowDown');
  await expect(page.getByLabel('当前单元格')).toHaveText('B2:C3');
  await button('合并').click(); await saved();
  expect((await api('/results/' + B.result)).edited.tables[0].cells.find(c => c.row === 1 && c.column === 1)).toMatchObject({row_span:2, column_span:2});
  await button('撤销').click(); await saved();
  record('Keyboard Shift+arrows creates B2:C3 rectangle and merge is undoable');

  await open(TEXT); await tab('表格').click();
  await page.getByLabel('新表格行数').fill('2');
  await page.getByLabel('新表格列数').fill('2');
  await button('创建表格').click(); await saved();
  await page.locator('.table-settings summary').click();
  await button('粘贴区域').click();
  await page.getByLabel('TSV 区域数据').fill('00123\t9007199254740993\n00456\t=SUM(A1)');
  await button('应用粘贴').click(); await saved();
  expect((await api('/results/' + TEXT.result)).edited.tables[0].cells.map(c => c.text)).toEqual(['00123','9007199254740993','00456','=SUM(A1)']);
  await button('删除整表').click(); await saved();
  expect((await api('/results/' + TEXT.result)).edited.tables).toHaveLength(0);
  await button('撤销').click(); await saved();
  expect((await api('/results/' + TEXT.result)).edited.tables).toHaveLength(1);
  record('Create/delete whole table and TSV paste preserve text identifiers and undo');

  await button('裁剪').focus();
  await page.keyboard.press('Enter');
  const coordinates = page.locator('.image-coordinate-editor input');
  await expect(coordinates).toHaveCount(4);
  await coordinates.nth(0).focus(); await page.keyboard.press('Control+A'); await page.keyboard.type('10');
  await coordinates.nth(1).focus(); await page.keyboard.press('Control+A'); await page.keyboard.type('10');
  await coordinates.nth(2).focus(); await page.keyboard.press('Control+A'); await page.keyboard.type('300');
  await coordinates.nth(3).focus(); await page.keyboard.press('Control+A'); await page.keyboard.type('200');
  await page.keyboard.press('Escape');
  await expect(page.locator('.image-coordinate-editor')).toHaveCount(0);
  record('Image crop coordinates are keyboard editable and Escape cancels selection');

  const failurePage = await context.newPage();
  failurePage.on('pageerror', error => errors.push(error.message));
  let reads = 0;
  await failurePage.route('**/api/results/' + A.alternate, route => {reads++; return route.fulfill({status:503, json:{message:'TEMPORARY_LOAD_FAILURE'}});});
  await failurePage.goto(seed.base + '/#token=' + seed.token);
  await expect(failurePage.getByRole('heading', {name:'结果加载失败'})).toBeVisible();
  await expect.poll(() => reads).toBe(3);
  await failurePage.unroute('**/api/results/' + A.alternate);
  await failurePage.getByRole('button', {name:'重试加载', exact:true}).click();
  await failurePage.getByRole('tab', {name:/^文字/}).click();
  await expect(failurePage.getByLabel('校对文字')).toHaveValue('ALTERNATE_UNADOPTED');
  await failurePage.close();
  record('Initial GET failure retries finitely and manual retry loads the same result without OCR');

  // Idle conditional polling returns only metadata, with no large image/task arrays.
  const snapshot = await api('/projects/' + seed.project);
  const unchanged = await api('/projects/' + seed.project + '?since_revision=' + snapshot.revision);
  expect(unchanged.unchanged).toBe(true);
  expect(unchanged.images).toBeUndefined();
  record('Unchanged project polling transfers only revision, queue and disk metadata');

  }
  await page.goto(seed.base + '/#token=' + seed.token);
  await open(B); await tab('表格').click();
  await page.evaluate(() => {localStorage.setItem('ocr-ui-split', '65'); localStorage.setItem('ocr-ui-sidebar', 'true');});
  await page.setViewportSize({width:1200,height:800}); await page.reload(); await open(B); await tab('表格').click();
  const geometry = async name => {
    const measured = await page.evaluate(() => {
      const panel = document.querySelector('.result-workspace').getBoundingClientRect();
      const scroll = document.querySelector('.table-scroll').getBoundingClientRect();
      const controls = [...document.querySelectorAll('.result-tabs button,.table-command-row .table-actions button')].filter(el => el.getClientRects().length).map(el => {const r = el.getBoundingClientRect(); return {text:el.textContent.trim(), x:r.x,y:r.y,width:r.width,height:r.height,inside:r.left>=panel.left-1&&r.right<=panel.right+1&&r.top>=panel.top&&r.bottom<=panel.bottom};});
      return {parts:Object.fromEntries([".app-header",".workspace-heading",".recognition-bar",".workspace-queue-toggle",".document-grid",".result-heading",".result-view-bar",".review-bar",".table-command-row",".table-settings",".result-footer",".task-drawer"].map(sel=>[sel,document.querySelector(sel)?.getBoundingClientRect().height])),viewport:[innerWidth,innerHeight],panelWidth:panel.width,tableWidth:scroll.width,tableHeight:scroll.height,controls,overflow:document.documentElement.scrollWidth>innerWidth};
    });
    layouts.push({name,...measured});
    await page.screenshot({path:path.join(out,name+'.png')});
    expect(measured.overflow).toBe(false);
    expect(measured.controls.every(c=>c.inside)).toBe(true);
    expect(measured.tableHeight).toBeGreaterThan(130);
  };
  await geometry('narrow-result-1200');
  await page.setViewportSize({width:1366,height:768});
  await page.evaluate(() => localStorage.setItem('ocr-ui-split', '48')); await page.reload(); await open(B); await tab('表格').click();
  await page.locator('.workspace-queue-toggle').click();
  await geometry('queue-open-1366');
  await button('收起任务队列').click();
  await page.setViewportSize({width:1100,height:750});
  await expect(page.locator('.result-filename')).toBeVisible();
  await expect(page.locator('.result-filename')).toContainText('B.png');
  await page.screenshot({path:path.join(out,'filename-1100.png')});
  const firstContext = await browser.newContext({viewport:{width:1024,height:576}});
  await firstContext.addInitScript(id => {localStorage.setItem('ocr-project',id); localStorage.setItem('ocr-ui-sidebar','false');},seed.empty_project);
  const firstPage = await firstContext.newPage();
  firstPage.on('pageerror', error => errors.push(error.message));
  await firstPage.goto(seed.base + '/#token=' + seed.token);
  await expect(firstPage.locator('.workspace-breadcrumb')).toContainText('首次使用空项目');
  await expect(firstPage.locator('.empty-panel').getByRole('button',{name:'导入图片',exact:true})).toBeVisible();
  await firstPage.screenshot({path:path.join(out,'first-use-empty-1024.png')});
  await firstPage.locator('input[type=file][accept*=".png"]').setInputFiles(path.join(out,'import.png'));
  await expect(firstPage.locator('.result-filename')).toContainText('import.png · 1/1');
  expect((await api('/projects/' + seed.empty_project)).images).toHaveLength(1);
  await firstPage.screenshot({path:path.join(out,'first-use-import-1024.png')});
  await firstContext.close();
  record('1200 narrow controls fit, 1366 queue leaves useful table height, filename persists, 1024 first-use imports');
  expect(errors).toEqual([]);
} finally {
  await browser.close();
  await fs.writeFile(path.join(out,'audit-fixes-evidence.json'),JSON.stringify({checks,errors,layouts},null,2));
}
console.log(JSON.stringify({passed:checks.length,errors,layouts},null,2));
