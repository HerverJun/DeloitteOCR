import { chromium, expect } from '@playwright/test';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';

const root = path.resolve('../..');
const record = process.argv.includes('--record');
const out = path.join(root, 'reports/tutorial', record ? 'recording' : 'acceptance');
mkdirSync(out, { recursive: true });
const manifest = JSON.parse(readFileSync('E:/image-platform-data/tutorial-flowers/manifest.json', 'utf8'));
const narrationFile = path.join(root, 'reports/tutorial/narration.json');
const narration = record ? JSON.parse(readFileSync(narrationFile, 'utf8')) : [];
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || 'C:/Users/A/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe',
  headless: true, args: ['--disable-gpu', '--disable-gpu-compositing'], slowMo: record ? 300 : 0,
});
const context = await browser.newContext({ baseURL: 'http://127.0.0.1:8765', viewport: { width: 1440, height: 900 },
  ...(record ? { recordVideo: { dir: out, size: { width: 1440, height: 900 } } } : {}), acceptDownloads: true });
context.setDefaultTimeout(20000);
await context.addInitScript(() => {
  document.addEventListener('DOMContentLoaded', () => {
    const cursor = document.createElement('div');
    cursor.style.cssText = 'position:fixed;left:-50px;top:-50px;width:26px;height:26px;border:3px solid #f29b22;border-radius:50%;background:#f29b2230;z-index:2147483647;pointer-events:none;transform:translate(-50%,-50%);box-shadow:0 0 0 2px white';
    document.body.appendChild(cursor);
    document.addEventListener('pointermove', e => { cursor.style.left = e.clientX+'px'; cursor.style.top=e.clientY+'px'; });
    document.addEventListener('pointerdown', () => { cursor.style.background='#f29b2299'; });
    document.addEventListener('pointerup', () => { cursor.style.background='#f29b2230'; });
  });
});
const start = Date.now();
const page = await context.newPage();
const errors = [], httpErrors = [], timeline = [];
const report = { started_at: new Date().toISOString(), record, source: manifest.homepage, checks: [], timeline };
page.on('pageerror', e => errors.push(e.message));
page.on('response', r => { if (r.status() >= 400 && r.url().includes('/api/')) httpErrors.push({url:r.url(),status:r.status()}); });
const get = async url => { const r=await context.request.get('/api/v1'+url); expect(r.ok()).toBeTruthy(); return r.json(); };
const postResponse = suffix => page.waitForResponse(r => r.url().endsWith(suffix) && r.request().method()==='POST');
const pause = seconds => record ? page.waitForTimeout(seconds * 1000) : Promise.resolve();
async function select(label, text) {
  await page.locator('.ant-select').filter({ has: page.getByRole('combobox', { name:label, exact:true }) }).locator('.ant-select-selector').click();
  await page.locator('.ant-select-dropdown:visible').getByText(text, {exact:true}).click();
  await expect(page.locator('.ant-select-dropdown:visible')).toHaveCount(0);
}
async function waitJob(id, timeout=180000) {
  const until = Date.now()+timeout;
  while (Date.now()<until) {
    const job=await get('/jobs/'+id);
    if (['failed','cancelled','completed_with_errors'].includes(job.state)) throw new Error(JSON.stringify(job));
    if (job.state==='succeeded') { expect(job.failed).toBe(0); return job; }
    await page.waitForTimeout(1000);
  }
  throw new Error('Job timeout: '+id);
}
async function step(id, fn) {
  console.log('STEP', id);
  const mark={id,start:(Date.now()-start)/1000}; timeline.push(mark);
  await fn();
  if(record) {
    const minimum=narration.find(s=>s.id===id)?.duration || 5;
    const remaining=minimum*1000+1000-(Date.now()-start-mark.start*1000);
    if(remaining>0) await page.waitForTimeout(remaining);
  }
  await page.screenshot({ path:path.join(out,id+'.png') });
  mark.end=(Date.now()-start)/1000;
  writeFileSync(path.join(out,'timeline.json'),JSON.stringify(timeline,null,2));
}
async function importFolder(folder, source) {
  await page.getByRole('menuitem',{name:'图像与标注'}).click();
  await page.getByRole('button',{name:'导入文件夹'}).click();
  await page.getByRole('textbox',{name:'文件夹完整路径'}).fill(folder);
  await page.getByRole('textbox',{name:'来源名称'}).fill(source);
}
let project, dataset, trainJob, model, inferJob, rowsBefore, assets, manual;
try {
  await page.goto('/');
  await step('01-create', async()=>{
    await page.getByRole('button',{name:'新建项目'}).click();
    await page.getByRole('textbox',{name:'项目名称'}).fill((record?'教学示范':'全流程验收')+' · 花卉三分类 · '+new Date().toLocaleTimeString('zh-CN',{hour12:false}));
    await page.getByRole('textbox',{name:'项目说明'}).fill('Oxford Flowers102 公开照片；60 张准备样本 + 30 张独立待分类图片。');
    await page.getByRole('textbox',{name:'类别定义'}).fill('daisy,牛眼菊\nrose,玫瑰\nsunflower,向日葵');
  });
  await step('02-empty',async()=>{
    const response=postResponse('/projects');
    await page.getByRole('button',{name:'创建项目'}).click();
    const r=await response; expect(r.status()).toBe(201); project=await r.json(); report.project_id=project.id;
    expect((await get(`/projects/${project.id}/assets`)).total).toBe(0);
    expect(await get(`/projects/${project.id}/models`)).toHaveLength(0);
    report.checks.push('new project initially has no assets or models');
    await expect(page.getByRole('heading',{name:'图像与标注'})).toBeVisible();
  });
  await step('03-import',async()=>{ await importFolder('E:\\image-platform-data\\tutorial-flowers\\01-prepare','Oxford 官方训练和验证照片'); });
  await step('04-images',async()=>{
    const response=postResponse('/imports'); await page.getByRole('button',{name:'开始导入'}).click();
    const {job_id}=await (await response).json(); report.import_job_id=job_id; await waitJob(job_id);
    assets=(await get(`/projects/${project.id}/assets?limit=100`)).items;
    expect(assets).toHaveLength(60); expect(assets.filter(a=>!a.label_id)).toHaveLength(1);
    await page.getByRole('menuitem',{name:'图像与标注'}).click();
    await expect(page.locator('.asset-card')).toHaveCount(24);
    report.checks.push('60 real photos imported; 59 auto-labelled and one unlabelled');
  });
  await step('05-label',async()=>{
    await select('筛选类别','未标注'); await expect(page.locator('.asset-card')).toHaveCount(1);
    await pause(2);
    manual=manifest.records.find(r=>r.manual_annotation);
    await page.getByRole('checkbox',{name:'选择 '+manual.filename,exact:true}).check();
    await select('设置选中图片类别',manual.display_name);
    await pause(4);
    const response=postResponse('/annotations'); await page.getByRole('button',{name:'保存标注'}).click(); expect((await response).ok()).toBeTruthy();
    await select('筛选类别','全部类别');
    const labelled=(await get(`/projects/${project.id}/assets?limit=100`)).items;
    expect(labelled.every(a=>a.label_id)).toBeTruthy();
    for(const c of manifest.classes) expect(labelled.filter(a=>a.label_id===c.label_id)).toHaveLength(20);
    report.checks.push('manual annotation saved; all 3 classes have 20 labelled images');
  });
  await step('06-freeze',async()=>{
    await page.getByRole('menuitem',{name:'训练与实验'}).click();
    await page.getByRole('button',{name:'发布数据版本'}).click();
    await page.getByRole('textbox',{name:'版本名称'}).fill('花卉样本 v1 · 60 张');
    await pause(5);
    const response=postResponse('/dataset-versions'); await page.getByRole('button',{name:'冻结并发布'}).click();
    const r=await response; expect(r.status()).toBe(201); dataset=await r.json(); report.dataset=dataset;
    expect(dataset.stats.total).toBe(60); expect(dataset.stats.splits).toEqual({train:36,val:12,test:12});
    report.checks.push('frozen 60-image snapshot split into 36 train / 12 validation / 12 test');
  });
  await step('07-configure',async()=>{
    await select('预训练编码器','DINOv2 ViT-S/14 · 通用视觉特征');
    await pause(3);
    await select('分类头方法','逻辑回归 · 验证集正则化搜索');
    await pause(3);
    await page.getByRole('button',{name:'开始训练并导出 ONNX'}).scrollIntoViewIfNeeded();
  });
  await step('08-train',async()=>{
    const response=postResponse('/experiments'); await page.getByRole('button',{name:'开始训练并导出 ONNX'}).click();
    const r=await response; expect(r.status()).toBe(202); const {job_id}=await r.json(); report.train_job_id=job_id;
    trainJob=await waitJob(job_id); report.train_job=trainJob;
    model=(await get(`/projects/${project.id}/models`))[0]; expect(model).toBeTruthy(); report.model=model;
    report.checks.push('new classifier trained from the frozen snapshot and exported to ONNX');
  });
  await step('09-evaluate',async()=>{
    await page.getByRole('menuitem',{name:'模型库'}).click(); await page.getByRole('button',{name:'评估详情'}).click();
    await expect(page.getByRole('heading',{name:'混淆矩阵'})).toBeVisible();
    await expect(page.getByText('CUDA GPU',{exact:true})).toBeVisible();
  });
  await step('10-model',async()=>{
    await page.getByRole('button',{name:/^关\s*闭$/}).click();
    const download=page.waitForEvent('download'); await page.getByRole('link',{name:/下载/}).click();
    const file=await download; await file.saveAs(path.join(out,'flower-model.zip')); expect(await file.failure()).toBeNull();
    const activate=page.getByRole('button',{name:'设为默认'}); if(await activate.isEnabled()) await activate.click();
    report.checks.push('model ZIP downloaded through UI');
  });
  await step('11-unseen',async()=>{ await importFolder('E:\\image-platform-data\\tutorial-flowers\\02-classify','Oxford 官方测试照片 · 独立待分类'); });
  await step('12-scope',async()=>{
    const response=postResponse('/imports'); await page.getByRole('button',{name:'开始导入'}).click();
    const {job_id}=await (await response).json(); report.holdout_import_job_id=job_id; await waitJob(job_id);
    const all=(await get(`/projects/${project.id}/assets?limit=100`)).items; expect(all).toHaveLength(90); expect(all.filter(a=>!a.label_id)).toHaveLength(30);
    await page.getByRole('button',{name:'新建分类任务'}).click();
    await expect(page.getByRole('checkbox',{name:'按验证集阈值自动接受结果（试验）'})).not.toBeChecked();
    report.checks.push('30 independent official-test images imported without labels after training');
  });
  await step('13-classify',async()=>{
    const response=postResponse('/batch-jobs'); await page.getByRole('button',{name:'开始分类'}).click();
    const r=await response; expect(r.status()).toBe(202); const {job_id}=await r.json(); report.inference_job_id=job_id;
    inferJob=await waitJob(job_id); report.inference_job=inferJob; expect(inferJob.done).toBe(30);
    const row=page.getByRole('row').filter({hasText:job_id.slice(-8)}); await row.getByRole('button',{name:/^详\s*情$/}).click();
    await page.locator('.job-detail').scrollIntoViewIfNeeded();
    await pause(3);
    await page.locator('.job-detail .data-toolbar').evaluate(el=>el.scrollIntoView({block:'start'}));
    rowsBefore=(await get(`/jobs/${job_id}/results?limit=100`)).items;
    expect(rowsBefore).toHaveLength(30);
    const truths=Object.fromEntries(manifest.records.filter(r=>r.role==='classify').map(r=>[r.filename,r.label_id]));
    expect(rowsBefore.every(r=>r.state==='succeeded' && r.prediction.status==='needs_review' && truths[r.name])).toBeTruthy();
    report.holdout={count:30,correct:rowsBefore.filter(r=>truths[r.name]===r.prediction.label_id).length,
      rows:rowsBefore.map(r=>({name:r.name,truth:truths[r.name],prediction:r.prediction.label_id,score:r.prediction.score}))};
    report.checks.push('all 30 unseen images classified; original predictions compared with sealed ground truth');
  });
  await step('14-review',async()=>{
    await page.locator('.result-card').first().getByRole('button',{name:'查看并确认'}).click();
    await expect(page.locator('.review-image img')).toBeVisible();
    const filename=await page.locator('.review-image img').getAttribute('alt');
    const truth=manifest.records.find(r=>r.filename===filename&&r.role==='classify');
    await select('人工确认类别',truth.display_name); report.reviewed_filename=filename;
  });
  await step('15-save',async()=>{
    const response=postResponse('/annotations'); await page.getByRole('button',{name:'确认并保存'}).click(); expect((await response).status()).toBe(200);
    const after=(await get(`/jobs/${inferJob.id}/results?limit=100`)).items;
    expect(after.map(r=>r.prediction)).toEqual(rowsBefore.map(r=>r.prediction));
    expect(after.find(r=>r.name===report.reviewed_filename).confirmed_label).toBe(manifest.records.find(r=>r.filename===report.reviewed_filename).label_id);
    expect((await get(`/projects/${project.id}/dataset-versions`))[0].stats).toEqual(dataset.stats);
    await page.locator('.job-detail .data-toolbar').scrollIntoViewIfNeeded();
    report.checks.push('human review saved without altering original predictions or frozen snapshot');
  });
  await step('16-export',async()=>{
    for(const format of ['CSV','JSONL']) {
      const download=page.waitForEvent('download'); await page.getByRole('link',{name:new RegExp(format)}).click();
      const file=await download; await file.saveAs(path.join(out,'classification-results.'+format.toLowerCase())); expect(await file.failure()).toBeNull();
      await pause(3);
    }
    const jsonl=readFileSync(path.join(out,'classification-results.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
    expect(jsonl).toHaveLength(30); expect(jsonl.every(r=>r.model_version_id===model.id)).toBeTruthy();
    const reviewedAsset=rowsBefore.find(r=>r.name===report.reviewed_filename);
    const exportedReview=jsonl.find(r=>r.asset_id===reviewedAsset.asset_id);
    expect(exportedReview.current_annotation_label_id).toBe(manifest.records.find(r=>r.filename===report.reviewed_filename).label_id);
    expect(exportedReview.label_id).toBe(reviewedAsset.prediction.label_id);
    expect(exportedReview.effective_label_id).toBe(exportedReview.current_annotation_label_id);
    const csv=readFileSync(path.join(out,'classification-results.csv'),'utf8'); expect(csv.trim().split(/\r?\n/)).toHaveLength(31);
    report.checks.push('CSV and JSONL downloaded, each contains all 30 predictions and correct model ID');
    report.checks.push('exports retain original prediction and include current annotation and effective label');
  });
  await step('17-reload',async()=>{
    await page.reload(); await expect(page.getByText(project.name,{exact:true}).first()).toBeVisible();
    await page.getByRole('menuitem',{name:'分类与复核'}).click();
    await page.getByRole('row').filter({hasText:inferJob.id.slice(-8)}).getByRole('button',{name:/^详\s*情$/}).click();
    await page.locator('.job-detail').scrollIntoViewIfNeeded();
    await page.locator('.job-detail .data-toolbar').evaluate(el=>el.scrollIntoView({block:'start'}));
    await expect(page.locator('.result-card')).toHaveCount(12);
    expect((await get(`/jobs/${inferJob.id}/results?limit=100`)).total).toBe(30);
    report.checks.push('project selection, completed jobs and all results persist after browser reload');
  });
  expect(errors).toEqual([]); expect(httpErrors).toEqual([]); report.status='passed';
} catch(error) {
  report.status='failed'; report.error=error.stack;
  await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});
  console.error(error); process.exitCode=1;
} finally {
  report.page_errors=errors; report.http_errors=httpErrors;
  report.elapsed_seconds=(Date.now()-start)/1000;
  writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));
  await context.close();
  if(record) { const video=await page.video().path(); writeFileSync(path.join(out,'video-path.txt'),video); }
  await browser.close();
  console.log(JSON.stringify({status:report.status,project_id:project?.id,holdout:report.holdout&&{correct:report.holdout.correct,count:30},out}));
}
