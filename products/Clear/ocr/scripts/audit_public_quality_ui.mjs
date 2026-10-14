import {chromium,expect} from '../frontend/node_modules/@playwright/test/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
const out=path.resolve(process.argv[2]);
const seed=JSON.parse(await fs.readFile(path.join(out,'seed.json'),'utf8'));
const checks=[],errors=[];
const api=async(url,method='GET',body)=>{
  const response=await fetch(seed.base+'/api'+url,{method,headers:{Authorization:'Bearer '+seed.token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  if(!response.ok)throw Error(await response.text());return response.json();
};
const browser=await chromium.launch({channel:'msedge',headless:true,args:['--disable-gpu','--disable-gpu-compositing']});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
page.on('pageerror',e=>errors.push(String(e)));
try {
  await page.goto(seed.base+'/#token='+seed.token);
  await page.getByLabel('选择文档',{exact:true}).selectOption(seed.document);
  await page.getByRole('region',{name:'文档与页面',exact:true}).getByRole('button',{name:/第 1 页/}).first().click();
  const issues=page.getByRole('region',{name:'页面内容核对',exact:true});
  await expect(issues).toContainText('此区域未识别出文字');
  const before=await api(`/results/${seed.result}/document-conflicts`);
  expect(before.conflicts.length).toBeGreaterThan(0);
  expect(before.conflicts.every(c=>c.reason==='region_no_text'&&!c.reviewed)).toBe(true);
  await issues.getByRole('button',{name:'查看原图区域',exact:true}).first().click();
  await page.screenshot({path:path.join(out,'empty-region-review.png'),fullPage:true,animations:'disabled'});
  checks.push({name:'empty OCR regions remain visible and navigate to original image',passed:true});
  await issues.getByRole('button',{name:'已核对当前保存内容',exact:true}).first().click();
  await expect(issues.getByRole('button',{name:'当前内容已核对',exact:true})).toHaveCount(1);
  const reviewed=await api(`/results/${seed.result}/document-conflicts`);
  expect(reviewed.conflicts.filter(c=>c.reviewed)).toHaveLength(1);
  checks.push({name:'explicit acknowledgement resolves only the selected region',passed:true});
  const result=await api(`/results/${seed.result}`);
  await api(`/results/${seed.result}`,'PUT',{revision:result.revision,edited:{...result.edited,text:result.edited.text+'\nReview changed content'}});
  await page.reload();
  await page.getByRole('region',{name:'文档与页面',exact:true}).getByRole('button',{name:/第 1 页/}).first().click();
  await expect(issues.getByRole('button',{name:'当前内容已核对',exact:true})).toHaveCount(0);
  expect((await api(`/results/${seed.result}/document-conflicts`)).conflicts.every(c=>!c.reviewed)).toBe(true);
  checks.push({name:'editing content invalidates previous acknowledgements',passed:true});
  expect(errors).toEqual([]);
} catch(e) { errors.push(e.stack||String(e));process.exitCode=1; }
finally {
  await browser.close();
  await fs.writeFile(path.join(out,'report.json'),JSON.stringify({passed:errors.length===0,checks,errors,browser_closed:true,scope:'scripted UI checks; no human efficiency or quality claim'},null,2));
}
