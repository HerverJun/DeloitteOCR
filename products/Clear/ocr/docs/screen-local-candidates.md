# 屏拍局部文字候选契约

本能力是显式请求的 GLM `local_text` 候选队列，供平台专用工具和疑点复核使用。它不替换原值、不选定结果、不签署人工复核，也不生成正式表格结果。默认屏拍策略仍为 `local_reread=false`。CPU 契约和真实模型收益分别验收；接口存在或任务成功不等于文字正确。

## 提交和恢复身份

接口前缀为 `/api/projects/{project_id}/screen-batches/{batch_id}/local-candidates`。

`POST` 前缀提交以下完整对象；`POST /lookup` 使用相同对象只查询。未知 lookup 返回 `null`，不会入队。

```json
{
  "request_id": "local-submit-001",
  "budget_seconds": 120,
  "target": {
    "input_version_id": "current-version-id",
    "result_id": "current-result-id",
    "result_revision": 0,
    "issue_ids": ["issue-1"]
  }
}
```

一次只请求一张图的 1–12 个当前疑点，预算为整数 10–900 秒。只接受诊断中经原图像素分区及独立行列锚点验证的完整单元格；不接受自由坐标、缩放参数、模型提示或参考答案。提交在短事务中冻结源 SHA、结果修订、编辑内容、选定结果、人工复核状态和格位证明，创建原生 `screen_local_text` 任务后立即返回；裁剪和模型等待由现有后台队列承担。

提交请求编号与完整 payload 永久绑定。重复同请求只读回既有状态，不追加模型调用；同编号不同参数拒绝。模型不可用的 blocked 回执也持久化，恢复模型后须明确使用新请求编号。同根批次、源 SHA、格位、模型身份和裁剪策略已尝试的像素不重复识别：同一输入版本和批次可返回 `reused`；他图或他批次的相同像素返回 blocked、空 task_ids，不扩展本次读取范围。同时选择已尝试和未尝试位置则 `blocked`，不会偷偷追加剩余位置。

所有回执含 `version=screen-local-candidates-v1`、提交 `request_id`、`batch_id`、原冻结 `target`、`task_ids`、`owned_task_ids`、请求预算和初次预留秒数。`status` 描述提交决策（submitted/reused/blocked），`execution_status` 描述实时执行（pending/succeeded/partial/failed 或无任务的 blocked），二者不可混用。

`GET /{request_id}?offset=0&limit=12` 读回 counts、分页 items 和 next_offset。每项含原任务自己的 `task_id/batch_id/root_batch_id/request_id/input_version_id/parent_result_id/pixel_key`、status/phase/error、冻结 binding、current_binding、candidate_state、最多 512 字文字预览和完整产物清单。`text_preview_only=true` 表示须读取完整产物。`reused` 项保留原任务的实际绑定；不得把它伪造为本次 target 的新识别结果。

## 队列控制

`POST /{submission_request_id}/control` 和 `POST /{submission_request_id}/control-lookup` 使用相同操作对象：

```json
{
  "action": "retry_failed",
  "request_id": "local-control-operation-001"
}
```

动作支持 pause/resume/cancel/retry_failed。`request_id` 是本次控制操作身份，须与提交身份不同；响应顶层仍保留候选提交身份，并附 `control` 回执，包含操作身份、submission_request_id、动作、affected_task_ids、`status=applied`、`committed=true`。SQL 状态变化和控制回执在同一事务提交；丢响应后先 control-lookup。未知 lookup 返回 null，已提交操作再次调用不重新改变状态，第二次失败也不会被同操作再次重试。

控制仅作用于 owned_task_ids，复用别的请求的任务不会被本请求取消。pause 暂停尚未开始的项，运行项继续完成；cancel 可取消当前运行项。retry_failed 只重试 failed，不恢复 cancelled。resume 和 retry 不重置已用时间或单任务原额度，取消/超时后的原始产物继续保留。

## 预算、过期和产物

主批次未结束时拒绝局部候选抢占其预算。每格预留最多 180 秒，并受请求总预算、原图来源额度、当前及根批次总额度、所有同源/兄弟任务在途预留和原墙钟截止时间共同限制。暂停、中断保留剩余预留；恢复和重试 claim 原子累积此前执行段。局部成本记入 schema 19 的 `screen_local_requests`，不与普通 page result 或精度子批次回执重复计费。

原图格位按 3 倍 Lanczos 放大并加 32 像素白边；保存真实裁剪 SHA、包含整数框、尺寸及原图到裁剪 affine。候选发布前复核实际 crop、prepared/input.png 文件 SHA、prepared 与 crop 的 RGB 像素一致性以及原始 raw.json 与返回解码证据一致性。开始前身份变化则失败；模型慢返回期间或返回后源、结果、选定值、人工复核发生变化，候选为 stale 历史建议。无论正常、取消或超时，已生成 raw/result/error/crop 等产物保留可读；截断、page 模式或 crop 回执不匹配不会发布候选。

`GET /{request_id}/tasks/{task_id}/files/{file_id}` 只允许 raw.json、result.json、error.json、input.png、result.txt、result.md、crop.png、crop-binding.json。查询须属于当前项目、已登记请求及任务，并复核清单 SHA 和当前任务输出目录；不接受任意磁盘路径。所有项目产物仅保存 F 盘。

提交、resume、retry_failed 使用现有识别许可前置检查；只读 lookup/status/files 和停止动作按现有认证路由访问。平台仍须按平台注册工具和项目资料权限调用，不能把 native 接口可访问误判为 Agent 工具已获授权。

局部候选应使用独立 job lifecycle：以局部任务 counts 和实际产物结束等待，不把它们作为普通 OCR results 或主批次完成项；不得据此改变 XLSX/ZIP 的当前选定版本及待复核数。

schema 18 升至 19 继续执行事务迁移、备份、外键及完整性检查；失败回滚。旧程序拒绝较新 schema，不能把 schema 19 数据库当旧工作区继续写。最终候选包和冷恢复验收须使用实际包含局部 side table 的源码和数据。
