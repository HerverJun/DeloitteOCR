// Full Portal on actual OCR outputs; scripted engineering operations only.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
const host=process.argv[2]?.replaceAll('\\','/'), output=process.argv[3]?.replaceAll('\\','/');
assert.ok(host?.startsWith('F:/OCR/output/') && output?.startsWith(host+'/'));
assert.ok(!fs.existsSync(output));fs.mkdirSync(output,{recursive:true});
const real=value=>fs.realpathSync(value).replaceAll('\\','/');
assert.ok(real(output).startsWith('F:/'));
for(const name of ['TEMP','TMP','LOCALAPPDATA','APPDATA']) assert.ok(real(process.env[name]).startsWith('F:/'));
const dependencies=real('F:/OCR/output/platform-specialized-agent-20261007/worktrees/ocr/frontend/node_modules');
assert.ok(dependencies.startsWith('F:/'));
const {chromium}=await import(pathToFileURL(`${dependencies}/@playwright/test/index.mjs`).href);
const ready=JSON.parse(fs.readFileSync(`${host}/ready.json`)),scope=JSON.parse(fs.readFileSync(`${host}/scope.json`));
const save=(name,value)=>fs.writeFileSync(`${output}/${name}`,JSON.stringify(value,null,2)+'\n');
for(const name of ['profile','browser-cache','crash','downloads','screenshots']) fs.mkdirSync(`${output}/${name}`,{recursive:true});
const context=await chromium.launchPersistentContext(`${output}/profile`,{
  executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,
  viewport:{width:1440,height:1000},acceptDownloads:true,downloadsPath:fs.realpathSync(`${output}/downloads`),
  args:['--disable-gpu','--disable-gpu-compositing','--disable-breakpad',
    `--disk-cache-dir=${output}/browser-cache`,`--crash-dumps-dir=${output}/crash`],
});
const page=await context.newPage();page.setDefaultTimeout(45000);
const errors=[],receipts=[],downloads=[],screenshots=[];
page.on('pageerror',error=>errors.push(String(error)));
const platformBase=`${ready.platform}/api/platform/v1/workspaces/${ready.workspace_id}`;
const base=`${platformBase}/ocr/screen-batches/${ready.batch_id}`;
const nativeBase=`${ready.native}/api/projects/${ready.project_id}/screen-batches/${ready.batch_id}`;
const headers={Origin:ready.platform},nativeHeaders={Authorization:`Bearer ${ready.token}`};
const dialog=page.getByRole('dialog');
const url=batch=>`${ready.platform}/apps/ocr/workflow?workspace=${ready.workspace_id}&set=${ready.asset_set_id}&project=${ready.project_id}${batch?'&screen_batch='+batch:''}`;
let passed=false;
async function getJSON(address,native=false) {
  const response=await page.request.get(address,{headers:native?nativeHeaders:headers});
  assert.equal(response.status(),200,await response.text());return response.json();
}
async function detail(name) {
  const value=await getJSON(`${nativeBase}/items/${scope.selected_version_id}`,true);
  save(`${name}-detail.json`,value);return value;
}
async function openReview() {
  await page.locator('.ocr-screen-batch tbody tr').filter({hasText:scope.selected_source_name})
    .getByRole('button',{name:'核对原图与结果',exact:true}).click();
  await dialog.getByLabel(`业务说明${scope.metadata_index+1}复核值`).waitFor();
  await dialog.getByRole('button',{name:'保留当前结果',exact:true}).waitFor();
}
async function action(label,expected=200) {
  const [response]=await Promise.all([
    page.waitForResponse(value=>value.url()===`${base}/action`&&value.request().method()==='POST'),
    dialog.getByRole('button',{name:label,exact:true}).click(),
  ]);
  const body=response.request().postDataJSON(),reply=await response.json();
  receipts.push({label,status:response.status(),request:body,response:reply});save('action-receipts.json',receipts);
  assert.equal(response.status(),expected,JSON.stringify(reply));
  if(expected===200) await dialog.waitFor({state:'hidden'});
  return {body,reply};
}
async function download(name,expectedFiles,includeReview) {
  await page.getByLabel('包含待复核文件').setChecked(includeReview);
  const before=await getJSON(`${nativeBase}/delivery-snapshot`,true);assert.equal(before.fresh,true);
  const selectedBefore=await detail(name+'-before');
  const [response]=await Promise.all([
    page.waitForResponse(value=>value.url()===`${base}/export`&&value.request().method()==='POST'),
    page.getByRole('button',{name:'下载逐图 Excel ZIP',exact:true}).click(),
  ]);
  assert.equal(response.status(),201,'Portal export must create/recover a managed bundle');const bundle=await response.json();
  const file=bundle.files.find(file=>file.name.endsWith('.zip'));assert.ok(file);
  const href=`/api/platform/v1/workspaces/${ready.workspace_id}/artifacts/${bundle.bundle_id}/files/${encodeURIComponent(file.name)}`;
  const link=page.locator(`a[href="${href}"]`);await link.waitFor();
  await page.getByText('当前成果',{exact:true}).waitFor();
  const [event]=await Promise.all([page.waitForEvent('download'),link.click()]);
  const path=`${output}/downloads/${name}.zip`;await event.saveAs(path);
  const after=await getJSON(`${nativeBase}/delivery-snapshot`,true);assert.deepEqual(after,before);
  save(`${name}-snapshot.json`,{before,after,selected:selectedBefore,request:response.request().postDataJSON(),bundle});
  downloads.push({name,path,expected_files:expectedFiles,bundle_id:bundle.bundle_id,
    sha256:crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex')});save('downloads.json',downloads);
  return bundle;
}
async function shot(name) {
  const path=`${output}/screenshots/${name}.png`;await page.screenshot({path,fullPage:false});
  screenshots.push({path,sha256:crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex')});
}
try {
  await page.goto(url(ready.batch_id));
  await page.getByRole('heading',{name:'进度与集中复核',exact:true}).waitFor();
  const initial=await detail('initial'), original=initial.result.original;
  const before=initial.business_metadata[scope.metadata_index];
  const oldBundle=await download('initial-all-pending',40,true);
  await openReview();
  const metadata=dialog.getByLabel(`业务说明${scope.metadata_index+1}复核值`);
  const cellInput=dialog.getByLabel('表1行1列1',{exact:true});
  const cell=initial.result.edited.tables[0].cells.find(value=>value.row===0&&value.column===0);
  const corrected=cell.text+'［平台工程复核副本］';
  await dialog.getByText('查看识别来源',{exact:true}).first().click();
  await dialog.getByRole('button',{name:`选用辅助识别原文 ${scope.metadata_index+1}`,exact:true}).click();
  assert.equal(await metadata.inputValue(),before.transcription_evidence.auxiliary_text);
  await cellInput.fill(corrected);
  assert.equal(await dialog.getByRole('button',{name:'稍后复核',exact:true}).isDisabled(),true);
  await dialog.getByRole('button',{name:'Close',exact:true}).click();
  await dialog.getByText('请先保存或清除当前表格和业务说明修改，再关闭复核窗口',{exact:true}).waitFor();
  assert.equal(await cellInput.inputValue(),corrected);
  await metadata.scrollIntoViewIfNeeded();await shot('01-actual-two-readings-and-protected-draft');
  const retained=await action('保留当前结果');
  const pending=await detail('auxiliary-pending');
  assert.equal(pending.status,'needs_review');assert.equal(pending.human_confirmed,false);
  assert.equal(pending.business_metadata[scope.metadata_index].text,before.transcription_evidence.auxiliary_text);
  assert.equal(pending.result.edited.tables[0].cells.find(value=>value.row===0&&value.column===0).text,corrected);
  assert.deepEqual(pending.result.original,original);
  const replay=await page.request.post(`${base}/action`,{headers,data:retained.body});assert.equal(replay.status(),200);
  assert.deepEqual((await detail('replayed')).result,pending.result);
  const freshness=await getJSON(`${platformBase}/artifacts/${oldBundle.bundle_id}/freshness`);
  assert.equal(freshness.current,false);save('initial-bundle-now-historical.json',freshness);
  await page.getByRole('link',{name:'下载历史 Excel ZIP',exact:true}).waitFor();
  await download('auxiliary-pending-current',40,true);
  await openReview();
  const manualMetadata=before.text+'［平台工程手改副本］';
  await metadata.fill(manualMetadata);await action('保留当前结果');
  const manual=await detail('manual-pending');
  assert.equal(manual.business_metadata[scope.metadata_index].source,'human_edit');
  assert.equal(manual.business_metadata[scope.metadata_index].text,manualMetadata);
  await openReview();await action('保存并确认本图');
  const accepted=await detail('accepted');assert.equal(accepted.human_confirmed,true);
  await download('accepted-partial-current',1,false);
  await openReview();
  const edited=structuredClone(accepted.result.edited),concurrent=corrected+'［并发平台工程编辑］';
  edited.tables[0].cells.find(value=>value.row===0&&value.column===0).text=concurrent;
  const external=await page.request.put(`${ready.native}/api/results/${accepted.result.id}`,
    {headers:nativeHeaders,data:{edited,revision:accepted.result.revision}});
  assert.equal(external.status(),200,await external.text());save('concurrent-editor-response.json',await external.json());
  await metadata.fill(manualMetadata+'［未保存草稿］');await action('保留当前结果',409);
  assert.equal(await metadata.inputValue(),manualMetadata+'［未保存草稿］');
  const stale=await page.request.post(`${base}/export`,{headers,data:{include_pending_review:true}});
  assert.equal(stale.status(),409);save('stale-export-rejected.json',{status:stale.status(),response:await stale.json()});
  await metadata.scrollIntoViewIfNeeded();await shot('02-stale-review-refused');
  await dialog.getByRole('button',{name:'重新加载当前复核版本',exact:true}).click();
  await page.waitForFunction(({name,text})=>Array.from(document.querySelectorAll('textarea')).some(node=>node.getAttribute('aria-label')===name&&node.value===text),
    {name:`业务说明${scope.metadata_index+1}复核值`,text:manualMetadata});
  await action('保留当前结果');
  const rebound=await detail('rebound');
  assert.equal(rebound.result.edited.tables[0].cells.find(value=>value.row===0&&value.column===0).text,concurrent);
  assert.equal(rebound.human_confirmed,false);
  await openReview();await action('稍后复核');assert.equal((await detail('deferred')).review_state,'deferred');
  await openReview();await action('保存并确认本图');
  await download('final-partial-current',1,false);await download('final-all-current',40,true);
  await openReview();assert.equal(await metadata.inputValue(),manualMetadata);
  await metadata.scrollIntoViewIfNeeded();await shot('03-reopened-current-platform-result');
  await dialog.getByRole('button',{name:'Close',exact:true}).click();
  const final=await detail('final');assert.deepEqual(final.result.original,original);
  // Full Portal precision entry, real bridge persistence and idempotent lookup.
  // No coordinator/queue exists, so this new request is cancelled before work.
  await page.goto(url());
  await page.getByLabel('批次处理策略').click();
  await page.getByText('提高精度（耗时增加，仍需核对）',{exact:true}).click();
  const submitUrl=`${platformBase}/ocr/screen-batches`;
  const [submission]=await Promise.all([
    page.waitForResponse(value=>value.url()===submitUrl&&value.request().method()==='POST'),
    page.getByRole('button',{name:'开始整批转 Excel',exact:true}).click(),
  ]);
  assert.equal(submission.status(),201,'Portal submission must return its durable batch identity');
  const submissionBody=submission.request().postDataJSON(),submitted=await submission.json();
  assert.equal(submissionBody.policy.engine_route,'business_language_v1');
  assert.equal(submissionBody.policy.batch_budget_seconds,36000);
  const nativeSubmitted=await getJSON(`${ready.native}/api/projects/${ready.project_id}/screen-batches/${submitted.batch_id}`,true);
  for(const [key,value] of Object.entries(submissionBody.policy)) assert.deepEqual(nativeSubmitted.policy[key],value);
  assert.equal(nativeSubmitted.counts.pending,40);
  const repeated=await page.request.post(submitUrl,{headers,data:submissionBody});assert.equal(repeated.status(),201);
  assert.equal((await repeated.json()).batch_id,submitted.batch_id);
  const lookup=await getJSON(`${ready.native}/api/projects/${ready.project_id}/screen-batch-requests/${submissionBody.request_id}`,true);
  assert.equal(lookup.batch_id,submitted.batch_id);
  save('precision-submission-before-cancel.json',{request:submissionBody,response:submitted,native:nativeSubmitted,lookup});
  await page.getByRole('button',{name:/^取\s*消$/}).click();
  await page.getByText('取消 40',{exact:true}).waitFor();
  const cancelled=await getJSON(`${ready.native}/api/projects/${ready.project_id}/screen-batches/${submitted.batch_id}`,true);
  assert.equal(cancelled.counts.cancelled,40);assert.deepEqual(cancelled.policy,nativeSubmitted.policy);
  save('precision-submission.json',{request:submissionBody,response:submitted,native:nativeSubmitted,lookup,cancelled});
  await shot('04-accuracy-request-cancelled-with-original-policy');
  assert.deepEqual(errors,[]);passed=true;
  save('browser.json',{format:'actual-platform-scripted-review-ui-v1',passed,host,images:40,
    selected_version_id:scope.selected_version_id,receipts,downloads,screenshots,page_errors:errors,
    full_production_portal:true,ocr_results_injected:false,actual_two_readings_selected:true,
    manual_cell_and_metadata_persisted:true,draft_close_and_defer_protected:true,action_replay_idempotent:true,
    stale_editor_and_export_rejected:true,historical_zip_and_current_freshness_verified:true,
    deliberate_reload_rebind_preserves_concurrent_edit:true,precision_submission_native_persistence_verified:true,
    precision_submission_cancelled_without_recognition:true,model_calls:0,agent_calls:0,
    scripted_review_actions:true,human_quality_or_relief_qualification:false,
    expected_cell:{table:0,row:0,column:0,initial:cell.text,pending:corrected,final:concurrent},
    expected_metadata:{index:scope.metadata_index,initial:before.text,auxiliary:before.transcription_evidence.auxiliary_text,manual:manualMetadata}});
  console.log(JSON.stringify({passed,images:40,exports:downloads.length,precision_submission:submitted.batch_id,model_calls:0}));
} catch(error) {
  save('browser-failure.json',{passed:false,error:String(error),stack:error.stack,body:await page.locator('body').innerText(),page_errors:errors,receipts,downloads});
  if(!page.isClosed()) await shot('failure');throw error;
} finally {
  await context.close();save('browser-stopped.json',{browser_stopped:true,passed});
}
