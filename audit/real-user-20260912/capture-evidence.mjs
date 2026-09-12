// Reproduce already-tested UI states for stable evidence PNGs only.
// Main functional testing was performed through the Codex in-app browser.
import { chromium, expect } from '../../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

const root = path.resolve('C:/Users/A/Desktop/OCR');
const out = path.join(root, 'audit/real-user-20260912/evidence/screenshots');
await fs.mkdir(out, { recursive: true });
const browser = await chromium.launch({channel:'msedge',headless:true,args:['--disable-gpu','--disable-gpu-compositing']});
const context = await browser.newContext({viewport:{width:1366,height:768}});
await context.addInitScript(() => localStorage.setItem('ocr-project','d2804da1366f4d138dda3f7e7a6612b6'));
const page = await context.newPage();
page.setDefaultTimeout(15000);
const capture = async name => page.screenshot({path:path.join(out,name),fullPage:true,animations:'disabled'});
try {
  await page.goto('http://127.0.0.1:8878/#token=real-user-audit-20260912-isolated-token');
  await page.getByRole('button',{name:'打开 chinese_handwriting.png',exact:true}).click();
  await page.getByLabel('当前识别结果',{exact:true}).selectOption({label:'HunyuanOCR · 结果 2'});
  await page.getByRole('tab',{name:/^表格/}).click();
  await expect(page.getByLabel('第 2 行第 1 列',{exact:true})).toHaveValue('一级');
  await capture('01-real-handwriting-table.png');
  await page.getByRole('button',{name:'引擎管理',exact:true}).click();
  await page.getByLabel('选择离线引擎包',{exact:true}).setInputFiles(path.join(root,'audit/real-user-20260912/edge-cases/invalid-engine.zip'));
  await expect(page.locator('.toast.error')).toContainText('缺少有效的引擎包清单');
  await expect.poll(() => page.locator('.toast.error').evaluate(el=>!!el.closest('[aria-hidden="true"]'))).toBe(true);
  await capture('02-engine-error-behind-modal.png');
  const engineError = await page.locator('.toast.error').evaluate(el=>({text:el.innerText,zIndex:getComputedStyle(el).zIndex,hiddenAncestor:!!el.closest('[aria-hidden="true"]')}));
  await page.getByRole('button',{name:'关闭',exact:true}).click();
  await page.getByRole('button',{name:'关闭提示',exact:true}).click();
  await page.getByRole('button',{name:'打开 chinese_scanned_exam.png',exact:true}).click();
  await page.getByRole('button',{name:'导出结果',exact:true}).click();
  await page.getByLabel('导出格式',{exact:true}).selectOption('xlsx');
  await page.getByRole('button',{name:'保存文件',exact:true}).click();
  await expect(page.locator('.toast.error')).toContainText('没有结构化表格');
  await capture('03-xlsx-error-behind-modal.png');
  const exportError = await page.locator('.toast.error').evaluate(el=>({text:el.innerText,zIndex:getComputedStyle(el).zIndex,hiddenAncestor:!!el.closest('[aria-hidden="true"]')}));
  await fs.writeFile(path.join(out,'capture-receipt.json'),JSON.stringify({created:new Date().toISOString(),purpose:'Stable screenshots reproducing states already tested in the in-app browser',browser:'External Edge/Playwright',args:['--disable-gpu','--disable-gpu-compositing'],viewport:{width:1366,height:768},engineError,exportError},null,2));
  console.log('3 evidence screenshots saved');
} finally { await context.close(); await browser.close(); }
