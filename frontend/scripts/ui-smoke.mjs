import { chromium } from "@playwright/test";
import fs from "node:fs/promises";
import path from "node:path";
const root = "E:/OCR-week23-build";
const state = JSON.parse(
  await fs.readFile(
    process.env.OCR_LAUNCHER_STATE ||
      path.join(root, "ui-project/launcher/launcher-state.json"),
    "utf8",
  ),
);
const token = (
  await fs.readFile(path.join(state.data, "launcher/session-token.txt"), "utf8")
).trim();
const out = path.join(root, "ui-smoke");
await fs.mkdir(out, { recursive: true });
const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--disable-gpu", "--disable-gpu-compositing"],
});
const page = await browser.newPage({
  viewport: { width: 1440, height: 1000 },
  deviceScaleFactor: 1,
});
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
try {
  await page.goto("http://127.0.0.1:" + state.port + "/#token=" + token);
  await page.getByText("让图片里的信息可编辑").waitFor();
  await page.screenshot({
    path: path.join(out, "01-empty.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "新建项目", exact: true }).click();
  await page
    .getByRole("textbox", { name: "项目名称", exact: true })
    .fill("交互验收 " + Date.now());
  await page.getByRole("button", { name: "创建项目", exact: true }).click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page
    .locator("input[type=file]")
    .first()
    .setInputFiles("C:/Users/A/Desktop/OCR/fixtures/table.png");
  await page.getByRole("img", { name: "当前图片版本" }).waitFor();
  await page
    .locator(".mode-buttons")
    .getByRole("button", { name: "表格", exact: true })
    .click();
  await page.getByRole("button", { name: "开始识别", exact: true }).click();
  await page
    .getByRole("textbox", { name: "第 3 行第 2 列", exact: true })
    .waitFor({ timeout: 240000 });
  await page.getByRole("button", { name: "收起任务队列", exact: true }).click();
  await page
    .getByRole("textbox", { name: "第 3 行第 2 列", exact: true })
    .fill("扫描仪（已校对）");
  await page.getByText("已保存", { exact: true }).waitFor();
  await page.screenshot({
    path: path.join(out, "02-table-edited.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "导出结果", exact: true }).click();
  const event = page.waitForEvent("download");
  await page.getByRole("button", { name: "保存文件", exact: true }).click();
  const download = await event;
  await download.saveAs(path.join(out, "edited.xlsx"));
  await page.reload();
  await page.getByRole("textbox", { name: "校对文字", exact: true }).waitFor();
  await page
    .locator(".result-tabs")
    .getByRole("button", { name: "表格 1", exact: true })
    .click();
  if (
    (await page
      .getByRole("textbox", { name: "第 3 行第 2 列", exact: true })
      .inputValue()) !== "扫描仪（已校对）"
  )
    throw Error("Edit did not survive browser reload");
  await page.screenshot({
    path: path.join(out, "03-restored.png"),
    fullPage: true,
  });
  if (errors.length) throw Error(errors.join("\n"));
  await fs.writeFile(
    path.join(out, "result.json"),
    JSON.stringify(
      {
        passed: true,
        browser: "Edge Chromium, GPU disabled for browser only",
        errors,
        checks: [
          "create project",
          "import real fixture",
          "PaddleOCR-VL real inference",
          "edit table",
          "auto-save",
          "XLSX browser download",
          "reload persists edit",
        ],
      },
      null,
      2,
    ),
  );
  console.log("UI smoke passed");
} catch (error) {
  await page.screenshot({
    path: path.join(out, "failure.png"),
    fullPage: true,
  });
  console.log(await page.locator("body").innerText());
  throw error;
} finally {
  await browser.close();
}
