# 外部视觉审校实现与验证 · 2026-09-18

源码版本 **0.12.0rc1**，数据库 **schema 12**。已实现一个外部连接的配置、图像测试、OpenAI Chat Completions / Anthropic Messages 适配，以及独立审校队列。用户入口为「视觉审校 → 配置外部 API」。使用步骤见[配置说明](external-visual-review.md)。

本轮使用 **127.0.0.1 本地模拟供应商**，没有使用用户真实 API Key，也未向真实供应商发送文档。测试验证工程行为，不能证明真实模型的纠错质量。

## 验证结果

| 检查 | 结果与范围 |
| --- | --- |
| 后端完整测试 | 441 项：440 通过，1 跳过；耗时 169.631 秒 |
| 前端 Vitest | 11 个文件，51 项通过 |
| TypeScript / Vite 生产构建 | 通过；保留已有的大于 500 kB chunk 提示 |
| 外部 Edge 浏览器验收 | 两种协议的配置至人工采用流程通过，无浏览器运行错误；浏览器与服务均正常关闭 |
| 独立服务运行时 | 离线安装 3 个已校验 wheel；HTTP 依赖版本匹配锁；禁止外部网络时启动成功并读取 schema 12 / 脱敏配置 |
| 凭据检查 | 真正 Windows DPAPI 加解密；API、数据库审校快照、任务文件、报告及浏览器持久存储不含测试 Key；独立暂存服务包扫描无测试 Key |

跳过项为既有 `test_structure_evaluation` 的独立 GriTS 评分检查，该服务运行时没有可选 `fitz`；不涉及新增 API 协议测试。测试日志中坏图片、数据库锁及卸载异常是既有故障注入用例，最终测试结果为 `OK (skipped=1)`。

完整回执：[receipt.json](../audit/external-review-20260918/receipt.json)、[后端日志](../audit/external-review-20260918/backend-tests.log)、[浏览器回执](../audit/external-review-20260918/ui-receipt.json)、[离线运行时回执](../audit/external-review-20260918/runtime-receipt.json)、[依赖回执](../audit/external-review-20260918/dependency-receipt.json)。

## 重点场景

- 两种协议的鉴权头、实际 Base64 图像、接口路径；Anthropic 分页和模型去重；手工填写模型 ID。
- URL 根地址、已有 `/v1`、自定义前缀及完整接口地址；拒绝 URL 中携带账户 / 查询参数 / 锚点。
- 随机测试图只在识别及结果格式均正确后保存；失败保持原连接；Key 不回显、同址留空复用、地址或协议改变时必须重新填写、清除连接。
- OpenAI 仅在测试中明确遇到参数不支持时协商 `max_tokens`，其他错误不会触发重发；模型可见性不被视为视觉能力。
- HTTP 401 / 403 / 404 / 405 / 429 / 500、重定向、超时、非 JSON、截断、工具调用、拒绝、缺失 / 未知 / 重复目标拒绝；保留人工采用门槛。
- 有定位目标的裁剪坐标、像素和传输哈希与原图一致；无框目标只发送页面上下文并标注无可靠裁剪，避免绑定邻格。
- 本地 GPU 锁被占用时外部队列仍完成任务；本地、融合、外部队列分别领取、恢复和执行批量操作，不能取消另一个工作线程的任务。
- 重复请求幂等；重启后的任务等待恢复；取消关闭 HTTP 等待、停止剩余批次且不产生建议；项目删除受正在运行的外部任务保护。
- 连接变更后尚未发送的任务失败且无网络请求，重试要求重新提交；两批之间修改连接停止后续发送；已经发送的单批按旧配置版本完成。
- 内容过期不能发布建议；仅校对模式可审校已有采用结果；采用后原始 OCR 不变，支持撤销以及 JSON / Markdown / XLSX 审校清单。
- 启动、读取配置、刷新页面不访问供应商。外部队列异常有状态和恢复入口；本地功能仍可离线执行。

浏览器证据：[配置弹窗](../audit/external-review-20260918/01-configure.png)、[原图核对与人工采用](../audit/external-review-20260918/02-proposal.png)。验收过程中修复了忙碌时按钮失焦导致弹窗被隐藏出辅助技术树的问题，随后完整流程通过。

## 复现

服务依赖准备见配置说明。安装依赖后的 Python 运行时执行：

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD\tests"
python -B -X utf8 -m unittest discover -s tests
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
python -B -X utf8 scripts/audit_external_review_ui.py --output build/external-api-review/new-ui-run
```

嵌入式 Python 的 `_pth` 若忽略 `PYTHONPATH`，应以 `-c` 将 `src`、`tests` 显式加入 `sys.path`；交付包使用自身 `app` 路径。UI 脚本要求本机 Edge 和前端开发依赖，输出目录须为新目录。模拟服务及 DPAPI 凭据使用独立验收目录，不写个人连接。

## 交付边界

已更新源码、前端、数据库迁移、测试、依赖锁、许可、离线构建脚本和使用说明。独立运行时副本的安装与启动已验证，未执行新完整包的全量模型拷贝或 ZIP 发布。原 `E:\DeloitteOCR-0.11.0rc1-Intranet-20260918.zip` 保持原状，不包含这次新增功能。

**真实服务尚未验证**。使用用户配置的服务完成一次内置测试图，再对一处真实文档局部审校并人工核对，才可记录该服务的实际验收结果；本地模拟测试不替代此步骤。
