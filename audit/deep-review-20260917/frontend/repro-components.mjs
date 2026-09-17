/** Source-level component tests, no browser or changes to production files.
 * TypeScript transpiles the actual component; React hooks and network boundaries
 * are controlled to expose concrete state transitions and outgoing requests.
 */
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const requireFrontend = createRequire(path.join(root, "frontend/package.json"));
const ts = requireFrontend("typescript");
const records = [];
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness(filename, mocks, initialStates = []) {
  let stateIndex = 0, refIndex = 0;
  const states = initialStates.slice(), refs = [];
  const effects = [];
  const hooks = {
    useState(initial) {
      const index = stateIndex++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }];
    },
    useRef(initial) { const index = refIndex++; return refs[index] ||= { current: initial }; },
    useEffect(fn, deps) { effects.push({ fn, deps }); },
    useCallback(fn) { return fn; },
    useMemo(fn) { return fn(); },
  };
  const code = ts.transpileModule(readFileSync(path.join(root, filename), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const module = { exports: {} };
  const jsx = (type, props, key) => ({ type, props: props || {}, key });
  const stub = new Proxy({}, { get: (_, key) => String(key) });
  const context = {
    module, exports: module.exports, console, URLSearchParams, Date, structuredClone,
    setTimeout: () => 1, clearTimeout: () => {},
    window: { addEventListener() {}, removeEventListener() {} },
    require(name) {
      if (name === "react") return hooks;
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
      if (name in mocks) return mocks[name];
      if (name.endsWith(".css")) return {};
      return stub;
    },
  };
  vm.runInNewContext(code, context, { filename });
  return {
    states, refs, effects,
    render(exportName, props) { stateIndex = 0; refIndex = 0; effects.length = 0; return module.exports[exportName](props); },
  };
}

function elements(tree) {
  const result = [];
  function walk(value) {
    if (Array.isArray(value)) { value.forEach(walk); return; }
    if (!value || typeof value !== "object") return;
    if (value.type && value.props) { result.push(value); Object.values(value.props).forEach(walk); }
  }
  walk(tree); return result;
}
function nodeText(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  return node?.props ? nodeText(node.props.children) : "";
}

// Drive the real App document-review callback with an existing non-adopted preview.
// A queue item points to adopted B, but the old preview A remains the load target.
{
  const requests = [], loadTargets = [];
  const editor = {
    result: { id: "preview-A", revision: 0, edited: { text: "Preview A", tables: [] },
      original: { blocks: [], tables: [], engine: "ppocr", project_image_version: "version-1" } },
    edit: { text: "Preview A", tables: [] },
    saveState: "已保存", loadState: "ready", getReviewDraft: () => null,
    getPendingDecision: () => null, flush: async () => {}, load: async id => { loadTargets.push(id); },
  };
  const project = { project: { id: "project", name: "Review fixture" },
    images: [{ id: "image-1", active_version: "version-1", selected_result: "adopted-B", name: "Page 1", document_kind: "pdf" }],
    documents: [{ id: "doc", name: "review.pdf", kind: "pdf", page_count: 1 }],
    versions: [{ id: "version-1", image_id: "image-1" }], tasks: [], queue: {} };
  const seed = [];
  seed[0] = [project.project]; seed[1] = "project"; seed[2] = project; seed[4] = "image-1";
  seed[7] = "text"; seed[9] = false; seed[33] = { "image-1": "preview-A" };
  const h = harness("frontend/src/App.tsx", {
    "./api": { api: async (...args) => { requests.push(args); return {}; } },
    "./useEditor": { useEditor: () => editor },
    "./RecognitionBar": { RecognitionBar: "RecognitionBar", modes: [{ id: "table", engine: "paddlevl" }] },
    "./workspacePreferences": { usePreference: (_, fallback) => [fallback, () => {}], readPreference: (_, fallback) => fallback },
    "./resultWorkflow": { adoptedResult: photo => photo.selected_result, exportPhotos: photos => photos, reviewNames: { pending: "待校对" } },
    "./documentText": { documentText: edit => ({ text: edit.text }) },
    "./types": { engineNames: { ppocr: "PP-OCR" } },
    "./DocumentReviewQueue": { documentReviewTab: kind => kind === "fusion" ? "review" : "structure" },
  }, seed);
  const tree = h.render("App");
  const documentTree = elements(tree).find(node => node.type === "DocumentTree");
  const task = { result_id: "adopted-B", kind: "structure", page_number: 1, proposal_ids: ["proposal-B"], target: {} };
  await documentTree.props.onOpen("image-1");
  await documentTree.props.onReview(task);
  h.render("App");
  // Run only the review-target and result-load effects, leaving startup and polling inert.
  const targetEffect = h.effects.find(effect => effect.deps?.[0] === task);
  const loadEffect = h.effects.find(effect => effect.deps?.length === 2 && effect.deps[0] === "image-1" && effect.deps[1] === "preview-A");
  assert.ok(targetEffect); assert.ok(loadEffect);
  targetEffect.fn(); loadEffect.fn(); await tick();
  assert.equal(h.states[7], "text");
  assert.equal(h.states[33]["image-1"], "preview-A");
  assert.equal(loadTargets.at(-1), "preview-A");
  records.push({ name: "document-review-cannot-replace-sticky-preview", reproduced: true,
    review_item_result: "adopted-B", expected_loaded_result: "adopted-B", actual_loaded_result: loadTargets.at(-1),
    expected_tab: "structure", actual_tab: h.states[7], source: "frontend/src/App.tsx:166,290-292,855-859" });
}

// User selected page 1 in the tree, then App selected page 2 through task/review navigation.
{
  const requests = [];
  const pages = [1, 2].map(n => ({ id: `page-${n}`, document_id: "doc", page_number: n,
    image_id: `image-${n}`, active_version: `version-${n}`, status: "processed", stage_status: "succeeded" }));
  const h = harness("frontend/src/DocumentTree.tsx", {
    "./api": { api: async (url, method, body) => { requests.push({ url, method, body }); return {}; } },
  }, ["doc", pages, 0, "1", "page-1"]);
  const tree = h.render("DocumentTree", {
    documents: [{ id: "doc", name: "two-pages.pdf", page_count: 2, status: "ready" }],
    activeImage: "image-2", reviewOnly: false,
    beforeOpen: async () => {}, onOpen: async () => {}, onRefresh: async () => {},
    onError: error => { throw Error(error); }, onReview: async () => {},
  });
  const nodes = elements(tree);
  const current = nodes.filter(node => node.props["aria-current"] === "page");
  assert.equal(current.length, 2);
  nodes.find(node => node.type === "Button" && nodeText(node) === "处理当前页").props.onClick();
  await tick();
  assert.equal(requests[0].url, "/pages/page-1/process");
  records.push({ name: "document-current-page-desync", reproduced: true, workspace_active_image: "image-2",
    expected_process_url: "/pages/page-2/process", actual_process_url: requests[0].url,
    aria_current_page_count: current.length, source: "frontend/src/DocumentTree.tsx:33,107,118" });
}

// On a recovered manual issue draft, ordinary edits and navigation are guarded.
// Verify useEditor saves serially and preserves an edit made during a slow save.
{
  let release;
  const requests = [], recoveries = [];
  let server = { id: "result", revision: 0, edited: { text: "original", tables: [] } };
  const h = harness("frontend/src/useEditor.ts", {
    "./api": { api: async (url, method, body) => {
      requests.push({ url, method, body });
      if (method === "PUT") {
        if (requests.filter(r => r.method === "PUT").length === 1) await new Promise(resolve => { release = resolve; });
        assert.equal(body.revision, server.revision);
        server = { ...server, revision: server.revision + 1, edited: body.edited };
      }
      return structuredClone(server);
    } },
    "./editorRecovery": { recoveryClient: () => "client", readRecovery: () => null,
      writeRecovery: (client, value) => recoveries.push(structuredClone(value)) },
    "./documentText": { editableResult: result => result.edited, savedEdit: value => value },
  });
  let editor = h.render("useEditor", error => { throw Error(error); });
  await editor.load("result");
  editor = h.render("useEditor", error => { throw Error(error); });
  editor.change({ text: "first draft", tables: [] });
  const save = editor.flush(); await tick();
  editor.change({ text: "second draft during first save", tables: [] });
  release(); await save;
  editor = h.render("useEditor", error => { throw Error(error); });
  assert.equal(server.edited.text, "second draft during first save");
  assert.equal(server.revision, 2);
  assert.equal(editor.edit.text, server.edited.text);
  assert.equal(editor.saveState, "已保存");
  assert.equal(recoveries.at(-1).edit, null);
  records.push({ name: "serialized-autosave-race-control", passed: true,
    saved_revisions: requests.filter(r => r.method === "PUT").map(r => r.body.revision),
    final_text: server.edited.text, recovery_cleared: recoveries.at(-1).edit === null });
}

// Save failure must stop navigation; failed explicit reload must retain the draft.
{
  const errors = [], requests = [], recoveries = [];
  let failGet = false;
  const original = { id: "result", revision: 0, edited: { text: "original", tables: [] } };
  const h = harness("frontend/src/useEditor.ts", {
    "./api": { api: async (url, method, body) => {
      requests.push({ url, method, body });
      if (method === "PUT" || failGet) throw Error("injected network failure");
      return structuredClone(original);
    } },
    "./editorRecovery": { recoveryClient: () => "client", readRecovery: () => null,
      writeRecovery: (_, value) => recoveries.push(structuredClone(value)) },
    "./documentText": { editableResult: result => result.edited, savedEdit: value => value },
  });
  const onError = error => errors.push(error);
  let editor = h.render("useEditor", onError);
  await editor.load("result");
  editor.change({ text: "must survive failed save and reload", tables: [] });
  await assert.rejects(editor.load("other-result"), /injected network failure/);
  assert.ok(!requests.some(request => request.url.endsWith("other-result")));
  failGet = true;
  await assert.rejects(editor.reload(), /injected network failure/);
  editor = h.render("useEditor", onError);
  assert.equal(editor.getCurrent().id, "result");
  assert.equal(editor.edit.text, "must survive failed save and reload");
  assert.equal(recoveries.at(-1).edit.text, editor.edit.text);
  records.push({ name: "failed-save-and-reload-retain-draft", passed: true,
    original_result_retained: true, navigation_prevented: true, recovery_draft_retained: true });
}

// A recovered stale draft keeps its old revision so it cannot overwrite another window.
{
  const requests = [], recoveries = [], errors = [];
  const h = harness("frontend/src/useEditor.ts", {
    "./api": { api: async (url, method, body) => {
      requests.push({ url, method, body });
      if (method === "PUT") { assert.equal(body.revision, 1); throw Error("revision conflict"); }
      return { id: "result", revision: 3, edited: { text: "newer server edit", tables: [] } };
    } },
    "./editorRecovery": { recoveryClient: () => "client",
      readRecovery: () => ({ revision: 1, edit: { text: "recovered old draft", tables: [] }, decision: null, review: null }),
      writeRecovery: (_, value) => recoveries.push(structuredClone(value)) },
    "./documentText": { editableResult: result => result.edited, savedEdit: value => value },
  });
  const onError = error => errors.push(error);
  let editor = h.render("useEditor", onError);
  await editor.load("result");
  await assert.rejects(editor.flush(), /revision conflict/);
  editor = h.render("useEditor", onError);
  assert.equal(editor.edit.text, "recovered old draft");
  assert.equal(recoveries.at(-1).revision, 1);
  records.push({ name: "recovered-revision-conflict-does-not-overwrite", passed: true,
    server_revision: 3, submitted_revision: requests.find(request => request.method === "PUT").body.revision,
    recovery_draft_retained: true });
}

// Late completion of the old GET must not replace the newly opened result.
{
  let release;
  const h = harness("frontend/src/useEditor.ts", {
    "./api": { api: async url => {
      const id = url.split("/").at(-1);
      if (id === "slow") await new Promise(resolve => { release = resolve; });
      return { id, revision: 0, edited: { text: id, tables: [] } };
    } },
    "./editorRecovery": { recoveryClient: () => "client", readRecovery: () => null, writeRecovery: () => {} },
    "./documentText": { editableResult: result => result.edited, savedEdit: value => value },
  });
  let editor = h.render("useEditor", error => { throw Error(error); });
  const oldLoad = editor.load("slow"); await tick();
  await editor.load("fast"); release(); await oldLoad;
  editor = h.render("useEditor", error => { throw Error(error); });
  assert.equal(editor.result.id, "fast");
  assert.equal(editor.edit.text, "fast");
  editor.setReviewDraft({ issueId: "issue", value: "manual unfinished" });
  await assert.rejects(editor.load("third"), /未提交/);
  assert.equal(editor.getCurrent().id, "fast");
  assert.equal(editor.getReviewDraft().value, "manual unfinished");
  records.push({ name: "out-of-order-load-and-review-draft-navigation", passed: true,
    late_read_ignored: true, manual_draft_navigation_prevented: true });
}

writeFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), "component-repros.json"), JSON.stringify(records, null, 2));
console.log(JSON.stringify(records, null, 2));
