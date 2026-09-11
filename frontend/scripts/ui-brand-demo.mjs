import { chromium, expect } from "@playwright/test";
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";

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
const out = path.join(root, "brand-demo-final");
await fs.mkdir(out, { recursive: true });
const api = async (url, method = "GET", body) => {
  const response = await fetch(base + "/api" + url, {
    method,
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw Error("API status " + response.status);
  return response.json();
};
const projects = (await api("/state")).projects;
const project = projects.find((p) => p.name === "设备验收 · 示例项目");
if (!project) throw Error("Real recognized synthetic demo project is required");
const modelResult = (await api("/projects/" + project.id)).tasks.find(
  (t) => t.status === "succeeded" && t.engine === "paddlevl",
).result_id;
const originalBefore = (await api("/results/" + modelResult)).original;
const errors = [],
  external = [],
  views = [];
const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--disable-gpu", "--disable-gpu-compositing"],
});
try {
  for (const version of ["before", "after"]) {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
    });
    await context.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (!/^https?:$/.test(url.protocol)) return route.continue();
      if (url.origin !== base) {
        external.push(url.origin);
        return route.abort();
      }
      if (version === "before" && !url.pathname.startsWith("/api/")) {
        const relative =
          url.pathname === "/"
            ? "index.html"
            : decodeURIComponent(url.pathname.slice(1));
        const file = path.resolve(root, "web-original", relative);
        if (!file.startsWith(path.resolve(root, "web-original") + path.sep))
          throw Error("Asset path escapes baseline");
        const types = {
          ".html": "text/html",
          ".js": "text/javascript",
          ".css": "text/css",
          ".svg": "image/svg+xml",
        };
        try {
          return await route.fulfill({
            status: 200,
            contentType:
              types[path.extname(file)] || "application/octet-stream",
            body: await fs.readFile(file),
          });
        } catch {
          return route.fulfill({ status: 404, body: "Not found" });
        }
      }
      return route.continue();
    });
    const page = await context.newPage();
    page.on("pageerror", (e) => errors.push({ version, message: e.message }));
    await page.goto(base + "/#token=" + token);
    await page.getByLabel("当前项目").selectOption(project.id);
    await page.getByLabel("当前识别结果").selectOption(modelResult);
    await expect(
      page.getByRole("button", { name: "开始识别", exact: true }),
    ).toBeEnabled();
    await page
      .locator(".result-tabs")
      .getByRole("button", { name: /^表格/ })
      .click();
    await expect(
      page.getByRole("textbox", { name: "第 2 行第 1 列", exact: true }),
    ).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    for (const [width, height] of [
      [1920, 1080],
      [1440, 900],
      [1366, 768],
    ]) {
      await page.setViewportSize({ width, height });
      await page.screenshot({
        animations: "disabled",
        path: path.join(out, `${version}-${width}.png`),
      });
      views.push({
        version,
        width,
        height,
        overflow: await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
        font: await page
          .locator(".editable-grid textarea")
          .first()
          .evaluate((el) => ({
            family: getComputedStyle(el).fontFamily,
            size: getComputedStyle(el).fontSize,
            localFontLoaded: document.fonts.check('14px "Workbench Sans"'),
          })),
      });
    }
    if (version === "after") {
      let empty = projects.find((p) => p.name === "新项目 · 界面示例");
      if (!empty)
        empty = await api("/projects", "POST", { name: "新项目 · 界面示例" });
      await page.reload();
      await page.getByLabel("当前项目").selectOption(empty.id);
      await page.setViewportSize({ width: 1440, height: 900 });
      await expect(page.locator(".empty-illustration")).toBeVisible();
      expect(
        await page
          .locator(".empty-illustration")
          .evaluate((el) => el.naturalWidth > 0),
      ).toBe(true);
      await page.screenshot({
        animations: "disabled",
        path: path.join(out, "after-empty.png"),
      });
      await page.getByLabel("当前项目").selectOption(project.id);
    }
    await context.close();
  }
  expect((await api("/results/" + modelResult)).original).toEqual(
    originalBefore,
  );
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
  expect(
    views.filter((v) => v.version === "after").every((v) => !v.overflow),
  ).toBe(true);
  await fs.writeFile(
    path.join(out, "result.json"),
    JSON.stringify(
      {
        passed: true,
        scope:
          "Same synthetic source and real model result, baseline assets supplied to browser from preserved web-original; no application assets or model output changed",
        project: project.id,
        result: modelResult,
        original_sha256: crypto
          .createHash("sha256")
          .update(JSON.stringify(originalBefore))
          .digest("hex"),
        views,
        errors,
        externalRequests: external,
        bundle_manifest_sha256: crypto
          .createHash("sha256")
          .update(await fs.readFile(path.join(root, "bundle/manifest.json")))
          .digest("hex"),
      },
      null,
      2,
    ),
  );
} finally {
  await browser.close();
}
