import { chromium, expect } from '../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
const out = path.resolve(process.argv[2]);
const seed = JSON.parse(await fs.readFile(path.join(out, 'seed.json'), 'utf8'));
const browser = await chromium.launch({channel:'msedge',headless:true,args:['--disable-gpu','--disable-gpu-compositing']});
const context = await browser.newContext({viewport:{width:1440,height:1000}});
await context.addInitScript(() => {
  Object.defineProperty(navigator, 'clipboard', {value:{writeText:async text => {window.__auditClipboard = text;}}});
});
const page = await context.newPage();
const evidence = {scope:'Real source UI/API/SQLite and PDF extraction; seeded OCR, geometry and review suggestions; clipboard captured in memory', checks:[], pageErrors:[]};
const record = (name, details={}) => { evidence.checks.push({name,passed:true,...details}); console.log(name); };
page.on('pageerror', error => evidence.pageErrors.push(String(error)));
try {
  await page.goto(seed.base + '/#token=' + seed.token);
  const tree = page.getByRole('region',{name:'文档与页面',exact:true});
  await page.getByLabel('选择文档',{exact:true}).selectOption(seed.document);
  await tree.locator('.document-pages').getByRole('button',{name:/第 1 页/}).click();
  await page.locator('button.queue-summary').click();
  await page.getByRole('region',{name:'任务队列',exact:true}).locator('.task-row').filter({hasText:seed.probe_page2_name}).filter({hasText:'PP-OCRv6'}).getByRole('button',{name:'查看',exact:true}).click();
  await page.getByRole('button',{name:'收起任务队列',exact:true}).click();
  await expect(page.getByLabel('当前识别结果',{exact:true})).toHaveValue(seed.probe_page2_result);
  const marked = tree.locator('.document-pages button[aria-current="page"]');
  await expect(marked).toHaveCount(1);
  await expect(marked).toContainText('第 2 页');
  record('R1 task preview synchronizes the only current page', {page:seed.probe_pages[1].id});

  const reviewQueue = page.getByRole('region',{name:'文档复核队列',exact:true});
  await reviewQueue.getByRole('button',{name:/第 2 页 · 视觉审校：导航审计用合成建议/}).click();
  await expect(page.getByLabel('当前识别结果',{exact:true})).toHaveValue(seed.probe_page2_adopted_result);
  await expect(page.getByRole('tab',{selected:true})).toHaveText(/视觉审校/);
  await expect(page.getByLabel('选择审校建议',{exact:true})).toHaveValue(seed.probe_page2_multimodal.proposal_id);
  record('R3 review navigation replaces the earlier preview with the issue result', {result:seed.probe_page2_adopted_result});
  await page.screenshot({path:path.join(out,'01-review-target.png'),fullPage:true,animations:'disabled'});

  await page.getByRole('tab',{name:'文字',exact:true}).click();
  const editor = page.getByLabel('校对文字',{exact:true});
  await expect(editor).toHaveValue(seed.probe_page2_adopted_text);
  const editedText = seed.probe_page2_adopted_text + '\n"甲方" C:\\财务\\报表\nélodie\t0000123\n=SUM(A1:A2)';
  await editor.fill(editedText);
  const savedTextResponse = page.waitForResponse(res => res.url().endsWith(`/api/results/${seed.probe_page2_adopted_result}/text`));
  await page.getByRole('button',{name:'复制',exact:true}).click();
  expect((await savedTextResponse).headers()['content-type']).toBe('text/plain; charset=utf-8');
  await expect.poll(() => page.evaluate(() => window.__auditClipboard)).toBe(editedText);
  const saved = await fetch(`${seed.base}/api/results/${seed.probe_page2_adopted_result}`, {headers:{Authorization:'Bearer '+seed.token}}).then(res=>res.json());
  expect(saved.edited.text).toBe(editedText);
  expect(saved.original.text).toBe(seed.probe_page2_adopted_text);
  record('R2 copy flushes the visible edit and preserves original text', {result:seed.probe_page2_adopted_result, revision:saved.revision});

  // Recreate R1 independently: review navigation itself could repair stale
  // page state, masking a regression in the task-queue entry point.
  await tree.locator('.document-pages').getByRole('button',{name:/第 1 页/}).click();
  await page.locator('button.queue-summary').click();
  await page.getByRole('region',{name:'任务队列',exact:true}).locator('.task-row').filter({hasText:seed.probe_page2_name}).filter({hasText:'PP-OCRv6'}).getByRole('button',{name:'查看',exact:true}).click();
  await page.getByRole('button',{name:'收起任务队列',exact:true}).click();
  await expect(page.getByLabel('当前识别结果',{exact:true})).toHaveValue(seed.probe_page2_result);
  await expect(marked).toHaveCount(1);
  await expect(marked).toContainText('第 2 页');
  const requested = page.waitForRequest(req => req.method()==='POST' && /\/api\/pages\/[^/]+\/process$/.test(req.url()));
  await tree.getByRole('button',{name:'处理当前页',exact:true}).click();
  const request = await requested;
  expect(request.url().split('/').at(-2)).toBe(seed.probe_pages[1].id);
  record('R1 process current page sends the visible page ID', {actualPage:request.url().split('/').at(-2)});

  await page.getByRole('button',{name:'打开 review-table.png',exact:true}).click();
  await page.getByLabel('当前识别结果',{exact:true}).selectOption(seed.result);
  await expect(tree.getByRole('button',{name:'处理当前页',exact:true})).toBeDisabled();
  await expect(marked).toHaveCount(0);
  record('R1 independent image clears stale document page actions');
  const copiedResponse = page.waitForResponse(res => res.url().endsWith(`/api/results/${seed.result}/text`) && res.request().method()==='GET');
  await page.getByRole('button',{name:'复制',exact:true}).click();
  const response = await copiedResponse;
  expect(response.status()).toBe(200);
  expect(response.headers()['content-type']).toBe('text/plain; charset=utf-8');
  const expected = 'Item\tCode\tAmount\nAlpha\t00123\t-12.50\nBeta\t00987\t123.45';
  await expect.poll(() => page.evaluate(() => window.__auditClipboard)).toBe(expected);
  record('R2 fusion clipboard contains literal table text', {contentType:response.headers()['content-type'],copied:expected});
  await page.screenshot({path:path.join(out,'02-copy-plain-text.png'),fullPage:true,animations:'disabled'});

  const unauthenticated = await fetch(`${seed.base}/api/results/${seed.result}/text`);
  expect(unauthenticated.status).toBe(401);
  expect(response.headers()['cache-control']).toContain('no-store');
  expect(response.headers()['x-content-type-options']).toBe('nosniff');
  record('R2 text endpoint preserves authentication and private-response headers');
  expect(evidence.pageErrors).toEqual([]);
} catch (error) {
  evidence.error = error.stack || String(error);
  await fs.writeFile(path.join(out,'failure-dom.txt'), await page.locator('body').innerText());
  await page.screenshot({path:path.join(out,'failure.png'),fullPage:true,animations:'disabled'}).catch(()=>{});
  process.exitCode = 1;
} finally {
  await browser.close();
  evidence.browserClosed = true;
  await fs.writeFile(path.join(out,'results.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence,null,2));
}
