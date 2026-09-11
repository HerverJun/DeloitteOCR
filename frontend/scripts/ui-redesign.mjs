import { chromium, expect } from "@playwright/test";
import fs from "node:fs/promises";
import path from "node:path";

const root = process.env.OCR_BUILD_ROOT || "E:/OCR-deloitte-build";
const state = JSON.parse(
  await fs.readFile(
    process.env.OCR_STATE_FILE ||
      path.join(root, "preview-project/launcher/launcher-state.json"),
    "utf8",
  ),
);
const token = (
  await fs.readFile(path.join(state.data, "launcher/session-token.txt"), "utf8")
).trim();
const base = `http://127.0.0.1:${state.port}`;
const out = process.env.OCR_UI_OUTPUT || path.join(root, "ui-redesign");
await fs.mkdir(out, { recursive: true });
const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--disable-gpu", "--disable-gpu-compositing"],
});
const checks = [],
  errors = [],
  external = [],
  sizes = [];
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
});
const page = await context.newPage();
page.setDefaultTimeout(15000);
page.on("pageerror", (e) => errors.push(e.message));
await context.route("**/*", (route) => {
  const url = route.request().url();
  if (/^https?:/.test(url) && new URL(url).origin !== base) {
    external.push(url.split("?")[0]);
    return route.abort();
  }
  return route.continue();
});
const button = (name) => page.getByRole("button", { name, exact: true });
const api = async (p) => {
  const r = await fetch(base + "/api" + p, {
    headers: { Authorization: "Bearer " + token },
  });
  if (!r.ok) throw Error(String(r.status));
  return r.json();
};
const check = (name) => {
  checks.push(name);
  console.log(name);
};
const tabs = page.locator(".result-tabs");
try {
  await page.goto(base + "/#token=" + token);
  await page.getByLabel("当前项目").selectOption({ label: "浏览器完整验收" });
  await page.getByLabel("当前识别结果").waitFor();
  const options = await page
    .getByLabel("当前识别结果")
    .locator("option")
    .allTextContents();
  await page.getByLabel("当前识别结果").selectOption({
    index: options.findIndex((v) => v.startsWith("PaddleOCR-VL")),
  });
  await expect(page.locator(".table-editor")).toBeVisible();
  await expect(button("开始识别")).toBeEnabled();
  await page.evaluate(() => document.fonts.ready);
  expect(await page.title()).toBe("Deloitte ｜ OCR 工作台");
  expect(
    await page.locator(".brand img").evaluate((el) => el.naturalWidth > 0),
  ).toBe(true);
  await expect(page.getByLabel("识别引擎")).toHaveValue("paddlevl");
  check("official local logo, product title and first-use table mode");

  const separator = page.getByRole("separator");
  await separator.focus();
  await separator.press("ArrowLeft");
  await expect(separator).toHaveAttribute("aria-valuenow", "46");
  await separator.press("Enter");
  await expect(separator).toHaveAttribute("aria-valuenow", "48");
  const box = await separator.boundingBox();
  await page.mouse.move(box.x + 4, box.y + 80);
  await page.mouse.down();
  await page.mouse.move(box.x + 85, box.y + 80, { steps: 5 });
  await page.mouse.up();
  expect(Number(await separator.getAttribute("aria-valuenow"))).toBeGreaterThan(
    48,
  );
  await separator.dblclick();
  await expect(separator).toHaveAttribute("aria-valuenow", "48");
  check("splitter pointer, keyboard and reset behavior");

  const originalResult = await page.getByLabel("当前识别结果").inputValue();
  const before = await api("/results/" + originalResult);
  await tabs.getByRole("button", { name: "文字", exact: true }).click();
  const text = page.getByLabel("校对文字");
  await text.fill(
    before.edited.text + "\nDeloitte 布局保存验收 00001234567890123456",
  );
  await button("收起资料栏").click();
  await button("展开校对区").click();
  await expect(page.locator(".save-status")).toHaveText("已保存");
  await expect(text).toHaveValue(/Deloitte 布局保存验收/);
  await button("恢复双栏").click();
  await button("展开资料栏").click();
  await expect(text).toBeVisible();
  const after = await api("/results/" + originalResult);
  expect(after.original).toEqual(before.original);
  await button("撤销").click();
  await expect(text).toHaveValue(before.edited.text);
  check(
    "layout changes preserve pending edits and original output, explicit result tab survives autosave",
  );

  const first = page.getByLabel("选择 table.png", { exact: true });
  await first.check();
  await page.getByLabel("搜索图片文件名").fill("folder");
  await expect(page.locator(".photo-open")).toHaveCount(1);
  await expect(page.locator(".selection-notice")).toContainText("1 张筛选外");
  await page.getByLabel("选择全部图片", { exact: true }).check();
  await expect(page.locator(".recognition-scope")).toContainText("2 张");
  await page.getByLabel("选择全部图片", { exact: true }).uncheck();
  await expect(page.locator(".recognition-scope")).toContainText("1 张");
  await page.getByLabel("搜索图片文件名").fill("");
  await first.uncheck();
  await page.getByLabel("筛选图片状态").selectOption("failed");
  await expect(page.getByText("没有匹配的图片", { exact: true })).toBeVisible();
  await page.getByLabel("筛选图片状态").selectOption("all");
  check("filename/status filters and explicit hidden-selection scope");

  await page
    .locator(".mode-buttons")
    .getByRole("button", { name: "照片文字", exact: true })
    .click();
  await page.reload();
  await expect(page.getByLabel("识别引擎")).toHaveValue("ppocr");
  await page
    .locator(".mode-buttons")
    .getByRole("button", { name: "表格", exact: true })
    .click();
  await expect(page.locator(".table-editor")).toBeVisible();
  check("mode and engine preferences survive reload");

  // Save failure is simulated only at the HTTP boundary; data/model content is never replaced.
  await tabs.getByRole("button", { name: "文字", exact: true }).click();
  const clean = await text.inputValue();
  const resultUrl =
    base +
    "/api/results/" +
    (await page.getByLabel("当前识别结果").inputValue());
  const failSave = async (route) =>
    route.request().method() === "PUT"
      ? route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ detail: "测试：磁盘暂不可写" }),
        })
      : route.continue();
  await page.route(resultUrl, failSave);
  await text.fill(clean + "\n保存恢复验收");
  await expect(page.locator(".save-status")).toHaveText("保存失败");
  await page.unroute(resultUrl, failSave);
  await button("关闭提示").click();
  await button("重试保存").click();
  await expect(page.locator(".save-status")).toHaveText("已保存");
  await button("撤销").click();
  await expect(text).toHaveValue(clean);
  check("save failure retains edits and explicit retry recovers");

  await tabs.getByRole("button", { name: /^表格/ }).click();
  for (const [width, height] of [
    [1920, 1080],
    [1440, 900],
    [1366, 768],
    [1093, 614],
    [900, 768],
  ]) {
    await page.setViewportSize({ width, height });
    await expect(button("导出结果")).toBeVisible();
    const geometry = await page.evaluate(() => {
      const rect = (s) => {
        const r = document.querySelector(s).getBoundingClientRect();
        return {
          x: r.x,
          y: r.y,
          width: r.width,
          height: r.height,
          bottom: r.bottom,
        };
      };
      return {
        overflow: document.documentElement.scrollWidth > innerWidth,
        footer: rect(".result-footer"),
        table: rect(".table-scroll"),
      };
    });
    expect(geometry.overflow).toBe(false);
    expect(geometry.footer.bottom).toBeLessThanOrEqual(height + 1);
    expect(geometry.table.y).toBeLessThan(geometry.footer.y);
    expect(geometry.table.height).toBeGreaterThanOrEqual(90);
    sizes.push({ width, height, ...geometry });
    await page.screenshot({
      animations: "disabled",
      path: path.join(out, `table-${width}.png`),
    });
  }
  await button("原图").click();
  await expect(page.getByRole("img", { name: "当前图片版本" })).toBeVisible();
  await button("校对").click();
  await page.setViewportSize({ width: 1366, height: 768 });
  await button("展开资料栏").click();
  await page.locator(".queue-summary").click();
  const footer = await page.locator(".result-footer").boundingBox(),
    queue = await page.locator(".task-drawer").boundingBox();
  expect(footer.y + footer.height).toBeLessThanOrEqual(queue.y + 1);
  await page.screenshot({
    animations: "disabled",
    path: path.join(out, "queue.png"),
  });
  await button("收起任务队列").click();
  check(
    "desktop/laptop/125-percent-equivalent viewports, small-window navigation, docked queue without overlap",
  );

  await page.locator(".project-menu summary").click();
  await button("项目占用与清理").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.screenshot({
    animations: "disabled",
    path: path.join(out, "project-dialog.png"),
  });
  await button("取消").click();
  await page.locator(".project-menu summary").click();
  await button("引擎管理").click();
  await expect(
    page.getByRole("heading", { name: "离线引擎管理", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    animations: "disabled",
    path: path.join(out, "engine-dialog.png"),
  });
  await button("关闭").click();
  check("management dialogs retain accessible controls under brand theme");

  const scaledContext = await browser.newContext({
    viewport: { width: 1093, height: 614 },
    deviceScaleFactor: 1.25,
  });
  await scaledContext.route("**/*", (route) => {
    const url = route.request().url();
    if (/^https?:/.test(url) && new URL(url).origin !== base) {
      external.push(url.split("?")[0]);
      return route.abort();
    }
    return route.continue();
  });
  const scaledPage = await scaledContext.newPage();
  scaledPage.on("pageerror", (e) => errors.push(e.message));
  await scaledPage.goto(base + "/#token=" + token);
  await scaledPage
    .getByRole("button", { name: "展开资料栏", exact: true })
    .click();
  await scaledPage
    .getByLabel("当前项目")
    .selectOption({ label: "浏览器完整验收" });
  await scaledPage
    .getByRole("button", { name: "收起资料栏", exact: true })
    .click();
  await expect(
    scaledPage.getByRole("button", { name: "导出结果", exact: true }),
  ).toBeEnabled();
  await scaledPage.evaluate(() => document.fonts.ready);
  expect(
    await scaledPage.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
  ).toBe(false);
  expect(await scaledPage.evaluate(() => devicePixelRatio)).toBe(1.25);
  await scaledPage.screenshot({
    animations: "disabled",
    path: path.join(out, "scale-125.png"),
  });
  await scaledContext.close();
  check(
    "Chromium 1.25 device scale at 1093x614 CSS viewport (not a Windows system scaling certification)",
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await button("新建项目").click();
  await page.getByLabel("项目名称", { exact: true }).fill("队列交互验收");
  await button("创建项目").click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  const queueProject = await page.getByLabel("当前项目").inputValue();
  const queueFiles = [];
  for (let i = 1; i <= 3; i++) {
    const file = path.join(out, `queue-${i}.png`);
    await fs.copyFile(path.join(root, "bundle/fixtures/table.png"), file);
    queueFiles.push(file);
  }
  await page.locator("input[type=file][accept*=png]").setInputFiles(queueFiles);
  await expect(page.locator(".photo-open")).toHaveCount(3);
  await page.getByLabel("选择全部图片", { exact: true }).check();
  await page.getByLabel("识别引擎").selectOption("paddlevl");
  await button("开始识别").click();
  const queueTasks = async () => (await api("/projects/" + queueProject)).tasks;
  await expect
    .poll(async () => (await queueTasks()).some((t) => t.status === "running"))
    .toBe(true);
  await button("暂停等待项").click();
  await expect
    .poll(
      async () =>
        (await queueTasks()).filter((t) => t.status === "paused").length,
    )
    .toBe(2);
  const runningTask = (await queueTasks()).find((t) => t.status === "running");
  await page
    .locator(".task-row")
    .filter({ has: page.locator(".task-status.running") })
    .getByRole("button", { name: "取消", exact: true })
    .click();
  await expect
    .poll(
      async () =>
        (await queueTasks()).find((t) => t.id === runningTask.id).status,
    )
    .toBe("cancelled");
  await page.reload();
  await page.getByLabel("当前项目").selectOption(queueProject);
  await page.locator(".queue-summary").click();
  expect((await queueTasks()).filter((t) => t.status === "paused").length).toBe(
    2,
  );
  await page
    .locator(".task-row")
    .filter({ has: page.locator(".task-status.cancelled") })
    .getByRole("button", { name: "重试", exact: true })
    .click();
  await page
    .locator(".task-drawer > header")
    .getByRole("button", { name: "继续", exact: true })
    .click();
  await expect
    .poll(
      async () =>
        (await queueTasks()).filter((t) => t.status === "succeeded").length,
      { timeout: 300000, intervals: [1000] },
    )
    .toBe(3);
  await expect(page.locator(".queue-counts")).toContainText("已完成 3");
  await page
    .locator(".task-row")
    .first()
    .getByRole("button", { name: "查看", exact: true })
    .click();
  await expect(button("导出结果")).toBeEnabled();
  await page.screenshot({
    animations: "disabled",
    path: path.join(out, "queue-recovered.png"),
  });
  await fs.writeFile(
    path.join(out, "queue-state.json"),
    JSON.stringify(await api("/projects/" + queueProject), null, 2),
  );
  await button("收起任务队列").click();
  await page.getByLabel("当前项目").selectOption({ label: "浏览器完整验收" });
  check(
    "real queue pause, cancel, reload persistence, retry, resume, completion counts and view result",
  );
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
  await fs.writeFile(
    path.join(out, "result.json"),
    JSON.stringify(
      {
        passed: true,
        checks,
        errors,
        externalRequests: external,
        viewports: sizes,
        scope: "真实结果界面回归；保存失败场景仅拦截一次测试项目 PUT 请求",
      },
      null,
      2,
    ),
  );
} catch (error) {
  await page.screenshot({
    animations: "disabled",
    path: path.join(out, "failure.png"),
  });
  await fs.writeFile(
    path.join(out, "failure.json"),
    JSON.stringify(
      { passed: false, message: String(error), checks, errors },
      null,
      2,
    ),
  );
  throw error;
} finally {
  await browser.close();
}
