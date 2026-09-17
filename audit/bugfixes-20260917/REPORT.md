# 八项功能缺陷修复与回归

2026-09-17。基线为 `a9d3974`，源码版本 0.11.0rc1 / schema 11。本轮关闭[深度审计](../deep-review-20260917/REPORT.md)中的 R1–R8；在交叉审查中进一步修复全文处理遇到冲突时的部分入队问题。无需数据库迁移。

修复位于当前源码与前端构建。`D:/OCR-multimodal-workbench-20260917/bundle` 仅提供测试依赖，已有便携包及安装目录没有更新。本轮没有重新运行 GPU 模型评测，不改变原审计记录的模型错误、定位质量和目标机器验收边界。

## 缺陷关闭情况

| 编号 | 修复后的行为 | 主要回归证据 |
|---|---|---|
| R1 | 页面操作以当前工作区图像为准。外部跳页同步文档、页码和 30 页窗口；独立图片清除旧页操作目标；旧渲染响应或保存等待结束后不会拉回旧页、处理旧页。 | 浏览器独立重建“树选 1 页 → 任务查看 2 页 → 处理当前页”，核对真实 POST ID；组件探针覆盖跨文档第 31 页和异步竞态。 |
| R2 | 复制先保存，再读取独立纯文本接口；校验结果身份与 Content-Type。正式下载保留来源 ZIP。 | 浏览器核对 Unicode、引号、路径、前导零、制表符及公式字面量；4 条后端用例覆盖普通、融合、结构修订和视觉审校结果，并检查来源下载与原始结果不变。 |
| R3 | 从文档复核队列打开任务时，加载其明确的 result_id、替换旧预览，再定位到建议。 | 浏览器确认 A/B 确为不同结果，跳转后的结果 ID、标签、建议 ID 和可见正文均对应 B。 |
| R4 | 同种操作、参数和图像版本的活动请求才复用；异类请求返回 409。首次展开发布图像的短暂窗口仍保持幂等；DPI 与入队同事务提交。全文或选页处理原子提交。 | 单页 HTTP 409、强制重跑、版本变化、DPI 竞争；两页批量冲突零新增，旧失败状态完整回滚，成功重试与重复请求回执一致。 |
| R5 | 先筛出区域依赖已结束的页面，再应用 50 页批次上限。 | 前 50 页暂停时，第 51 页仍能完成，包含跨项目情况；52 页验证批次上限和失败页不阻塞后续。 |
| R6 | 搜索解码后的当前正文、单元格、标题及保留原文，统一 Unicode casefold。编辑替换掉的旧表格文字不再产生伪命中。 | 引号、Windows 路径、换行、ÉLODIE、ß→ss、摘要位置、采用结果和分页；一次 SQLite 读取快照逐行扫描。 |
| R7 | 结构面板、全文队列和决定接口共用当前候选规则，核对图像版本/哈希、工具运行、提供方最新候选、结果修订；已检查页数核对候选指纹和工具状态。 | 工具升级及重查、候选换代、图像/修订变化、并发读取快照；历史 accepted/kept/rejected 仍可查看，can_apply=false。 |
| R8 | 单条复核任务使用持久 proposal_id/task_id/issue_id 构造队列身份，结构备选继续显式分组。 | 两轮 6 条同目标建议与 2 个排队任务共 8 个唯一 ID；分页无重复，状态改变与文字目标重基后 ID 保持稳定。 |

新增正式测试：[文档请求/调度/搜索](../../tests/test_audit_document_regressions.py)、[候选有效性/队列身份](../../tests/test_audit_review_queue.py)、[纯文本与来源导出](../../tests/test_result_text.py)、[前端导航](../../frontend/src/documentNavigation.test.ts)、[复制失败与结果切换](../../frontend/src/resultClipboard.test.ts)。

## 验证结果

汇总回执：[validation-summary.json](validation-summary.json)。

- 最终完整 Python 回归：414 项运行，413 通过、1 跳过。跳过项为需要独立 GriTS 评分环境的交叉核验。[最终日志](backend-tests-final.log)。
- 前端：51 项通过；TypeScript/Vite 生产构建通过。Vite 仍提示主包超过 500 kB，属于已有体积提示。[测试](frontend-tests.log)、[构建](frontend-build.log)。
- 外部 Edge 浏览器：36 个不同场景通过，其中本轮修复 7 项，既有文档 10 项、结构复核 6 项、工具恢复 4 项、视觉审校 9 项。均无页面脚本错误，浏览器已关闭。[修复](ui-regressions-final/results.json)、[文档](ui-document/ui-results.json)、[结构](ui-structure/report.json)、[工具恢复](ui-tool-recovery/report.json)、[视觉审校](ui-multimodal/ui-report.json)。
- 无浏览器组件探针：6 组通过，直接执行当前 TSX 并控制 hooks/请求，检查页面同步、正常展开、晚响应、刷新竞争、保存期间跳页及真实 App 复核回调。这不是额外的真实浏览器场景。[探针](frontend/components.mjs)、[回执](frontend/component-results.json)。
- 新增 Python 用例共 31 项，包含文档 15 项、复核队列 12 项、文本 4 项；均纳入完整回归。定向记录保留于 [backend](backend/summary.json) 与 [review-queue](review-queue/README.md)，不与全量结果重复计数。

所有服务、数据库和导出使用本轮隔离目录。浏览器使用外部 Edge，带 `--disable-gpu --disable-gpu-compositing`，串行执行并在每组结束后关闭。UI 中 OCR、几何和审校建议为显式合成夹具；PDF 导入、原生提取、HTTP、SQLite、编辑保存和导出为真实执行。剪贴板写入在浏览器内捕获，未覆盖系统剪贴板。

首轮完整回归为 412 项运行、411 通过、1 跳过，见 [backend-tests.log](backend-tests.log)。随后独立审查发现全文处理半提交，补上原子批量入队和 2 条回归后重新执行完整套件。最初浏览器修复探针 6 项也已通过，但处理请求检查位于复核跳转之后，可能掩盖页状态错误；最终探针重新建立原始反例并增加真实保存后复制检查，共 7 项。旧过程回执保留，不覆盖为最终结果。

## 审查与改动整理

三个独立实现范围完成后交叉审查。发现的批量半提交已在同一 SQLite `BEGIN IMMEDIATE` 中修复，入队失败回滚全部插入和重试状态，成功提交后才唤醒 worker。纯文本接口沿用现有鉴权、no-store 和 nosniff，来源下载格式不变。R1 浏览器反例已与 R3 状态变更隔离，避免测试互相掩盖。

README、结构与视觉审校使用说明已更新当前修复状态。原审计报告和原提交说明只增加后续报告链接，历史失败记录保持原样。提交内容包括源码、测试、可重跑脚本、日志和必要截图/导出证据；运行数据库、会话令牌和合成输入留在本地并忽略，未删除原文件。修复源码与测试哈希见 [source-snapshot.json](source-snapshot.json)。

## 兼容性与限制

- 忙碌页面上的不同请求现在明确返回 409，需要完成或取消原请求后再提交；整篇处理不会出现部分成功但无回执。
- 文档搜索按内容线性扫描，逐行解码并只保留当前分页结果；本轮没有引入全文索引或宣称大文档性能提升。
- 队列展示 ID 生成规则改变，升级后展示 ID 会变化一次；底层任务、建议、修订及来源标识保持不变。
- 本轮程序修复不消除小模型误改文字、无局部定位时的误判或已有定位覆盖率不足；仍需人工复核建议。

## 复现

在仓库根目录使用具备服务依赖的 Python，将 `src` 和 `tests` 加入 `sys.path` 后执行 `unittest.defaultTestLoader.discover('tests')`；前端运行 `npm test` 和 `npm run build`。本轮使用便携包的 `runtimes/service/python.exe -B -X utf8`。

新修复浏览器入口：

```powershell
& 'D:/OCR-multimodal-workbench-20260917/bundle/runtimes/service/python.exe' -B -X utf8 scripts/audit_bugfix_ui.py --bundle 'D:/OCR-multimodal-workbench-20260917/bundle' --output audit/runs/bugfix-ui-new
```

输出目录必须尚不存在。脚本复用文档工作流夹具并显式建立不同的预览与采用结果；依赖已有 PDF 测试夹具、前端构建及 Edge，不调用 OCR/GPU 模型。`scripts/audit_document_ui.mjs` 的页面按钮定位已限定在 `.document-pages`，避免与新增复核队列按钮重名。
