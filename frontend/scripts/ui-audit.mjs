import { chromium, expect } from "@playwright/test";
import fs from "node:fs/promises";
import path from "node:path";
const root = process.env.OCR_BUILD_ROOT || "E:/OCR-week23-build";
const state = JSON.parse(
  await fs.readFile(
    process.env.OCR_STATE_FILE || path.join(root, "ui-project/launcher/launcher-state.json"),
    "utf8",
  ),
);
const token = (
  await fs.readFile(path.join(state.data, "launcher/session-token.txt"), "utf8")
).trim();
const out = process.env.OCR_UI_OUTPUT || path.join(root, "ui-audit");
await fs.mkdir(out, { recursive: true });
const base = "http://127.0.0.1:" + state.port;
const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--disable-gpu", "--disable-gpu-compositing"],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  deviceScaleFactor: 1,
  permissions: ["clipboard-read", "clipboard-write"],
});
const page = await context.newPage();
page.setDefaultTimeout(15000);
const errors = [],
  checks = [];
page.on("pageerror", (e) => errors.push(e.message));
const check = (name) => {
  checks.push(name);
  console.log(name);
};
const button = (name) => page.getByRole("button", { name, exact: true });
const cell = (r, c) =>
  page.getByRole("textbox", { name: `第 ${r} 行第 ${c} 列`, exact: true });
const saved = () => expect(page.locator(".save-status")).toHaveText("已保存");
async function historyStep(name) {
  const response = page.waitForResponse(
    (r) => r.url().endsWith("/history") && r.request().method() === "POST",
  );
  await button(name).click();
  const value = await response;
  expect(value.ok()).toBe(true);
  await expect(button("开始识别")).toBeEnabled();
}
const api = async (p) => {
  const r = await fetch(base + "/api" + p, {
    headers: { Authorization: "Bearer " + token },
  });
  if (!r.ok) throw Error(await r.text());
  return r.json();
};
const selectedProject = () => page.getByLabel("当前项目").inputValue();
const activeVersion = () => page.getByLabel("图片版本").inputValue();
const transform = async (name) => {
  const old = await activeVersion();
  await button(name).click();
  await expect.poll(activeVersion).not.toBe(old);
  await expect(page.getByRole("img", { name: "当前图片版本" })).toBeVisible();
};
async function dragRegion() {
  const rect = await page.locator(".paper-image").boundingBox();
  if (!rect) throw Error("No image geometry");
  await page.mouse.move(
    rect.x + rect.width * 0.05,
    rect.y + rect.height * 0.05,
  );
  await page.mouse.down();
  await page.mouse.move(
    rect.x + rect.width * 0.95,
    rect.y + rect.height * 0.95,
    { steps: 12 },
  );
  await page.mouse.up();
}
try {
  await page.goto(base + "/#token=" + token);
  await button("新建项目").click();
  await page.getByLabel("项目名称", { exact: true }).fill("浏览器完整验收");
  await button("创建项目").click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await expect(
    page.getByLabel("当前项目").locator("option:checked"),
  ).toHaveText("浏览器完整验收");
  await page
    .locator("input[type=file][accept*=png]")
    .setInputFiles(path.join(root, "bundle/fixtures/table.png"));
  await expect(page.getByRole("img", { name: "当前图片版本" })).toBeVisible();
  check("named project import waits for project creation");
  await page.screenshot({
    path: path.join(out, "01-import.png"),
    fullPage: true,
  });
  const original = await activeVersion();
  await page
    .locator(".mode-buttons")
    .getByRole("button", { name: "模型对比", exact: true })
    .click();
  await button("开始识别").click();
  await expect
    .poll(
      async () => {
        const p = await api("/projects/" + (await selectedProject()));
        return p.tasks.filter((t) => t.status === "succeeded").length;
      },
      { timeout: 300000, intervals: [1000] },
    )
    .toBe(4);
  await button("收起任务队列").click();
  await expect(page.locator(".comparison-result")).toHaveCount(4);
  await page
    .locator(".comparison-result")
    .filter({ hasText: "PaddleOCR-VL" })
    .getByRole("button")
    .click();
  await expect(
    page.getByLabel("当前识别结果").locator("option:checked"),
  ).toContainText("PaddleOCR-VL");
  check("four real model comparison, timing, differences and adoption");
  await page.screenshot({
    path: path.join(out, "02-comparison.png"),
    fullPage: true,
  });
  await page
    .locator(".result-tabs")
    .getByRole("button", { name: "表格 1", exact: true })
    .click();
  await cell(3, 2).fill("扫描仪（浏览器校对）");
  await saved();
  await historyStep("撤销");
  await expect(cell(3, 2)).toHaveValue("扫描仪");
  await historyStep("重做");
  await expect(cell(3, 2)).toHaveValue("扫描仪（浏览器校对）");
  await cell(3, 2).click();
  await button("加行").click();
  await expect(page.locator(".table-selector")).toContainText("6 行");
  await saved();
  await button("删行").click();
  await expect(page.locator(".table-selector")).toContainText("5 行");
  await saved();
  await historyStep("撤销");
  await saved();
  await historyStep("撤销");
  await expect(cell(3, 2)).toHaveValue("扫描仪（浏览器校对）");
  await cell(3, 2).click();
  await button("加列").click();
  await expect(page.locator(".table-selector")).toContainText("5 列");
  await saved();
  await historyStep("撤销");
  await expect(page.locator(".table-selector")).toContainText("4 列");
  await cell(4, 3).click();
  await cell(4, 4).click({ modifiers: ["Shift"] });
  await button("合并").click();
  await expect(cell(4, 4)).toHaveCount(0);
  await button("拆分").click();
  await expect(cell(4, 4)).toHaveCount(1);
  await saved();
  check("table edit, insert/delete, merge/split, undo/redo and autosave");
  await page.screenshot({
    path: path.join(out, "03-table.png"),
    fullPage: true,
  });
  await page
    .locator(".result-tabs")
    .getByRole("button", { name: "文字", exact: true })
    .click();
  const text = page.getByLabel("校对文字");
  const oldText = await text.inputValue();
  await text.fill(oldText + "\n浏览器文字校对 00001234567890123456");
  await saved();
  await page.getByLabel("搜索识别文字").fill("00001234567890123456");
  await button("下一个匹配").click();
  expect(
    await text.evaluate((el) =>
      el.value.slice(el.selectionStart, el.selectionEnd),
    ),
  ).toBe("00001234567890123456");
  await button("复制").click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain(
    "浏览器文字校对",
  );
  check("text editing, search selection and clipboard");
  const pp = await page
    .getByLabel("当前识别结果")
    .locator("option")
    .allTextContents();
  const ppIndex = pp.findIndex((x) => x.startsWith("PP-OCR"));
  await page.getByLabel("当前识别结果").selectOption({ index: ppIndex });
  await page.locator(".text-blocks summary").click();
  await page.locator(".text-blocks button:not([disabled])").first().click();
  await expect(
    page.locator('.image-overlay polygon[fill="#106bcb25"]'),
  ).toHaveCount(1);
  check("text block locates matching image coordinates");
  const vlIndex = pp.findIndex((x) => x.startsWith("PaddleOCR-VL"));
  await page.getByLabel("当前识别结果").selectOption({ index: vlIndex });
  await expect(text).toHaveValue(/浏览器文字校对/);
  for (const format of ["xlsx", "txt", "md", "json"]) {
    await button("导出结果").click();
    await page.getByLabel("导出格式").selectOption(format);
    const event = page.waitForEvent("download");
    await button("保存文件").click();
    await (await event).saveAs(path.join(out, "edited." + format));
    await page.getByRole("dialog").waitFor({ state: "hidden" });
  }
  check("browser downloads XLSX, TXT, Markdown and JSON");
  await button("放大").click();
  await button("缩小").click();
  await page.locator(".zoom-label").click();
  check("image zoom controls");
  await transform("顺时针旋转");
  await button("查看原图").click();
  await expect.poll(activeVersion).toBe(original);
  await transform("增强对比度");
  await button("查看原图").click();
  await expect.poll(activeVersion).toBe(original);
  await button("裁剪").click();
  await dragRegion();
  await transform("应用");
  await button("查看原图").click();
  await expect.poll(activeVersion).toBe(original);
  await button("四角透视").click();
  const rect = await page.locator(".paper-image").boundingBox();
  for (const [x, y] of [
    [0.03, 0.03],
    [0.97, 0.03],
    [0.97, 0.97],
    [0.03, 0.97],
  ])
    await page.mouse.click(rect.x + rect.width * x, rect.y + rect.height * y);
  const corner = page.locator(".image-overlay circle").first();
  const c = await corner.boundingBox();
  await page.mouse.move(c.x + c.width / 2, c.y + c.height / 2);
  await page.mouse.down();
  await page.mouse.move(c.x + c.width / 2 + 3, c.y + c.height / 2 + 3, {
    steps: 3,
  });
  await page.mouse.up();
  await transform("应用");
  check(
    "rotation, contrast, crop and draggable four-point perspective create versions",
  );
  await page.screenshot({
    path: path.join(out, "04-perspective.png"),
    fullPage: true,
  });
  await button("查看原图").click();
  await expect.poll(activeVersion).toBe(original);
  await page.getByLabel("识别引擎").selectOption("ppocr");
  await button("区域重识别").click();
  await dragRegion();
  await transform("应用");
  await expect
    .poll(
      async () => {
        const p = await api("/projects/" + (await selectedProject()));
        return p.tasks.filter((t) => t.status === "succeeded").length;
      },
      { timeout: 120000, intervals: [1000] },
    )
    .toBe(5);
  await button("收起任务队列").click();
  check("region crop runs selected real engine");
  const beforeDewarp = await activeVersion();
  await button("去弯曲").click();
  await expect
    .poll(
      async () => {
        const p = await api("/projects/" + (await selectedProject()));
        return p.tasks.filter(
          (t) => t.kind === "dewarp" && t.status === "succeeded",
        ).length;
      },
      { timeout: 120000, intervals: [1000] },
    )
    .toBe(1);
  await expect.poll(activeVersion).not.toBe(beforeDewarp);
  await button("收起任务队列").click();
  check("optional real dewarp from UI");
  const currentProject = await selectedProject();
  // Switch projects before the debounce expires; the old result must flush first.
  await text.fill((await text.inputValue()) + "\n切换前自动保存");
  await button("新建项目").click();
  await page.getByLabel("项目名称", { exact: true }).fill("切换验收");
  await button("创建项目").click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page.getByLabel("当前项目").selectOption(currentProject);
  await expect(text).toHaveValue(/切换前自动保存/);
  await page.reload();
  await expect(text).toHaveValue(/切换前自动保存/);
  check("immediate project switch and reload preserve pending edit");
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.screenshot({
    path: path.join(out, "05-laptop.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  check("1366 by 768 viewport has no horizontal page overflow");
  // Exercise folder and drag-and-drop paths using local fixture bytes.
  const folder = path.join(out, "folder-input");
  await fs.mkdir(folder, { recursive: true });
  await fs.copyFile(
    path.join(root, "bundle/fixtures/printed.png"),
    path.join(folder, "folder.png"),
  );
  await page.locator("input[webkitdirectory]").setInputFiles(folder);
  await expect(page.locator(".photo-name strong")).toHaveCount(2);
  const bytes = await fs.readFile(
    path.join(root, "bundle/fixtures/printed.png"),
  );
  const transfer = await page.evaluateHandle((data) => {
    const dt = new DataTransfer();
    dt.items.add(
      new File([new Uint8Array(data)], "dropped.png", { type: "image/png" }),
    );
    return dt;
  }, Array.from(bytes));
  await page
    .locator(".app-shell")
    .dispatchEvent("drop", { dataTransfer: transfer });
  await expect(page.locator(".photo-name strong")).toHaveCount(3);
  check("folder import and dropped file");
  expect(errors).toEqual([]);
  await fs.writeFile(
    path.join(out, "result.json"),
    JSON.stringify(
      { passed: true, checks, errors, project: currentProject },
      null,
      2,
    ),
  );
} catch (error) {
  await page.screenshot({
    path: path.join(out, "failure.png"),
    fullPage: true,
  });
  await fs.writeFile(
    path.join(out, "failure.txt"),
    String(error) + "\n" + (await page.locator("body").innerText()),
  );
  throw error;
} finally {
  await browser.close();
}
