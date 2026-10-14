import { chromium, expect } from "../frontend/node_modules/@playwright/test/index.mjs";
import fs from "node:fs/promises";
import path from "node:path";
const out = path.resolve(process.argv[2]);
const seed = JSON.parse(await fs.readFile(path.join(out, "seed.json"), "utf8"));
const results = [], errors = [];
const api = async (url, method="GET", body) => {
  const response = await fetch(seed.base+"/api"+url, { method,
    headers: {Authorization: "Bearer "+seed.token, "Content-Type":"application/json"},
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw Error(await response.text());
  return response.json();
};
const browser = await chromium.launch({channel:"msedge", headless:true, args:["--disable-gpu", "--disable-gpu-compositing"]});
const context = await browser.newContext({viewport:{width:1440,height:900}, acceptDownloads:true});
const page = await context.newPage();
page.on("pageerror", error => errors.push(String(error)));
const record = name => { results.push({name, passed:true}); console.log(name); };
let fused;
try {
  await page.goto(seed.base+"/#token="+seed.token);
  await page.getByRole("button", {name:"打开 A.png", exact:true}).click();
  await page.getByText("融合识别 · 生成有来源的校对草稿", {exact:true}).click();
  await expect(page.getByRole("button", {name:"复用当前图片结果",exact:true})).toBeEnabled();
  await expect(page.getByRole("button", {name:/完整识别并融合/})).toBeDisabled();
  await expect(page.getByText("队列异常，请查看恢复提示", {exact:true})).toHaveCount(0);
  record("review-only permits existing CPU fusion without an OCR fault warning");
  await page.getByRole("button", {name:"复用当前图片结果",exact:true}).click();
  await expect.poll(async () => {
    const state = await api(`/projects/${seed.project}`);
    fused = state.tasks.find(t => t.kind === "fusion" && t.status === "succeeded");
    return !!fused;
  }, {timeout:20000}).toBe(true);
  await expect(page.getByLabel("当前识别结果")).toContainText("融合草稿");
  let state = await api(`/projects/${seed.project}`);
  expect(state.images.find(p=>p.id===seed.photos[0].id).selected_result).not.toBe(fused.result_id);
  await page.getByLabel("当前识别结果").selectOption(fused.result_id);
  await page.getByRole("tab",{name:"快速校对",exact:true}).click();
  await expect(page.getByText("先采用此预览，再提交校对决策。", {exact:true})).toBeVisible();
  await expect(page.getByRole("button",{name:"保留当前并继续",exact:true})).toBeDisabled();
  record("fusion result is preview-only until explicit adoption");
  await page.getByRole("button",{name:"采用此预览",exact:true}).click();
  const review = page.getByRole("region",{name:"快速校对",exact:true});
  await expect(review.getByRole("button",{name:"保留当前并继续",exact:true})).toBeEnabled();
  await expect(review).toContainText("整表 / 文字区域");
  await fs.writeFile(path.join(out,"location-debug.json"), JSON.stringify({
    image:await page.locator('.image-workspace').innerText(),
    version:await page.getByLabel('图片版本',{exact:true}).inputValue(),
    review:(await api(`/results/${fused.result_id}/issues`)).issues[0].location
  },null,2));
  await page.getByRole("button", {name:"放大疑点区域",exact:true}).click();
  record("real issue region can be enlarged with surrounding image context");
  const initial = await api(`/results/${fused.result_id}/issues`);
  expect(initial.counts.pending).toBeGreaterThan(0);
  await review.getByRole("button",{name:"下一项（跳过）",exact:true}).click();
  expect((await api(`/results/${fused.result_id}/issues`)).counts).toEqual(initial.counts);
  await review.getByRole("button",{name:"上一项",exact:true}).click();
  record("skip changes position without marking an issue complete");
  await review.getByLabel("疑点手工修改").fill("000098765432109876");
  await page.getByRole("button",{name:"打开 B.png",exact:true}).click();
  await expect(page.getByLabel("当前识别结果")).toHaveValue(fused.result_id);
  await expect(review.getByLabel("疑点手工修改")).toHaveValue("000098765432109876");
  record("navigation cannot discard a pending review draft");
  await page.reload();
  // Preview choice is not persisted, but adoption is; recovery restores review.
  await page.getByRole("tab",{name:"快速校对",exact:true}).click();
  await expect(review.getByLabel("疑点手工修改")).toHaveValue("000098765432109876");
  record("reload restores unsaved review draft and saved issue position");
  let dropped = false;
  await page.route("**/api/results/*/issues/*/decision", async route => {
    if (!dropped) { dropped = true; await route.fetch(); await route.abort("failed"); }
    else await route.continue();
  });
  await review.getByLabel("疑点手工修改").evaluate(el => el.dispatchEvent(new CompositionEvent("compositionstart", {bubbles:true})));
  await review.getByRole("button",{name:"保存并继续",exact:true}).click();
  expect((await api(`/results/${fused.result_id}`)).revision).toBe(0);
  await review.getByLabel("疑点手工修改").evaluate(el => el.dispatchEvent(new CompositionEvent("compositionend", {bubbles:true})));
  record("IME composition does not submit a partial draft");
  await review.getByRole("button",{name:"保存并继续",exact:true}).click();
  await expect(review.getByRole("alert")).toBeVisible();
  const committed = await api(`/results/${fused.result_id}`);
  expect(committed.revision).toBe(1);
  await expect(review.getByLabel("疑点手工修改")).toBeDisabled();
  await expect(review.getByRole("button",{name:"下一项（跳过）",exact:true})).toBeDisabled();
  await page.reload();
  await page.getByRole("tab",{name:"快速校对",exact:true}).click();
  await expect(review.getByRole("button",{name:"重试上次提交",exact:true})).toBeVisible();
  await review.getByRole("button",{name:"重试上次提交",exact:true}).click();
  await expect(review.getByRole("alert")).toHaveCount(0);
  expect((await api(`/results/${fused.result_id}`)).revision).toBe(1);
  await page.unroute("**/api/results/*/issues/*/decision");
  record("lost decision response locks its intent and retries idempotently after reload");
  await review.getByRole("button",{name:"暂不确定",exact:true}).click();
  await expect.poll(async () => (await api(`/results/${fused.result_id}/issues`)).counts.question).toBe(1);
  record("uncertain is persisted separately from resolved");
  await page.getByRole("button",{name:"撤销",exact:true}).click();
  await expect.poll(async () => (await api(`/results/${fused.result_id}/issues`)).counts.stale).toBeGreaterThan(0);
  record("undo expires the corresponding review decision");
  await page.getByRole("button",{name:"导出结果",exact:true}).click();
  await page.getByLabel("导出格式",{exact:true}).selectOption("json");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button",{name:"保存文件",exact:true}).click();
  const download = await downloadPromise;
  await download.saveAs(path.join(out,download.suggestedFilename()));
  record("UI exports saved fusion content with provenance ZIP");
  await page.setViewportSize({width:1366,height:768});
  await expect(review).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await review.evaluate(el => { let p=el.parentElement; while(p) { p.scrollTop=0; p=p.parentElement; } });
  await page.screenshot({path:path.join(out,"quick-review-1366.png"), fullPage:true, animations:"disabled"});
  record("laptop layout has no page-level horizontal overflow");
  await page.route('**/api/diagnostics', route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'诊断服务暂不可用（回归注入）'})}));
  await page.getByRole('button',{name:'环境检查',exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('诊断服务暂不可用');
  await page.getByRole('dialog').getByRole('button',{name:'关闭',exact:true}).click();
  await page.unroute('**/api/diagnostics');
  record('diagnostic failures are readable inside the open dialog');
  await page.getByText('项目管理',{exact:true}).click();
  await page.route('**/api/projects/*/storage', route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'占用查询暂不可用（回归注入）'})}));
  await page.getByRole('button',{name:'项目占用与清理',exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('占用查询暂不可用');
  await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).click();
  await page.unroute('**/api/projects/*/storage');
  record('storage failures are readable inside the open dialog');
  await page.locator('input[type=file][accept*=".png"]').setInputFiles({name:'corrupt.png',mimeType:'image/png',buffer:Buffer.from('not an image')});
  await expect(page.getByText(/文件可能损坏或格式不受支持/).first()).toBeVisible();
  record('corrupt-image import reports a readable Chinese error');
  await page.route('**/api/state',async route=>{const r=await route.fetch();const body=await r.json();body.review_only=false;await route.fulfill({response:r,json:body});});
  await page.route('**/api/engine-packages',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'引擎包清单暂不可用（回归注入）'})}));
  await page.reload();
  await page.getByRole('button',{name:'引擎管理',exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('引擎包清单暂不可用');
  await page.getByRole('dialog').getByRole('button',{name:'关闭',exact:true}).click();
  record('engine-package failures are readable inside the open dialog');
  expect(errors).toEqual([]);
} catch (error) {
  errors.push(String(error));
  await fs.writeFile(path.join(out,"failure-dom.txt"), await page.locator('body').innerText());
  await page.screenshot({path:path.join(out,"failure.png"),fullPage:true,animations:"disabled"});
  throw error;
} finally {
  await browser.close();
  await fs.writeFile(path.join(out,"ui-results.json"), JSON.stringify({scope:seed.evidence_scope,results,errors,browser_closed:true},null,2));
}
