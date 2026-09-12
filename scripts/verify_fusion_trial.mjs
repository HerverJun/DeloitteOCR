import { chromium, expect } from '../frontend/node_modules/@playwright/test/index.mjs';
const browser = await chromium.launch({channel:'msedge', headless:true, args:['--disable-gpu','--disable-gpu-compositing']});
try {
  const page = await browser.newPage({viewport:{width:1366,height:768}});
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(process.argv[2]);
  if (process.argv.includes('--interactive-scratch')) {
    const base = new URL(process.argv[2]).origin;
    const token = new URLSearchParams(new URL(process.argv[2]).hash.slice(1)).get('token');
    const api = async (path, method='GET', body) => {
      const response = await fetch(base+'/api'+path, {method, headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'}, body:body?JSON.stringify(body):undefined});
      expect(response.ok).toBe(true);
      return response.json();
    };
    await page.locator('#operator').fill('AUTOMATION-SCRATCH-NOT-HUMAN');
    const popup = page.waitForEvent('popup');
    await page.locator('#start').click();
    const workbench = await popup;
    let state = await api('/trial/state');
    let round = state.current;
    let project = await api('/projects/'+round.project_id);
    await expect(workbench.getByLabel('当前项目',{exact:true})).toHaveValue(round.project_id);
    const result = await api('/results/'+round.target_result);
    await api('/images/'+round.image_id+'/selection','PUT',{result_id:result.id, revision:result.revision});
    await api('/images/'+round.image_id+'/review','PUT',{status:'confirmed',result_id:result.id,revision:result.revision,version_id:round.version_id});
    await page.locator('#finish').click();
    await expect(page.locator('#start')).toBeEnabled();
    const navigation = workbench.waitForEvent('domcontentloaded');
    await page.locator('#start').click();
    await expect.poll(async()=> (await api('/trial/state')).current.round).toBe(round.round+1);
    state = await api('/trial/state');
    await navigation;
    project = await api('/projects/'+state.current.project_id);
    await expect(workbench.getByLabel('当前项目',{exact:true})).toHaveValue(state.current.project_id);
    expect(errors).toEqual([]);
  } else {
  await expect(page.locator('#status')).toContainText('"completed": 0');
  await expect(page.locator('#start')).toBeEnabled();
  await expect(page.locator('#finish')).toBeDisabled();
  await expect(page.locator('#reopen')).toBeDisabled();
  expect(errors).toEqual([]);
  }
} finally {
  await browser.close();
}
