// Execute the current TSX with controlled hooks/network; no browser or model use.
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
const output = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(output, "../../..");
const ts = createRequire(path.join(root, "frontend/package.json"))("typescript");
const results = [];
const tick = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function harness(filename, mocks, seed = []) {
  let stateIndex = 0, refIndex = 0, effectIndex = 0, timerId = 0;
  const states = seed.slice(), refs = [], effects = [], pending = [], timers = new Map();
  const h = { states, effects, timers, dirty: false };
  const hooks = {
    useState(initial) {
      const index = stateIndex++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index], value => { const next = typeof value === "function" ? value(states[index]) : value;
        if (!Object.is(next, states[index])) { states[index] = next; h.dirty = true; } }];
    },
    useRef(initial) { const index = refIndex++; return refs[index] ||= { current: initial }; },
    useEffect(fn, deps) {
      const index = effectIndex++, old = effects[index];
      if (!old || deps.some((value, i) => !Object.is(value, old.deps[i]))) {
        effects[index] = { fn, deps, cleanup: old?.cleanup }; pending.push(index);
      }
    },
    useCallback(fn) { return fn; }, useMemo(fn) { return fn(); },
  };
  const jsx = (type, props, key) => ({ type, props: props || {}, key });
  const stub = new Proxy({}, { get: (_, key) => String(key) });
  const cache = new Map();
  function load(file) {
    if (cache.has(file)) return cache.get(file);
    const code = ts.transpileModule(readFileSync(path.join(root, file), "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    const module = { exports: {} };
    const context = { module, exports: module.exports, console, URLSearchParams, Date, structuredClone,
      setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); },
      require(name) {
        if (name === "react") return hooks;
        if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
        if (name in mocks) return mocks[name];
        if (["./documentNavigation", "./resultClipboard"].includes(name)) return load(`frontend/src/${name.slice(2)}.ts`);
        if (name.endsWith(".css")) return {};
        return stub;
      } };
    vm.runInNewContext(code, context, { filename: file }); cache.set(file, module.exports); return module.exports;
  }
  const component = load(filename);
  h.render = (name, props) => { stateIndex = 0; refIndex = 0; effectIndex = 0; h.dirty = false; return component[name](props); };
  h.commit = () => { for (const index of pending.splice(0)) { const effect = effects[index]; effect.cleanup?.(); effect.cleanup = effect.fn(); } };
  h.poll = async () => { const pair = [...timers].find(([, timer]) => timer.ms === 1200); assert.ok(pair); timers.delete(pair[0]); pair[1].fn(); await tick(); };
  return h;
}
async function settle(h, name, props) {
  for (let i = 0; i < 12; i++) { const tree = h.render(name, props); h.commit(); await tick(); if (!h.dirty) return tree; }
  throw Error("component did not settle");
}
function nodes(tree) {
  const out = [];
  const visit = value => { if (Array.isArray(value)) return value.forEach(visit);
    if (value?.type && value.props) { out.push(value); Object.values(value.props).forEach(visit); } };
  visit(tree); return out;
}
const text = node => typeof node === "string" || typeof node === "number" ? String(node) : Array.isArray(node) ? node.map(text).join("") : node?.props ? text(node.props.children) : "";
const find = (tree, label) => nodes(tree).find(node => text(node) === label && ["Button", "button"].includes(node.type));
const page = (document, n, rendered = true) => ({ id: `${document}-page-${n}`, document_id: document, page_number: n,
  image_id: rendered ? `${document}-image-${n}` : null, active_version: rendered ? `${document}-version-${n}` : null, status: "ready", stage_status: null });
const documents = ["A", "B"].map(id => ({ id, name: id + ".pdf", kind: "pdf", page_count: 65, status: "ready" }));
function treeFixture(extraApi = async () => ({})) {
  const requests = [], opened = [], pages = documents.flatMap(doc => Array.from({ length: 65 }, (_, i) => page(doc.id, i + 1)));
  const props = { documents, activeImage: "A-image-1", activePage: { documentId: "A", pageNumber: 1 }, reviewOnly: false,
    beforeOpen: async () => {}, onOpen: async (id, current) => { if (!current || current()) opened.push(id); },
    onRefresh: async () => {}, onError: error => { throw Error(error); }, onReview: async () => {} };
  const h = harness("frontend/src/DocumentTree.tsx", { "./api": { api: async (url, method, body) => {
    requests.push({ url, method, body });
    const match = url.match(/^\/documents\/([^/]+)\/pages\?offset=(\d+)&limit=(\d+)/);
    if (match) return { pages: pages.filter(p => p.document_id === match[1]).slice(+match[2], +match[2] + +match[3]) };
    return extraApi(url, method, body);
  } } });
  return { h, props, requests, opened, pages };
}

// Start on a non-first document and page 31, then cross documents and independent images.
{
  const f = treeFixture(); f.props.activeImage = "B-image-31"; f.props.activePage = { documentId: "B", pageNumber: 31 };
  let tree = await settle(f.h, "DocumentTree", f.props);
  assert.equal(nodes(tree).find(n => n.props["aria-label"] === "选择文档").props.value, "B");
  assert.equal(nodes(tree).find(n => n.props["aria-label"] === "跳转页码").props.value, "31");
  assert.equal(nodes(tree).filter(n => n.props["aria-current"] === "page").length, 1);
  assert.ok(f.requests.some(r => r.url === "/documents/B/pages?offset=30&limit=30"));
  find(tree, "处理当前页").props.onClick(); await tick();
  assert.equal(f.requests.find(r => r.method === "POST").url, "/pages/B-page-31/process");
  f.props.activeImage = "independent"; f.props.activePage = null;
  tree = await settle(f.h, "DocumentTree", f.props);
  assert.equal(find(tree, "处理当前页").props.disabled, true);
  assert.equal(nodes(tree).filter(n => n.props["aria-current"] === "page").length, 0);
  f.props.activeImage = "A-image-2"; f.props.activePage = { documentId: "A", pageNumber: 2 };
  tree = await settle(f.h, "DocumentTree", f.props);
  assert.equal(nodes(tree).find(n => n.props["aria-label"] === "选择文档").props.value, "A");
  assert.equal(nodes(tree).find(n => n.props["aria-label"] === "跳转页码").props.value, "2");
  find(tree, "处理当前页").props.onClick(); await tick();
  assert.equal(f.requests.filter(r => r.method === "POST").at(-1).url, "/pages/A-page-2/process");
  results.push({ name: "external-document-page-31-and-independent-image", passed: true });
}

// The pending-render guard must still allow an uninterrupted requested page to open.
{
  const f = treeFixture(); f.pages[1] = page("A", 2, false);
  let tree = await settle(f.h, "DocumentTree", f.props);
  nodes(tree).find(n => n.type === "button" && n.props.children?.some?.(child => text(child) === "第 2 页")).props.onClick();
  await tick(); await settle(f.h, "DocumentTree", f.props);
  f.pages[1] = page("A", 2); await f.h.poll();
  assert.deepEqual(f.opened, ["A-image-2"]);
  results.push({ name: "requested-render-opens-when-navigation-is-unchanged", passed: true });
}

// Finishing a render request cannot pull the workspace back after external navigation.
{
  const render = deferred(); const f = treeFixture(async url => url.endsWith("/render") ? render.promise : {});
  f.pages[1] = page("A", 2, false);
  let tree = await settle(f.h, "DocumentTree", f.props);
  nodes(tree).find(n => n.type === "button" && n.props.children?.some?.(child => text(child) === "第 2 页")).props.onClick();
  await tick();
  f.props.activeImage = "A-image-3"; f.props.activePage = { documentId: "A", pageNumber: 3 };
  await settle(f.h, "DocumentTree", f.props);
  f.pages[1] = page("A", 2); render.resolve({}); await tick(); await f.h.poll();
  assert.deepEqual(f.opened, []);
  results.push({ name: "late-render-does-not-reopen-old-page", passed: true });
}

// A render already ready at polling time is still guarded across project refresh.
{
  const f = treeFixture(), refresh = deferred(); f.pages[1] = page("A", 2, false);
  let tree = await settle(f.h, "DocumentTree", f.props);
  nodes(tree).find(n => n.type === "button" && n.props.children?.some?.(child => text(child) === "第 2 页")).props.onClick();
  await tick(); await settle(f.h, "DocumentTree", f.props);
  f.props.onRefresh = () => refresh.promise; await settle(f.h, "DocumentTree", f.props);
  f.pages[1] = page("A", 2); await f.h.poll();
  f.props.activeImage = "independent"; f.props.activePage = null;
  await settle(f.h, "DocumentTree", f.props); refresh.resolve(); await tick();
  assert.deepEqual(f.opened, []);
  results.push({ name: "ready-render-refresh-race-is-cancelled", passed: true });
}

// Changing the active page during flush prevents the old page processing POST.
{
  const f = treeFixture(), flush = deferred();
  f.props.beforeOpen = () => flush.promise;
  let tree = await settle(f.h, "DocumentTree", f.props);
  find(tree, "处理当前页").props.onClick(); await tick();
  f.props.activeImage = "A-image-2"; f.props.activePage = { documentId: "A", pageNumber: 2 };
  await settle(f.h, "DocumentTree", f.props); flush.resolve(); await tick();
  assert.ok(!f.requests.some(r => r.url.endsWith("/process")));
  results.push({ name: "process-page-flush-navigation-race", passed: true });
}

// The real App callback loads the queue result before scheduling its review target.
{
  const loads = [], order = [];
  const result = id => ({ id, revision: 0, edited: { text: id, tables: [] }, original: { blocks: [], tables: [], engine: "ppocr", project_image_version: "version" } });
  const editor = { result: result("preview-A"), edit: { text: "A", tables: [] }, saveState: "已保存", loadState: "ready",
    getReviewDraft: () => null, getPendingDecision: () => null,
    flush: async () => { order.push("flush"); }, load: async id => { order.push("load"); loads.push(id); editor.result = result(id); },
    getCurrent: () => editor.result };
  const project = { project: { id: "project", name: "fixture" }, documents: [{ id: "doc", name: "doc", kind: "pdf", page_count: 1 }],
    images: [{ id: "image", active_version: "version", selected_result: "adopted-B", name: "page", document_kind: "pdf", document_id: "doc", page_number: 1 }],
    versions: [{ id: "version", image_id: "image" }], tasks: [], queue: {} };
  const seed = []; seed[0] = [project.project]; seed[1] = "project"; seed[2] = project; seed[4] = "image";
  seed[7] = "text"; seed[9] = false; seed[33] = { image: "preview-A" };
  const h = harness("frontend/src/App.tsx", {
    "./api": { api: async () => ({}) }, "./useEditor": { useEditor: () => editor },
    "./RecognitionBar": { RecognitionBar: "RecognitionBar", modes: [{ id: "table", engine: "paddlevl" }] },
    "./workspacePreferences": { usePreference: (_, fallback) => [fallback, () => {}], readPreference: (_, fallback) => fallback },
    "./resultWorkflow": { adoptedResult: photo => photo.selected_result, exportPhotos: photos => photos, reviewNames: { pending: "待校对" } },
    "./documentText": { documentText: edit => ({ text: edit.text }) }, "./types": { engineNames: { ppocr: "OCR" } },
    "./DocumentReviewQueue": { documentReviewTab: () => "structure" },
  }, seed);
  const tree = h.render("App"), callback = nodes(tree).find(n => n.type === "DocumentTree").props.onReview;
  const task = { image_id: "image", result_id: "adopted-B", kind: "structure", page_number: 1, proposal_ids: ["proposal"], target: {} };
  await callback(task); h.render("App");
  h.effects.find(e => e.deps?.[0] === task).fn();
  assert.deepEqual(order, ["flush", "load"]); assert.deepEqual(loads, ["adopted-B"]);
  assert.equal(h.states[33].image, "adopted-B"); assert.equal(h.states[7], "structure");
  results.push({ name: "review-navigation-replaces-sticky-preview-before-focus", passed: true });
}

writeFileSync(path.join(output, "component-results.json"), JSON.stringify(results, null, 2));
console.log(JSON.stringify(results, null, 2));
