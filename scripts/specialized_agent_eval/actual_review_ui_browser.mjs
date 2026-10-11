// Production UI on a byte copy of actual recognition; scripted engineering review.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';

const host=process.argv[2]?.replaceAll('\\','/');
const output=process.argv[3]?.replaceAll('\\','/')||host;
assert.ok(output?.startsWith('F:/OCR/output/'));
if(output!==host) {assert.ok(!fs.existsSync(output));fs.mkdirSync(output,{recursive:true});}
const real=value=>fs.realpathSync(value).replaceAll('\\','/');
assert.ok(real(output).startsWith('F:/'));
for(const name of ['TEMP','TMP','LOCALAPPDATA','APPDATA']) assert.ok(real(process.env[name]).startsWith('F:/'));
const dependencies=real('F:/OCR/output/platform-specialized-agent-20261007/worktrees/ocr/frontend/node_modules');
assert.ok(dependencies.startsWith('F:/'));
const {chromium}=await import(pathToFileURL(`${dependencies}/@playwright/test/index.mjs`).href);
const ready=JSON.parse(fs.readFileSync(`${host}/ready.json`));
const scope=JSON.parse(fs.readFileSync(`${host}/scope.json`));
const save=(name,value)=>fs.writeFileSync(`${output}/${name}`,JSON.stringify(value,null,2)+'\n');
for(const name of ['profile','browser-cache','crash','downloads','screenshots']) fs.mkdirSync(`${output}/${name}`,{recursive:true});
const context=await chromium.launchPersistentContext(`${output}/profile`,{
  executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,
  viewport:{width:1366,height:900},acceptDownloads:true,downloadsPath:fs.realpathSync(`${output}/downloads`),
  args:['--disable-gpu','--disable-gpu-compositing','--disable-breakpad',
    `--disk-cache-dir=${output}/browser-cache`,`--crash-dumps-dir=${output}/crash`],
});
const page=await context.newPage();page.setDefaultTimeout(45000);
const errors=[],receipts=[],downloads=[],screenshots=[];
page.on('pageerror',error=>errors.push(String(error)));
const headers={Authorization:`Bearer ${ready.token}`};
const base=`${ready.url}/api/projects/${ready.project_id}/screen-batches/${ready.batch_id}`;
const itemUrl=`${base}/items/${scope.selected_version_id}`;
const dialog=page.getByRole('dialog');
const manualCellSuffix='［工程复核副本］';
let passed=false;

async function detail(name) {
  const response=await page.request.get(itemUrl,{headers});
  assert.equal(response.status(),200,await response.text());
  const value=await response.json();save(`${name}-detail.json`,value);return value;
}
async function action(label,expected=200) {
  const [response]=await Promise.all([
    page.waitForResponse(value=>value.url()===`${base}/action`&&value.request().method()==='POST'),
    dialog.getByRole('button',{name:label,exact:true}).click(),
  ]);
  const body=response.request().postDataJSON(),reply=await response.json();
  receipts.push({label,status:response.status(),request:body,response:reply});
  save('action-receipts.json',receipts);
  assert.equal(response.status(),expected,JSON.stringify(reply));
  await dialog.getByRole('button',{name:label,exact:true}).waitFor({state:'visible'});
  await page.waitForFunction(text=>Array.from(document.querySelectorAll('button')).some(button=>button.textContent===text&&!button.disabled),label);
  return {body,reply};
}
async function download(name,expectedFiles) {
  const snapshotBefore=await page.request.get(`${base}/delivery-snapshot`,{headers});
  assert.equal(snapshotBefore.status(),200);
  const before=await snapshotBefore.json();assert.equal(before.fresh,true);
  const selectedBefore=await detail(`${name}-before`);
  const [response,file]=await Promise.all([
    page.waitForResponse(value=>value.url()===`${base}/export`&&value.request().method()==='POST'),
    page.waitForEvent('download'),
    dialog.getByRole('button',{name:'下载逐图 Excel ZIP',exact:true}).click(),
  ]);
  assert.equal(response.status(),200);
  const path=`${output}/downloads/${name}.zip`;
  await file.saveAs(path);
  const snapshotAfter=await page.request.get(`${base}/delivery-snapshot`,{headers});
  assert.equal(snapshotAfter.status(),200);
  const after=await snapshotAfter.json();assert.deepEqual(after,before);
  save(`${name}-snapshot.json`,{before,after,selected:selectedBefore,
    request:response.request().postDataJSON()});
  downloads.push({name,path,expected_files:expectedFiles,
    sha256:crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex')});
  save('downloads.json',downloads);
}
async function shot(name) {
  const path=`${output}/screenshots/${name}.png`;
  await page.screenshot({path,fullPage:false});
  screenshots.push({path,sha256:crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex')});
}
try {
  await page.goto(`${ready.url}/#token=${ready.token}`);
  await page.getByLabel('当前项目').selectOption(ready.project_id);
  await page.getByRole('button',{name:'拍屏表格批量转 Excel',exact:true}).click();
  await page.getByLabel('最近表格批次').selectOption(ready.batch_id);
  await dialog.getByLabel('下载时包含待核对结果').check();
  await download('initial-all-pending',scope.images);
  if(scope.selected_position>=30) await dialog.getByRole('button',{name:'下一页',exact:true}).click();
  await dialog.locator('.screen-item').filter({hasText:scope.selected_source_name}).click();
  const metadata=dialog.getByLabel(`业务说明${scope.metadata_index+1}复核值`);
  await metadata.waitFor();
  const initial=await detail('initial');
  const original=initial.result.original;
  const before=initial.business_metadata[scope.metadata_index];
  await dialog.getByRole('button',{name:'比较候选差异',exact:true}).click();
  await dialog.getByRole('status').filter({hasText:'尚未采用或修改结果'}).waitFor();
  assert.deepEqual((await detail('after-comparison')).result,initial.result);
  await dialog.getByLabel('核对图片范围').selectOption('original');
  await dialog.locator('.zoom-tools button').filter({hasText:'100%'}).click();
  await dialog.getByRole('button',{name:'放大',exact:true}).click();
  await dialog.getByLabel('业务说明复核').getByText('查看两版原文及识别来源').first().click();
  await dialog.getByRole('button',{name:`选用辅助识别原文 ${scope.metadata_index+1}`,exact:true}).click();
  assert.equal(await metadata.inputValue(),before.transcription_evidence.auxiliary_text);
  await dialog.getByLabel('核对表格').selectOption('0');
  await dialog.getByLabel('核对行').fill('1');
  await dialog.getByLabel('核对列').fill('1');
  const cell=initial.result.edited.tables[0].cells.find(value=>value.row===0&&value.column===0);
  assert.ok(cell);
  const corrected=cell.text+manualCellSuffix;
  await dialog.getByLabel('当前单元格内容').fill(corrected);
  assert.equal(await page.getByLabel('最近表格批次').isDisabled(),true);
  assert.equal(await dialog.getByRole('button',{name:'下载逐图 Excel ZIP',exact:true}).isDisabled(),true);
  assert.equal(await dialog.getByRole('button',{name:'稍后核对',exact:true}).isDisabled(),true);
  await dialog.locator('.screen-item').filter({hasText:scope.selected_source_name}).click();
  await dialog.getByRole('alert').filter({hasText:'再切换图片'}).waitFor();
  assert.equal(await metadata.inputValue(),before.transcription_evidence.auxiliary_text);
  await dialog.getByRole('button',{name:'关闭批量表格处理',exact:true}).click();
  await dialog.getByRole('alert').filter({hasText:'关闭'}).waitFor();
  assert.equal(await dialog.getByLabel('当前单元格内容').inputValue(),corrected);
  await metadata.scrollIntoViewIfNeeded();await shot('01-two-readings-and-draft');
  const retained=await action('保留当前内容，仍待核对');
  const pending=await detail('auxiliary-pending');
  assert.equal(pending.status,'needs_review');assert.equal(pending.human_confirmed,false);
  assert.equal(pending.business_metadata[scope.metadata_index].text,before.transcription_evidence.auxiliary_text);
  assert.equal(pending.business_metadata[scope.metadata_index].human_confirmed,false);
  assert.equal(pending.result.edited.tables[0].cells.find(value=>value.row===0&&value.column===0).text,corrected);
  assert.deepEqual(pending.result.original,original);
  const replay=await page.request.post(`${base}/action`,{headers,data:retained.body});
  assert.equal(replay.status(),200);assert.deepEqual((await detail('after-action-replay')).result,pending.result);
  const lookup=await page.request.post(`${base}/action-lookup`,{headers,data:retained.body});
  assert.equal(lookup.status(),200);
  await download('auxiliary-pending-current',scope.images);
  const manualMetadata=before.text+'［工程手改副本］';
  await metadata.fill(manualMetadata);
  await action('保留当前内容，仍待核对');
  const manual=await detail('manual-pending');
  assert.equal(manual.business_metadata[scope.metadata_index].text,manualMetadata);
  assert.equal(manual.business_metadata[scope.metadata_index].source,'human_edit');
  assert.equal(manual.human_confirmed,false);
  await action('保存并标记核对完成');
  const accepted=await detail('accepted');
  assert.equal(accepted.status,'completed');assert.equal(accepted.human_confirmed,true);
  assert.equal(accepted.business_metadata[scope.metadata_index].human_confirmed,true);
  assert.deepEqual(accepted.result.original,original);
  await dialog.getByLabel('下载时包含待核对结果').uncheck();
  await download('accepted-partial-current',1);
  await metadata.scrollIntoViewIfNeeded();await shot('02-scripted-confirmation');
  // A second real HTTP editor changes the result. The first UI must not
  // overwrite it or silently export its stale review binding.
  const edited=structuredClone(accepted.result.edited);
  const concurrent=corrected+'［并发工程编辑］';
  edited.tables[0].cells.find(value=>value.row===0&&value.column===0).text=concurrent;
  const external=await page.request.put(`${ready.url}/api/results/${accepted.result.id}`,{
    headers,data:{edited,revision:accepted.result.revision}});
  assert.equal(external.status(),200,await external.text());
  save('concurrent-editor-response.json',await external.json());
  await metadata.fill(manualMetadata+'［未保存草稿］');
  await action('保留当前内容，仍待核对',409);
  assert.equal(await metadata.inputValue(),manualMetadata+'［未保存草稿］');
  const stale=await page.request.post(`${base}/export`,{headers,data:{include_pending_review:true}});
  assert.equal(stale.status(),409);save('stale-export-rejected.json',{status:stale.status(),response:await stale.json()});
  await metadata.scrollIntoViewIfNeeded();await shot('03-stale-review-refused');
  await dialog.getByRole('button',{name:'重新加载当前复核版本',exact:true}).click();
  await page.waitForFunction(({name,text})=>Array.from(document.querySelectorAll('textarea')).some(node=>node.getAttribute('aria-label')===name&&node.value===text),{
    name:`业务说明${scope.metadata_index+1}复核值`,text:manualMetadata});
  await action('保留当前内容，仍待核对');
  const rebound=await detail('rebound');
  assert.equal(rebound.status,'needs_review');assert.equal(rebound.human_confirmed,false);
  assert.equal(rebound.result.edited.tables[0].cells.find(value=>value.row===0&&value.column===0).text,concurrent);
  await action('稍后核对');
  assert.equal((await detail('deferred')).review_state,'deferred');
  await action('保存并标记核对完成');
  const final=await detail('final');
  assert.equal(final.status,'completed');assert.equal(final.human_confirmed,true);
  assert.deepEqual(final.result.original,original);
  await download('final-partial-current',1);
  await dialog.getByLabel('下载时包含待核对结果').check();
  await download('final-all-current',scope.images);
  await dialog.getByRole('button',{name:'关闭面板，后台继续',exact:true}).click();
  await page.getByRole('button',{name:'拍屏表格批量转 Excel',exact:true}).click();
  await dialog.locator('.screen-item').filter({hasText:scope.selected_source_name}).click();
  await metadata.waitFor();assert.equal(await metadata.inputValue(),manualMetadata);
  await metadata.scrollIntoViewIfNeeded();await shot('04-reopened-current-version');
  assert.deepEqual(errors,[]);passed=true;
  save('browser.json',{format:'actual-recognition-scripted-review-ui-v1',passed:true,
    source_run:scope.source_run,host,images:scope.images,selected_version_id:scope.selected_version_id,
    receipts,downloads,screenshots,page_errors:errors,original_recognition_preserved:true,
    actual_two_readings_selected:true,manual_cell_and_metadata_persisted:true,
    draft_navigation_and_export_protected:true,action_replay_idempotent:true,
    stale_editor_and_export_rejected:true,deliberate_reload_rebind_preserves_concurrent_edit:true,
    pending_partial_and_current_full_exports:true,model_calls:0,agent_calls:0,
    scripted_review_actions:true,human_quality_or_relief_qualification:false,
    expected_cell:{table:0,row:0,column:0,initial:cell.text,pending:corrected,final:concurrent},
    expected_metadata:{index:scope.metadata_index,initial:before.text,
      auxiliary:before.transcription_evidence.auxiliary_text,manual:manualMetadata}});
  console.log(JSON.stringify({passed:true,images:scope.images,exports:downloads.length,model_calls:0,agent_calls:0}));
} catch(error) {
  save('browser-failure.json',{passed:false,error:String(error),stack:error.stack,
    body:await page.locator('body').innerText(),page_errors:errors,receipts,downloads});
  if(!page.isClosed()) await shot('failure');
  throw error;
} finally {
  await context.close();save('browser-stopped.json',{browser_stopped:true,passed});
}
