import { chromium, expect } from '../../frontend/node_modules/@playwright/test/index.mjs';
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
const evidence = {scope:'Real UI/API/SQLite with explicitly seeded OCR and fusion; clipboard OS write captured in memory', findings:[], pageErrors:[]};
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
  const marked = await tree.locator('.document-pages button[aria-current="page"]').allTextContents();
  const requested = page.waitForRequest(req => req.method()==='POST' && /\/api\/pages\/[^/]+\/process$/.test(req.url()));
  await tree.getByRole('button',{name:'处理当前页',exact:true}).click();
  const request = await requested;
  evidence.findings.push({id:'F02-navigation', visibleImage:seed.probe_page2_name, visibleResult:await page.getByLabel('当前识别结果',{exact:true}).inputValue(), expectedPage:seed.probe_pages[1].id, actualPage:request.url().split('/').at(-2), markedCurrentPages:marked, reproduced:request.url().includes('/'+seed.probe_pages[0].id+'/')});
  await page.screenshot({path:path.join(out,'navigation-wrong-page.png'),fullPage:true,animations:'disabled'});
  const reviewQueue = page.getByRole('region',{name:'文档复核队列',exact:true});
  const reviewButton = reviewQueue.getByRole('button',{name:/第 2 页 · 视觉审校：导航审计用合成建议/});
  await reviewButton.click();
  await expect(reviewButton).toBeEnabled();
  await page.evaluate(() => new Promise(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  const actualReviewResult = await page.getByLabel('当前识别结果',{exact:true}).inputValue();
  evidence.findings.push({id:'F03-review-sticky-preview',expectedResult:seed.probe_page2_adopted_result,actualResult:actualReviewResult,previewResult:seed.probe_page2_result,selectedTab:await page.getByRole('tab',{selected:true}).innerText(),reproduced:actualReviewResult===seed.probe_page2_result&&actualReviewResult!==seed.probe_page2_adopted_result});
  await page.screenshot({path:path.join(out,'review-wrong-result.png'),fullPage:true,animations:'disabled'});
  await page.getByRole('button',{name:'打开 review-table.png',exact:true}).click();
  await page.getByLabel('当前识别结果',{exact:true}).selectOption(seed.result);
  const exported = page.waitForResponse(res => res.url().endsWith('/api/export') && res.request().method()==='POST');
  await page.getByRole('button',{name:'复制',exact:true}).click();
  const response = await exported;
  await expect.poll(() => page.evaluate(() => window.__auditClipboard?.length || 0)).toBeGreaterThan(0);
  const copied = await page.evaluate(() => window.__auditClipboard);
  evidence.findings.push({id:'F01-copy-zip',contentType:response.headers()['content-type'],characters:copied.length,prefixCodePoints:[...copied.slice(0,4)].map(c=>c.charCodeAt(0)),prefix:copied.slice(0,80),reproduced:copied.startsWith('PK\x03\x04')});
  await page.screenshot({path:path.join(out,'copy-zip-success-toast.png'),fullPage:true,animations:'disabled'});
  expect(evidence.findings.every(item=>item.reproduced)).toBe(true);
} catch (error) {
  evidence.error = error.stack || String(error);
  await fs.writeFile(path.join(out,'failure-dom.txt'), await page.locator('body').innerText());
  await page.screenshot({path:path.join(out,'failure.png'),fullPage:true,animations:'disabled'}).catch(()=>{});
  process.exitCode = 1;
} finally {
  await browser.close();
  evidence.browserClosed = true;
  await fs.writeFile(path.join(out,'extended-results.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence,null,2));
}
