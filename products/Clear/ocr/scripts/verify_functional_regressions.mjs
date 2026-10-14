import {
  chromium,
  expect,
} from "../frontend/node_modules/@playwright/test/index.mjs";
import fs from "node:fs/promises";
import path from "node:path";

const out = path.resolve(process.argv[2]);
const seed = JSON.parse(await fs.readFile(path.join(out, "seed.json"), "utf8"));
const { base } = seed;
const headers = {
  Authorization: "Bearer " + seed.token,
  "Content-Type": "application/json",
};
const response = (url, method = "GET", body) =>
  fetch(base + "/api" + url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
const api = async (url, method = "GET", body) => {
  const res = await response(url, method, body);
  if (!res.ok) throw Error(await res.text());
  return res.json();
};
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { resolve, promise };
};
const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--disable-gpu", "--disable-gpu-compositing"],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  permissions: ["clipboard-read", "clipboard-write"],
});
await context.addInitScript((project) => {
  localStorage.setItem("ocr-project", project);
  localStorage.setItem("ocr-ui-mode", "text");
}, seed.project);
const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [],
  checks = [],
  releases = [];
page.on("pageerror", (error) => errors.push(error.message));
const button = (name) => page.getByRole("button", { name, exact: true });
const resultTab = (name) => page.getByRole("tab", { name, exact: true });
const { A: a, B: b, C: c, TEXT: textOnly, MIXED: mixed } = seed.seeds;
const saved = async (id) => api("/results/" + id);
const open = async (item) => {
  await button("打开 " + item.photo.name).click();
  await expect(page.getByLabel("当前识别结果")).toHaveValue(item.result);
};
const writeText = async (item, text) => {
  await resultTab("文字").click();
  await page.getByLabel("校对文字").fill(text);
  await expect(page.locator(".save-status")).toHaveText("已保存");
  expect((await saved(item.result)).edited.text).toBe(text);
};
const hold = async (pattern) => {
  const received = deferred(),
    release = deferred(),
    completed = deferred();
  releases.push(release.resolve);
  await page.route(pattern, async (route) => {
    const res = await route.fetch();
    received.resolve();
    await release.promise;
    await route.fulfill({ response: res });
    completed.resolve();
  });
  return {
    received: received.promise,
    release: release.resolve,
    clear: async () => {
      await completed.promise;
      await page.unroute(pattern);
    },
  };
};
const record = (name) => {
  checks.push(name);
  console.log("PASS " + name);
};

try {
  await page.goto(base + "/#token=" + seed.token);
  await expect(page.getByLabel("当前识别结果")).toHaveValue(a.result);
  await resultTab("文字").click();
  const undoImage = await hold("**/api/results/" + a.result + "/history");
  await button("撤销").click();
  await undoImage.received;
  await button("打开 B.png").click();
  await expect(page.getByLabel("校对文字")).toHaveAttribute("readonly", "");
  undoImage.release();
  await expect(page.getByLabel("当前识别结果")).toHaveValue(b.result);
  await writeText(b, "SAVED_ONLY_TO_B_AFTER_UNDO");
  expect((await saved(a.result)).edited.text).toBe(
    (await saved(a.result)).original.text,
  );
  await undoImage.clear();
  record("Delayed undo + image switch: subsequent edit is saved only to B");

  await open(a);
  await writeText(a, "A_BEFORE_PROJECT_UNDO");
  const undoProject = await hold("**/api/results/" + a.result + "/history");
  await button("撤销").click();
  await undoProject.received;
  await page.getByLabel("当前项目").selectOption(seed.second_project);
  undoProject.release();
  await expect(page.getByLabel("当前识别结果")).toHaveValue(c.result);
  await writeText(c, "SAVED_ONLY_TO_C_AFTER_UNDO");
  expect((await saved(a.result)).edited.text).toBe(
    (await saved(a.result)).original.text,
  );
  await undoProject.clear();
  record("Delayed undo + project switch: subsequent edit is saved only to C");

  await page.getByLabel("当前项目").selectOption(seed.project);
  await expect(page.getByLabel("当前识别结果")).toHaveValue(a.result);
  const aPath = "**/api/results/" + a.result;
  await page.route(aPath, (route) =>
    route.request().method() === "PUT"
      ? route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ message: "回归用保存冲突" }),
        })
      : route.continue(),
  );
  await resultTab("文字").click();
  await page.getByLabel("校对文字").fill("UNSAVED_CONFLICT");
  await expect(page.locator(".save-status")).toHaveText("保存失败");
  await page.unroute(aPath);
  const reloadImage = await hold(aPath);
  await button("放弃本次修改并重新加载").click();
  const discardDialog = page.getByRole("dialog", {
    name: "放弃本次未保存修改",
  });
  await expect(discardDialog).toBeVisible();
  await discardDialog
    .getByRole("button", { name: "放弃修改并加载", exact: true })
    .click();
  await reloadImage.received;
  // The explicit discard dialog now protects navigation while reload is pending.
  // Release the delayed response before leaving it; the subsequent edit must
  // still belong exclusively to B, preserving the original isolation assertion.
  await expect(discardDialog).toBeVisible();
  await expect(
    discardDialog.getByRole("button", { name: "放弃修改并加载", exact: true }),
  ).toBeDisabled();
  reloadImage.release();
  await expect(discardDialog).not.toBeVisible();
  await button("打开 B.png").click();
  await expect(page.getByLabel("当前识别结果")).toHaveValue(b.result);
  await writeText(b, "SAVED_ONLY_TO_B_AFTER_RELOAD");
  expect((await saved(a.result)).edited.text).toBe(
    (await saved(a.result)).original.text,
  );
  await reloadImage.clear();
  record(
    "Delayed confirmed reload protects navigation; subsequent B edit never crosses result IDs",
  );

  const slowLoad = await hold(aPath);
  await button("打开 A.png").click();
  await slowLoad.received;
  await open(b);
  slowLoad.release();
  await slowLoad.clear();
  await writeText(b, "B_SURVIVES_LATE_A_LOAD");
  await expect(page.getByLabel("当前识别结果")).toHaveValue(b.result);
  record("Late result load cannot replace the newer image");

  await open(a);
  await resultTab("表格 1").click();
  const cell = (r, col) =>
    page.getByRole("textbox", { name: `第 ${r} 行第 ${col} 列`, exact: true });
  await cell(1, 1).fill("CELL_EDIT_NEW");
  // Copy immediately: it must flush pending edits and use the TXT exporter.
  await button("复制").click();
  await expect(page.locator(".toast")).toContainText("文字已复制");
  const clipboard = await page.evaluate(() => navigator.clipboard.readText());
  const txt = await response("/export", "POST", {
    result_ids: [a.result],
    format: "txt",
  });
  expect(clipboard).toBe(await txt.text());
  expect(clipboard).toContain("CELL_EDIT_NEW");
  expect(clipboard).not.toContain("A11");
  record("Copy flushes pending cell edits and equals TXT export");

  await cell(1, 1).click();
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press("Tab");
    if (await cell(2, 1).evaluate((el) => el === document.activeElement)) break;
  }
  await expect(cell(2, 1)).toBeFocused();
  await expect(page.getByLabel("当前单元格")).toHaveText("A2");
  await page.keyboard.type("DELETE_THIS_ROW");
  await button("删行").click();
  await expect(page.locator(".save-status")).toHaveText("已保存");
  const remaining = (await saved(a.result)).edited.tables[0];
  expect(remaining.rows).toBe(1);
  expect(remaining.cells.map((cell) => cell.text)).toEqual([
    "CELL_EDIT_NEW",
    "A12",
  ]);
  await cell(1, 1).click();
  await page.keyboard.press("Tab");
  await expect(cell(1, 2)).toBeFocused();
  await expect(page.getByLabel("当前单元格")).toHaveText("B1");
  await button("删列").click();
  await expect(page.locator(".save-status")).toHaveText("已保存");
  expect(
    (await saved(a.result)).edited.tables[0].cells.map((cell) => cell.text),
  ).toEqual(["CELL_EDIT_NEW"]);
  record("Tab focus drives the address, row deletion and column deletion");

  await open(b);
  await resultTab("表格 1").click();
  await cell(1, 1).click();
  await cell(2, 2).click({ modifiers: ["Shift"] });
  expect(await page.locator(".selected-cell").count()).toBe(4);
  await button("合并").click();
  await expect(page.locator(".save-status")).toHaveText("已保存");
  const merged = (await saved(b.result)).edited.tables[0];
  expect(merged.cells).toHaveLength(1);
  expect(merged.cells[0]).toMatchObject({ row_span: 2, column_span: 2 });
  await button("拆分").click();
  await expect(page.locator(".save-status")).toHaveText("已保存");
  record("Shift-click retains the anchor for rectangular merge and split");

  const image = await fs.readFile(path.join(out, "import.png"));
  const imported = await hold("**/api/projects/" + seed.project + "/images");
  await page
    .locator("input[type=file][accept*=png]")
    .setInputFiles({ name: "D.png", mimeType: "image/png", buffer: image });
  await imported.received;
  await page.getByLabel("当前项目").selectOption(seed.second_project);
  await expect(page.locator(".result-filename")).toContainText("C.png");
  imported.release();
  await expect(page.locator(".toast")).toContainText(
    "导入 1 张图片（已保存到原项目）",
  );
  await expect(page.locator(".result-filename")).toContainText("C.png");
  await expect(page.getByLabel("当前识别结果")).toHaveValue(c.result);
  await expect(page.locator(".recognition-scope")).not.toContainText(
    "已选 1 张",
  );
  await button("开始识别").click();
  await expect(page.locator(".toast")).toContainText("已加入 1 项任务");
  const state = await api("/projects/" + seed.second_project);
  expect(
    state.tasks
      .filter((task) => task.status === "queued")
      .map((task) => task.image_id),
  ).toEqual([c.photo.id]);
  expect(
    (await api("/projects/" + seed.project)).images.some(
      (photo) => photo.name === "D.png",
    ),
  ).toBe(true);
  await imported.clear();
  record(
    "Late import keeps C active and recognition queues C in the current project",
  );

  await page.getByLabel("当前项目").selectOption(seed.project);
  await expect(page.getByLabel("当前识别结果")).toHaveValue(a.result);
  const roundTrip = await hold("**/api/projects/" + seed.project + "/images");
  await page
    .locator("input[type=file][accept*=png]")
    .setInputFiles({ name: "E.png", mimeType: "image/png", buffer: image });
  await roundTrip.received;
  await page.getByLabel("当前项目").selectOption(seed.second_project);
  await expect(page.locator(".result-filename")).toContainText("C.png");
  await page.getByLabel("当前项目").selectOption(seed.project);
  await expect(page.getByLabel("当前识别结果")).toHaveValue(a.result);
  roundTrip.release();
  await expect(page.locator(".toast")).toContainText("导入 1 张图片");
  await expect(page.locator(".result-filename")).toContainText("A.png");
  await expect(page.locator(".recognition-scope")).not.toContainText(
    "已选 1 张",
  );
  await roundTrip.clear();
  record(
    "Returning to the import project still preserves the newer selection generation",
  );

  for (const aggregate of [false, true]) {
    const res = await response("/export", "POST", {
      result_ids: [a.result, textOnly.result],
      format: "xlsx",
      aggregate,
    });
    expect(res.status).toBe(400);
    expect((await res.json()).message).toContain("TEXT.png");
  }
  for (const format of ["txt", "md"]) {
    const res = await response("/export", "POST", {
      result_ids: [mixed.result],
      format,
    });
    expect(res.status).toBe(200);
    const content = await res.text();
    for (const text of [
      "MD_HEADER",
      "MD_VALUE",
      "HTML_VALUE",
      "BEFORE",
      "BETWEEN",
      "AFTER",
    ])
      expect(content).toContain(text);
    expect(content.indexOf("MD_VALUE")).toBeLessThan(
      content.indexOf("BETWEEN"),
    );
    expect(content.indexOf("HTML_VALUE")).toBeGreaterThan(
      content.indexOf("BETWEEN"),
    );
  }
  record(
    "Real HTTP export preserves mixed tables and rejects incomplete Excel batches",
  );
  expect(errors).toEqual([]);
} catch (error) {
  checks.push({ failure: String(error.stack || error) });
  process.exitCode = 1;
} finally {
  for (const release of releases) release();
  await browser.close();
  const report = { checks, page_errors: errors, passed: !process.exitCode };
  await fs.writeFile(
    path.join(out, "regression-results.json"),
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report, null, 2));
}
