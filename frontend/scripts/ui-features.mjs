import { chromium, expect } from "@playwright/test";
import fs from "node:fs/promises";
import path from "node:path";
const root = process.env.OCR_BUILD_ROOT || "E:/OCR-week23-build";
const state = JSON.parse(
  await fs.readFile(
    process.env.OCR_STATE_FILE ||
      path.join(root, "ui-project/launcher/launcher-state.json"),
    "utf8",
  ),
);
const token = (
  await fs.readFile(path.join(state.data, "launcher/session-token.txt"), "utf8")
).trim();
const out = process.env.OCR_UI_OUTPUT || path.join(root, "ui-feature-audit");
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
  const currentProject = (await api("/state")).projects.find(
    (p) => p.name === "浏览器完整验收",
  ).id;
  let featureProject = (await api("/state")).projects.find(
    (p) => p.name === "新增批量与清理验收",
  )?.id;
  if (!process.env.OCR_ENGINE_ONLY) {
    await button("新建项目").click();
    await page
      .getByLabel("项目名称", { exact: true })
      .fill("新增批量与清理验收");
    await button("创建项目").click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    featureProject = await selectedProject();
    const duplicate = path.join(out, "second-table.png");
    await fs.copyFile(path.join(root, "bundle/fixtures/table.png"), duplicate);
    await page
      .locator("input[type=file][accept*=png]")
      .setInputFiles([path.join(root, "bundle/fixtures/table.png"), duplicate]);
    await expect(page.locator(".photo-name strong")).toHaveCount(2);
    await page.getByLabel("选择全部图片", { exact: true }).check();
    await page
      .getByLabel("批次预处理", { exact: true })
      .selectOption("contrast");
    await page.getByLabel("识别引擎", { exact: true }).selectOption("glm");
    await page.getByRole("button", { name: /^开始识别/ }).click();
    await expect
      .poll(
        async () =>
          (await api("/projects/" + featureProject)).tasks.filter(
            (t) => t.status === "succeeded",
          ).length,
        { timeout: 300000, intervals: [1000] },
      )
      .toBe(2);
    const featureState = await api("/projects/" + featureProject);
    expect(
      featureState.tasks.every(
        (t) =>
          JSON.parse(t.preprocess)[0].kind === "contrast" &&
          t.version_id !== t.input_version_id,
      ),
    ).toBe(true);
    await fs.writeFile(
      path.join(out, "batch-state.json"),
      JSON.stringify(featureState, null, 2),
    );
    check(
      "GUI batch preset creates immutable processed versions for both images",
    );
    await button("收起任务队列").click();
    for (const aggregate of [false, true]) {
      await button("导出结果").click();
      await page.getByLabel("导出格式").selectOption("xlsx");
      await page.getByLabel(/导出选中图片采用的结果/).check();
      await page
        .getByLabel("汇总为一个工作簿", { exact: true })
        .setChecked(aggregate);
      const event = page.waitForEvent("download");
      await button("保存文件").click();
      await (
        await event
      ).saveAs(
        path.join(
          out,
          aggregate ? "batch-aggregate.xlsx" : "batch-separate.zip",
        ),
      );
      await page.getByRole("dialog").waitFor({ state: "hidden" });
    }
    check(
      "GUI defaults to separate image workbooks and supports explicit aggregate",
    );
  }
  await button("引擎管理").click();
  await expect(
    page.getByRole("heading", { name: "离线引擎管理", exact: true }),
  ).toBeVisible();
  const beforePackages = await api("/engine-packages");
  if (process.env.OCR_DIALOG_ONLY) {
    await page.getByLabel(/PP-OCR.*启用版本/).selectOption("builtin");
  } else if (
    beforePackages.installed.some((p) => p.id === "ppocr-validation-v1")
  ) {
    await page
      .getByLabel(/PP-OCR.*启用版本/)
      .selectOption("ppocr-validation-v1");
    await expect
      .poll(async () => (await api("/engine-packages")).active.ppocr, {
        timeout: 600000,
        intervals: [1000],
      })
      .toBe("ppocr-validation-v1");
  } else {
    if ((await api("/engine-packages")).staged.some((p) => p.ready)) {
      await button("继续此暂存包").first().click();
    } else {
      await page
        .getByLabel("选择离线引擎包", { exact: true })
        .setInputFiles(
          path.join(root, "engine-packages/ppocr-validation-v1.zip"),
        );
    }
    await page
      .getByText("启用此引擎包", { exact: true })
      .waitFor({ state: "visible", timeout: 600000 });
    const accessibility = await page.evaluate(() =>
      Array.from(document.querySelectorAll("button"))
        .filter((b) => b.textContent.includes("启用此引擎包"))
        .map((b) => ({
          html: b.outerHTML,
          ancestors: Array.from(
            (function* (n) {
              while (n) {
                yield n;
                n = n.parentElement;
              }
            })(b),
          ).map((n) => ({
            tag: n.tagName,
            hidden: n.getAttribute("aria-hidden"),
            role: n.getAttribute("role"),
          })),
        })),
    );
    await fs.writeFile(
      path.join(out, "activation-accessibility.json"),
      JSON.stringify(accessibility, null, 2),
    );
    expect(await button("启用此引擎包").count()).toBe(1);
    await expect(button("启用此引擎包")).toBeEnabled({ timeout: 600000 });
    await page.screenshot({
      path: path.join(out, "06-engine-stage.png"),
      fullPage: true,
    });
    await button("启用此引擎包").click();
    await expect
      .poll(async () => (await api("/engine-packages")).active.ppocr, {
        timeout: 600000,
        intervals: [1000],
      })
      .toBe("ppocr-validation-v1");
  }
  check(
    process.env.OCR_DIALOG_ONLY
      ? "DIAGNOSTIC: refresh built-in selection"
      : "GUI activates a real complete offline engine package",
  );
  const selector = page.getByLabel(/PP-OCR.*启用版本/);
  await expect(selector).toBeEnabled({ timeout: 15000 });
  await selector.selectOption("builtin");
  await expect
    .poll(async () => (await api("/engine-packages")).active.ppocr)
    .toBe("builtin");
  await expect(button("关闭")).toBeEnabled();
  await button("关闭").click();
  check(
    process.env.OCR_DIALOG_ONLY
      ? "DIAGNOSTIC: dialog remains accessible after asynchronous selection"
      : "GUI returns to built-in engine after real complete-package activation",
  );
  if (!process.env.OCR_DIALOG_ONLY) {
    await button("项目占用与清理").click();
    await expect(page.getByLabel("确认清理项目名称")).toBeVisible();
    await expect(button("清理并删除项目")).toBeDisabled();
    await page.getByLabel("确认清理项目名称").fill("新增批量与清理验收");
    await expect(button("清理并删除项目")).toBeEnabled();
    await page.screenshot({
      path: path.join(out, "07-project-storage.png"),
      fullPage: true,
    });
    await button("清理并删除项目").click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    expect(
      (await api("/state")).projects.some((p) => p.id === featureProject),
    ).toBe(false);
    expect((await api("/projects/" + currentProject)).images.length).toBe(3);
    check(
      "GUI cleans selected project after exact name confirmation and preserves another project",
    );
  }
  expect(errors).toEqual([]);
  await fs.writeFile(
    path.join(
      out,
      process.env.OCR_DIALOG_ONLY ? "dialog-diagnostic.json" : "result.json",
    ),
    JSON.stringify(
      { passed: true, checks, errors, retainedProject: currentProject },
      null,
      2,
    ),
  );
} catch (error) {
  await fs.writeFile(path.join(out, "failure-dom.html"), await page.content());
  await fs.writeFile(
    path.join(out, "failure-accessibility.json"),
    JSON.stringify(
      await page.evaluate(() =>
        Array.from(document.querySelectorAll("button"))
          .filter((b) => b.textContent.includes("关闭"))
          .map((b) => ({
            html: b.outerHTML,
            ancestors: Array.from(
              (function* (n) {
                while (n) {
                  yield n;
                  n = n.parentElement;
                }
              })(b),
            ).map((n) => ({
              tag: n.tagName,
              hidden: n.getAttribute("aria-hidden"),
              role: n.getAttribute("role"),
              inert: n.inert,
            })),
          })),
      ),
      null,
      2,
    ),
  );
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
