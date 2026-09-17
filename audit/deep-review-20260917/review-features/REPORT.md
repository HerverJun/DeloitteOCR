# 新功能跨流程复盘与测试

日期：2026-09-17。只读检查产品源码；复现脚本和回执写在本目录。所有数据库、图片和人工生成模型响应均在临时目录中使用，已清理。未启动浏览器或 GPU 推理，也未访问实际工作区的数据。

## 已复现问题

### P2：多次审校同一目标会返回重复的文档复核队列 ID

- 源码：`src/ocr_workbench/document_review.py:30`。队列 ID 只包含 page/result/kind/target，未包含多模态 `proposal_id` 或 `task_id`。
- 触发：对同一已采用结果、同一修订执行两轮整页审校，尚未采纳任何一轮；或连续提交两次尚未执行的审校请求。前端允许使用不同请求编号提交，后端也会合法创建两条独立任务。
- 复现结果：两轮各三个文字目标，共六条独立建议在队列中只有三个唯一 ID；两个尚未执行的请求也有相同队列 ID，但 `task_id` 不同。
- 影响：公开队列接口无法使用 `id` 唯一标识真实任务。前端 `frontend/src/DocumentReviewQueue.tsx:37` 正好使用此值作为 React key，违反列表子项 key 唯一性要求，状态更新时存在错误复用条目的风险。本文没有运行浏览器，所以没有把具体 DOM 丢项/错误点击当作已复现事实。
- 边界：单次审校不会触发。底层 `proposal_id`/`task_id` 保持唯一，尚未发现错误内容因此被写入。应在需要独立显示的条目身份中纳入对应建议/任务 ID；若要按目标合并，则需要像结构建议那样显式形成一个分组。

### P2：PDF 表格工具升级后，文档复核队列仍显示已失效的建议

- 源码：`src/ocr_workbench/document_review.py:45`–`57` 未用当前工具 run_id 过滤候选和建议；`:67`–`72` 的 candidate_pages/checked_pages 也直接统计历史数据库行。
- 触发：当前 PDF 结果已有待复核的 pdfplumber 结构建议，表格工具配置更新，导致工具 key 变化。
- 复现结果：同一结果的结构面板接口返回 `table_tool.state=outdated`、零个候选、零个建议；文档复核队列却继续显示旧的结构差异，并将其 alternative 标记为 `can_apply=true`，同时报告 `candidate_pages=1`、`checked_pages=1`。
- 影响：队列指向一个结构面板已经过滤掉的条目，用户无法按队列内容完成该项；已检查/候选就绪的统计和当前工具状态不一致。额外实测重新执行当前页的结构检查后，旧结构待办仍然存在；检查动作只写入新的检查记录，没有淘汰该旧工具的 pending/deferred 行。
- 边界：采用接口有额外保护，实际采用被拒绝，错误为 `表格工具候选已过期，请重新提取并检查`，修订仍为 0。没有复现旧候选污染已采用内容。应复用 `structure_view` 的当前候选过滤规则，并明确历史已处理建议与当前待办的统计口径。
- 测试方式：人工构造原生 PDF 结果元数据和独立候选，在临时数据库中模拟工具身份变化；没有把假响应当真实模型质量证据。

## 通过的检查与实测范围

定向回归总计 78 条，全部通过、无跳过：

- `test_multimodal_store`、`test_structure_workflow`：43 条，5.283 秒。
- `test_multimodal_integration`、`test_geometry`、`test_pdf_table_tools`：35 条，4.979 秒，完整日志为 `targeted-integration.log`。

覆盖了请求和决定的幂等重放、并发旧修订冲突、撤销重做导致建议失效、同批建议位置重基、原始识别结果不可变、取消/失败/关闭时不发布过期建议、视觉模型缺失后的已提交请求恢复、仅校对模式、升级迁移备份和回滚、独立结构候选、几何证据过期、PDF 原生文字结构预览、公式样文本与前导零的导出读回、报告与内容修订的同快照一致性。

本次未进行真实 GPU 识别准确率/速度测试；合成模型响应只能验证工作流和持久化合约。系统默认 Python 缺 `pillow_heif`，因此使用完整服务运行时 `D:/OCR-multimodal-workbench-20260917/bundle/runtimes/service/python.exe`，通过 `sys.path` 指向本仓库的 `src` 和 `tests`，实际测试的是当前源码。

## 复现

```powershell
& 'D:/OCR-multimodal-workbench-20260917/bundle/runtimes/service/python.exe' -B -X utf8 'audit/deep-review-20260917/review-features/probe_review_features.py'
```

两项复现均使用断言校验，并写入 `review-features-receipt.json`。脚本再次运行会覆盖本目录的回执，不修改产品代码和用户工作区。
