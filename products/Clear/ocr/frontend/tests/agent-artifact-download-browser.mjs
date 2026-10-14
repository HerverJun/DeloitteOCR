/** Real React download clicks against a local synthetic product HTTP service. */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
import { chromium, expect } from '@playwright/test';

const [backend, project, , completeId, partialId, output] = process.argv.slice(2);
if (!backend?.startsWith('http://127.0.0.1:') || !project || !completeId || !partialId || !output) {
  throw Error('Expected explicit loopback backend, project, artifact IDs and output');
}
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const receipt = { status: 'running', scope: 'React AgentArtifact + actual loopback product HTTP; synthetic data; no real model/OCR' };
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0, open: false,
  proxy: { '/api': { target: backend, changeOrigin: true } } },
  plugins: [{ name: 'artifact-test-page', configureServer(vite) {
    vite.middlewares.use(async (req, res, next) => {
      if (req.url?.split('?')[0] !== '/__artifact_download_test.html') return next();
      const html = `<!doctype html><html><body><div id="root"></div><script type="module">
        import React from 'react'; import {createRoot} from 'react-dom/client';
        import {AgentArtifact} from '/src/AgentArtifact.tsx';
        const ids = ${JSON.stringify({ project, completeId, partialId })};
        const action = work => work();
        createRoot(document.getElementById('root')).render(React.createElement('main', null,
          React.createElement('section', {id:'complete'}, React.createElement(AgentArtifact, {projectId:ids.project,id:ids.completeId,busy:false,action})),
          React.createElement('section', {id:'partial'}, React.createElement(AgentArtifact, {projectId:ids.project,id:ids.partialId,busy:false,action}))));
      </script></body></html>`;
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(await vite.transformIndexHtml(req.url, html));
    });
  } }] });
let browser;
try {
  await server.listen();
  const base = server.resolvedUrls.local[0].replace(/\/$/, '');
  browser = await chromium.launch({ channel: 'msedge', headless: true, args: ['--disable-gpu', '--disable-gpu-compositing'] });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto(base + '/__artifact_download_test.html#token=synthetic-http-token');
  const full = page.locator('#complete'), partial = page.locator('#partial');
  await expect(full).toContainText('导出已保存');
  await expect(partial).toContainText('部分导出，请同时下载覆盖清单');
  await expect(full.getByRole('button', { name: '下载覆盖清单' })).toHaveCount(0);
  await expect(partial.getByRole('button', { name: '下载覆盖清单' })).toBeEnabled();
  async function click(scope, label, filename) {
    const [download] = await Promise.all([page.waitForEvent('download'), scope.getByRole('button', { name: label }).click()]);
    assert.equal(download.suggestedFilename(), filename);
    const bytes = await fs.readFile(await download.path());
    return { bytes, sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
  }
  const complete = await click(full, '下载导出文件', 'export.txt');
  const exported = await click(partial, '下载导出文件', 'export-partial.txt');
  const sidecar = await click(partial, '下载覆盖清单', 'export-partial-manifest.json');
  const manifest = JSON.parse(sidecar.bytes.toString('utf8'));
  assert.equal(manifest.sha256, exported.sha256);
  assert.equal(manifest.bytes, exported.bytes.length);
  assert.ok(manifest.manifest.coverage.failed_pages.length);
  assert.deepEqual(errors, []);
  receipt.status = 'pass';
  receipt.complete_sha256 = complete.sha256;
  receipt.partial_sha256 = exported.sha256;
  receipt.partial_manifest_sha256 = sidecar.sha256;
  receipt.buttons = { complete_manifest: false, partial_manifest: true };
  await context.close();
} catch (error) {
  receipt.status = 'fail';
  receipt.error = String(error);
  throw error;
} finally {
  await browser?.close();
  await server.close();
  await fs.writeFile(output, JSON.stringify(receipt, null, 2) + '\n');
}
