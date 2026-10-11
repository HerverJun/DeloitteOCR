// Real production UI and HTTP persistence. No mocked endpoint or model result.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';

const output=process.argv[2]?.replaceAll('\\','/');
const resume=process.argv[3]==='--resume';
assert.ok(output?.startsWith('F:/OCR/output/'));
const real=value=>fs.realpathSync(value).replaceAll('\\','/');
assert.ok(real(output).startsWith('F:/'));
for(const name of ['TEMP','TMP','LOCALAPPDATA','APPDATA']) assert.ok(real(process.env[name]).startsWith('F:/'));
const dependencies=real('F:/OCR/output/platform-specialized-agent-20261007/worktrees/ocr/frontend/node_modules');
assert.ok(dependencies.startsWith('F:/'));
const {chromium}=await import(pathToFileURL(`${dependencies}/@playwright/test/index.mjs`).href);
const ready=JSON.parse(fs.readFileSync(`${output}/ready.json`));
const scope=JSON.parse(fs.readFileSync(`${output}/scope.json`));
const save=(name,value)=>fs.writeFileSync(`${output}/${name}`,JSON.stringify(value,null,2)+'\n');
for(const name of ['profile','browser-cache','crash','downloads','screenshots']) fs.mkdirSync(`${output}/${name}`,{recursive:true});
const context=await chromium.launchPersistentContext(`${output}/profile`,{
  executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,
  viewport:{width:1366,height:900},acceptDownloads:true,downloadsPath:`${output}/downloads`,
  args:['--disable-gpu','--disable-gpu-compositing','--disable-breakpad',
    `--disk-cache-dir=${output}/browser-cache`,`--crash-dumps-dir=${output}/crash`],
});
const page=await context.newPage();
page.setDefaultTimeout(45000);
const errors=[];
page.on('pageerror',error=>errors.push(String(error)));
const base=`/api/projects/${ready.project_id}/screen-batches`;
const headers={Authorization:`Bearer ${ready.token}`};
let passed=false;
let body,batch,ordinaryBatch;
const cancelTimings=[];
try {
  await page.goto(`${ready.url}/#token=${ready.token}`);
  await page.getByLabel('当前项目').selectOption(ready.project_id);
  await page.getByRole('button',{name:'拍屏表格批量转 Excel',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await dialog.getByRole('checkbox',{name:'提高精度（耗时增加，仍需核对）',exact:true}).check();
  if(resume) {
    ({request:body,response:batch}=JSON.parse(fs.readFileSync(`${output}/precision-submission.json`)));
    ordinaryBatch=JSON.parse(fs.readFileSync(`${output}/ordinary-submission.json`)).response;
    const lookup=await page.request.get(`${ready.url}/api/projects/${ready.project_id}/screen-batch-requests/${body.request_id}`,{headers});
    assert.equal(lookup.status(),200);
    assert.equal((await lookup.json()).batch_id,batch.batch_id);
  } else {
  const [response]=await Promise.all([
    page.waitForResponse(response=>response.url()===ready.url+base&&response.request().method()==='POST'),
    dialog.getByRole('button',{name:'开始处理 240 张',exact:true}).click(),
  ]);
  assert.equal(response.status(),200,await response.text());
  body=response.request().postDataJSON();
  batch=await response.json();
  assert.deepEqual(new Set(body.version_ids),new Set(scope.version_ids));
  assert.equal(body.policy.batch_budget_seconds,86400);
  assert.equal(body.policy.budget_seconds,360);
  assert.equal(body.policy.technical_fallback_reserve_seconds,90);
  assert.equal(body.policy.engine_route,'business_language_v1');
  assert.equal(body.policy.upgrade_trigger,'failure_only');
  assert.equal(batch.policy.engine_route,'business_language_v1');
  assert.equal(batch.counts.total,240);
  assert.equal(batch.counts.pending,240);
  save('precision-submission.json',{request:body,response:batch});
  const replay=await page.request.post(ready.url+base,{headers,data:body});
  assert.equal(replay.status(),200);
  assert.equal((await replay.json()).batch_id,batch.batch_id);
  const changed=await page.request.post(ready.url+base,{headers,data:{...body,
    policy:{...body.policy,batch_budget_seconds:86401}}});
  assert.equal(changed.status(),409);
  await dialog.getByRole('checkbox',{name:'提高精度（耗时增加，仍需核对）',exact:true}).uncheck();
  const [ordinary]=await Promise.all([
    page.waitForResponse(value=>value.url()===ready.url+base&&value.request().method()==='POST'),
    dialog.getByRole('button',{name:'开始处理 240 张',exact:true}).click(),
  ]);
  assert.equal(ordinary.status(),200,await ordinary.text());
  const ordinaryBody=ordinary.request().postDataJSON();
  assert.deepEqual(ordinaryBody.policy,{primary_engine:'auto',mode:'auto'});
  assert.notEqual(ordinaryBody.request_id,body.request_id);
  ordinaryBatch=await ordinary.json();
  assert.equal(ordinaryBatch.policy.engine_route,'fixed');
  save('ordinary-submission.json',{request:ordinaryBody,response:ordinaryBatch});
  }
  // Stop the unstarted engineering batches; no coordinator or model ran.
  for(const current of [batch,ordinaryBatch]) {
    const action={action:'cancel',request_id:`precision-ui-cancel-${current.batch_id}`};
    const start=performance.now();
    const lookup=await page.request.post(`${ready.url}${base}/${current.batch_id}/action-lookup`,{headers,data:action,timeout:60000});
    assert.equal(lookup.status(),200);
    let receipt=await lookup.json();
    const recovered=receipt!==null;
    if(!recovered) {
      const cancel=await page.request.post(`${ready.url}${base}/${current.batch_id}/action`,{headers,data:action,timeout:60000});
      assert.equal(cancel.status(),200,await cancel.text());
      receipt=await cancel.json();
    }
    assert.equal(receipt.counts.cancelled,240);
    cancelTimings.push({batch_id:current.batch_id,original_action_recovered:recovered,seconds:(performance.now()-start)/1000});
  }
  await dialog.getByRole('button',{name:'刷新批次',exact:true}).click();
  await page.getByLabel('最近表格批次').selectOption(ordinaryBatch.batch_id);
  await dialog.locator('.screen-batch-progress strong').filter({hasText:'已取消'}).waitFor();
  await dialog.getByRole('checkbox',{name:'提高精度（耗时增加，仍需核对）',exact:true}).check();
  await page.screenshot({path:`${output}/screenshots/precision-entry.png`,fullPage:false});
  assert.deepEqual(errors,[]);
  passed=true;
  save('browser.json',{format:'precision-route-real-ui-admission-v1',passed:true,
    images:240,precision_batch_id:batch.batch_id,ordinary_batch_id:ordinaryBatch.batch_id,
    original_batch_budget_seconds:86400,replay_kept_batch:true,changed_budget_rejected:true,
    ordinary_request_preserved:true,unstarted_batches_cancelled:true,model_calls:0,agent_calls:0,
    resumed_original_identity:resume,actual_control_seconds:cancelTimings,
    screenshot_sha256:crypto.createHash('sha256').update(fs.readFileSync(`${output}/screenshots/precision-entry.png`)).digest('hex'),
    recognition_or_review_relief_qualification:false,page_errors:errors});
  console.log(JSON.stringify({passed:true,images:240,model_calls:0,agent_calls:0}));
} catch(error) {
  save('browser-failure.json',{passed:false,error:String(error),stack:error.stack,page_errors:errors});
  if(!page.isClosed()) {
    save('failure-page.json',{body:await page.locator('body').innerText()});
    await page.screenshot({path:`${output}/screenshots/failure.png`,fullPage:false});
  }
  throw error;
} finally {
  await context.close();
  save('browser-stopped.json',{browser_stopped:true,passed});
}
