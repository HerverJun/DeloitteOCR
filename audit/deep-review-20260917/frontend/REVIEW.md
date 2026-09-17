# 前端功能复盘（2026-09-17）

范围：App、编辑器持久化与恢复、文档导航/复核队列、表格/文字交互、任务队列、复制与导出。未改动产品源码；未启动浏览器。下面的组件复现直接转译并执行当前 TS/TSX，控制 React hooks、网络与子组件边界，属于源级行为验证，不冒充浏览器端到端验证。

## 已证实的缺陷

### F1 · P1 · 页面跳转后“处理当前页”可能处理另一页

- 位置：`frontend/src/DocumentTree.tsx:33`；动作 `:117–118`；选择标记 `:107`。其他页面入口只更新 `active`，如 `frontend/src/App.tsx:365–369`、`:515–518`、`:1007–1026`。
- 前置：同一 PDF 有两页，先在文档树打开第 1 页。
- 复现：经任务队列“查看”或“下一张待校对”打开第 2 页，再点击文档树“处理当前页”。
- 预期：请求第 2 页的 `/pages/<page-2>/process`。
- 实际：`current` 优先旧 `selectedPage`，请求第 1 页；页面树同时有两个 `aria-current="page"`。
- 用户影响：预览与操作目标分离，尤其“整页重新 OCR”会对非预期页发起重新处理。原数据是否被最终替换取决于处理模式及后端采用逻辑，不能把本条夸大为已证实的数据删除。
- 证据：`component-repros.json` 的 `document-current-page-desync`，记录 expected page-2、actual page-1。
- 建议：令页面选择与工作区 activeImage 具有统一来源；对外部跳转同步文档、分页 offset、selectedPage 与 jump。不要仅改高亮而留下操作目标。

### F2 · P2 · 带来源证据的结果点击“复制”会得到 ZIP 乱码

- 位置：`frontend/src/App.tsx:1203–1212`；后端归档分支 `src/ocr_workbench/exporting.py:240–274`。
- 前置：融合结果，或带已采用结构来源/任意多模态审校请求来源的结果。
- 复现：打开结果并点击“复制”。
- 预期：剪贴板为已保存校对文字。
- 实际：前端以 `format: "txt"` 请求 `/export`，直接把 `response.text()` 写入剪贴板；该导出在存在来源信息时实际返回 ZIP。UI 仍提示“文字已复制”。
- 实验：真实临时 Store 创建三引擎合成结果并运行 CPU fusion，`build_export(..., "txt")` 返回 `OCR-fusion-export.zip`，文件头 `504b0304`。UTF-8 解码后约 3,130 字符；实际所需文字仅 `项目\t金额\n项目甲\t00123`。ZIP 内 `OCR-result.txt` 才是正确文本。
- 证据：`repro-copy-export.py`、`copy-export-repro.json`。普通单引擎结果的对照导出为 `.txt`。
- 建议：复制使用明确的文本呈现接口或本地已保存结果呈现；下载继续保留来源归档。应校验响应类型，避免把归档内容作为可复制文字。

### F3 · P2 · 曾预览其他结果后，文档复核队列无法打开目标结果

- 位置：`frontend/src/App.tsx:855–859`；结果优先级 `:290–292`；复核定位 guard `:166`；`frontend/src/DocumentTree.tsx:125–126`。
- 前置：同一页采用结果 B 有复核任务；用户曾通过“当前识别结果”下拉预览未采用结果 A。
- 复现：点击文档复核队列中对应 B 的条目。
- 预期：加载 B 并打开其结构/融合/视觉审校页签，定位目标建议。
- 实际：`goTo` 仅回到图片；`onReview` 仅设置复核目标。`previews[imageId]` 仍为 A，currentResult 优先 A；效果钩子因实际结果 ID 不是 B 直接返回。仍停留 A 原页签。
- 证据：实际 App 源级回调测试 `document-review-cannot-replace-sticky-preview`：期望 B/structure，实际加载 A/text。
- 建议：复核跳转作为一个带 result_id 的导航动作，明确替换该页的预览选择，完成加载后再定位 issue/proposal。

## 通过的关键边界控制

`repro-components.mjs` 另外执行四组正向控制，均通过：

1. 第一次保存响应延迟时继续修改，串行 PUT 正确使用修订 0、1，最后保存第二份文字，恢复副本清理。
2. 保存失败时阻止切换到其他结果；显式重新加载再失败仍保留当前结果、文字草稿和恢复副本。
3. 恢复旧修订草稿时保留原 expected revision，服务器修订为 3 时仍提交 1 并接收冲突，未静默覆盖新内容。
4. 较早 GET 晚返回时不能替换后打开结果；快速校对未提交草稿阻止切换。

这些验证说明保存串行化、恢复冲突与普通结果加载竞争防护已有较好基础。本轮发现主要集中在跨组件导航状态和功能之间的响应格式契约。

## 测试与设计复盘

- 前端现有 Vitest 主要覆盖纯函数。`useEditor` 没有现成测试文件，DocumentTree/App 的组合状态也未被覆盖；本轮补充的源级实验仅放入审计目录。
- `scripts/verify_functional_regressions.mjs:189–195` 的复制断言比较“剪贴板 == 同一个导出接口 response.text()”，该断言无法识别“双方同为 ZIP 乱码”，且其内容断言仅使用普通单引擎样本。应按用户期望的纯文本断言，并加入来源归档结果。
- 页面、图片、预览结果、采用结果、复核目标分别存储，单个组件里的检查可以正确，但跨入口无法保证它们始终指向同一上下文。应形成包含 document/page/image/version/result/revision/issue 的明确导航上下文，并测试至少“树→任务”“预览→复核”“复核→下一页”。
- 导出接口同时承担“文本呈现”和“带证据归档”，导致调用方不能仅按请求 format 推断返回格式。需要明确返回类型契约。

## 复跑

```powershell
node audit/deep-review-20260917/frontend/repro-components.mjs
& 'D:/OCR-multimodal-workbench-20260917/bundle/runtimes/service/python.exe' -B -X utf8 audit/deep-review-20260917/frontend/repro-copy-export.py
```

Python 使用临时目录及合成数据，清理由 TemporaryDirectory 执行；不会访问实际工作区用户项目或运行模型。系统 `D:/anaconda3/python.exe` 缺少 `pillow_heif`，不适合本项目集成复现。
