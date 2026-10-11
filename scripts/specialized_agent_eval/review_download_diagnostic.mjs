// Controlled browser-only diagnostic; no review edits and no source changes.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const host=process.argv[2],mode=process.argv[3];
const out=`${host}/download-diagnostic-${mode}`;
assert.ok(!fs.existsSync(out));fs.mkdirSync(out);
const ready=JSON.parse(fs.readFileSync(`${host}/ready.json`));
const deps=fs.realpathSync('F:/OCR/output/platform-specialized-agent-20261007/worktrees/ocr/frontend/node_modules');
const {chromium}=await import(pathToFileURL(`${deps}/@playwright/test/index.mjs`).href);
const context=await chromium.launchPersistentContext(`${out}/profile`,{
  executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,
  viewport:{width:1366,height:900},downloadsPath:`${out}/downloads`,acceptDownloads:true,
  args:['--disable-gpu','--disable-gpu-compositing','--disable-breakpad',`--disk-cache-dir=${out}/cache`,`--crash-dumps-dir=${out}/crash`]});
const page=await context.newPage(),events=[];
const cdp=await context.newCDPSession(page);
cdp.on('Browser.downloadProgress',value=>events.push(value));
await cdp.send('Browser.setDownloadBehavior',{behavior:'allowAndName',downloadPath:mode==='native-path'?`${out}/downloads`.replaceAll('/','\\'):`${out}/downloads`,eventsEnabled:true});
await page.addInitScript(mode=>{
  window.__revokes=[];const revoke=URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL=url=>{window.__revokes.push({at:performance.now(),url});if(mode!=='no-revoke')revoke(url);};
},mode);
try {
  await page.goto(`${ready.url}/#token=${ready.token}`);
  await page.getByLabel('当前项目').selectOption(ready.project_id);
  await page.getByRole('button',{name:'拍屏表格批量转 Excel',exact:true}).click();
  await page.getByLabel('最近表格批次').selectOption(ready.batch_id);
  await page.getByLabel('下载时包含待核对结果').check();
  const [file]=await Promise.all([page.waitForEvent('download'),page.getByRole('button',{name:'下载逐图 Excel ZIP',exact:true}).click()]);
  let failure=await file.failure();
  if(!failure)await file.saveAs(`${out}/actual.zip`);
  const revokes=await page.evaluate(()=>window.__revokes);
  const downloadPage=await context.newPage();await downloadPage.goto('chrome://downloads/');
  await downloadPage.waitForFunction(()=>document.querySelector('downloads-manager')?.shadowRoot?.querySelector('downloads-item'));
  const browserDownloads=await downloadPage.evaluate(()=>{
    const manager=document.querySelector('downloads-manager');
    const item=manager?.shadowRoot?.querySelector('downloads-item');
    return {items:manager?.items_,item:item?.data,text:item?.shadowRoot?.textContent};
  });
  fs.writeFileSync(`${out}/result.json`,JSON.stringify({mode,failure,events,revokes,browserDownloads},(key,value)=>typeof value==='bigint'?String(value):value,2));
  console.log({mode,failure,events});
} finally {await context.close();}
