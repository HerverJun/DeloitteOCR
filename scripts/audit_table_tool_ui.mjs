import {chromium,expect} from '../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
const out=path.resolve(process.argv[2]),seed=JSON.parse(await fs.readFile(path.join(out,'seed.json'),'utf8'));
const checks=[],errors=[];
const api=async url=>{const response=await fetch(seed.base+'/api'+url,{headers:{Authorization:'Bearer '+seed.token}});if(!response.ok)throw Error(await response.text());return response.json();};
const browser=await chromium.launch({channel:'msedge',headless:true,args:['--disable-gpu','--disable-gpu-compositing']});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
page.on('pageerror',e=>errors.push(String(e)));
try {
  await page.goto(seed.base+'/#token='+seed.token);
  await page.getByLabel('选择文档',{exact:true}).selectOption(seed.document);
  await page.getByRole('region',{name:'文档与页面',exact:true}).getByRole('button',{name:/第 1 页/}).first().click();
  const queue=page.getByRole('region',{name:'文档复核队列',exact:true});
  await expect(queue).toContainText('PDF 表格提取失败');
  await queue.getByRole('button',{name:/PDF 表格提取失败/}).click();
  const status=page.getByRole('status',{name:'PDF 表格提取状态',exact:true});
  await expect(status).toContainText('模拟表格工具超时');
  checks.push({name:'tool failure appears in document queue and structure status',passed:true});
  await page.screenshot({path:path.join(out,'tool-failure.png'),fullPage:true,animations:'disabled'});
  await status.getByRole('button',{name:'重新提取 PDF 表格',exact:true}).click();
  await expect(status).toContainText('未检测到有线表格',{timeout:20000});
  await expect(status).not.toContainText('模拟表格工具超时');
  await expect(queue).not.toContainText('PDF 表格提取失败',{timeout:10000});
  checks.push({name:'auxiliary-only retry reaches valid empty detection and clears failure queue',passed:true});
  const after=await api(`/results/${seed.result}`);
  expect(after.revision).toBe(seed.before.revision);
  expect(after.edited).toEqual(seed.before.edited);
  expect(after.original).toEqual(seed.before.original);
  checks.push({name:'manual content and result revision remain unchanged after retry',passed:true});
  await page.reload();
  await page.getByRole('region',{name:'文档与页面',exact:true}).getByRole('button',{name:/第 1 页/}).first().click();
  expect((await api(`/results/${seed.result}/structure`)).table_tool.state).toBe('empty');
  checks.push({name:'auxiliary status persists across UI reload',passed:true});
  expect(errors).toEqual([]);
} catch(e){errors.push(e.stack||String(e));process.exitCode=1;}
finally{await browser.close();await fs.writeFile(path.join(out,'report.json'),JSON.stringify({passed:!errors.length,checks,errors,browser_closed:true},null,2));}
