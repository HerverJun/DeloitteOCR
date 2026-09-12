// Stable PNG of the review-only state already exercised in the in-app browser.
import { chromium, expect } from '../../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
const out='C:/Users/A/Desktop/OCR/audit/real-user-20260912/evidence';
const browser=await chromium.launch({channel:'msedge',headless:true,args:['--disable-gpu','--disable-gpu-compositing']});
try {
  const context=await browser.newContext({viewport:{width:1366,height:768}});
  const page=await context.newPage();
  await page.goto('http://127.0.0.1:8878/#token=real-user-audit-20260912-isolated-token');
  await page.getByLabel('当前项目',{exact:true}).selectOption({label:'文件夹导入 已重命名'});
  await page.getByRole('tab',{name:'文字',exact:true}).click();
  await expect(page.getByRole('button',{name:'任务队列 队列异常，请查看恢复提示',exact:true})).toBeVisible();
  await expect(page.getByLabel('校对文字',{exact:true})).toHaveValue(/Yuyuan/);
  await page.screenshot({path:out+'/screenshots/04-review-only-queue.png',fullPage:true,animations:'disabled'});
  const health=await (await fetch('http://127.0.0.1:8878/api/health')).json();
  await fs.writeFile(out+'/review-only-checks.json',JSON.stringify({time:new Date().toISOString(),health,uiEvidence:{label:'仅校对与导出模式',queueSummary:'队列异常，请查看恢复提示',startRecognitionEnabled:await page.getByRole('button',{name:'开始识别',exact:true}).isEnabled(),regionRecognitionEnabled:await page.getByRole('button',{name:'区域重识别',exact:true}).isEnabled(),engineManagementButtons:await page.getByRole('button',{name:'引擎管理',exact:true}).count()},inAppTests:{textEditAutosave:true,textExportMarker:'QA REVIEW_ONLY 保存导出',originalTextRestored:true,contrastProducedVersion3:true,dewarpDisabled:true,continueAndRetryDisabled:true},finding:'A healthy review-only service is incorrectly described as an abnormal queue in the sidebar.'},null,2));
  await context.close();
  console.log('Review-only evidence saved');
} finally {await browser.close();}
