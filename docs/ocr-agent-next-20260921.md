# OCR Agent 当前执行入口：LangGraph 计划 v2

更新：2026-09-24。本文件最初为 2026-09-21 的 LangGraph 执行入口；实际进展以包外审计 [release-status.json](../audit/ocr-agent-20260921-langgraph/release-status.json) 为准，旧回执不改写。

权威入口：[主计划 v2](ocr-agent-development-plan-20260920.md)、[任务 JSON v2](ocr-agent-development-tasks-20260920.json)、[ADR-008](ocr-agent-langgraph-adr-20260921.md)、[分阶段验收](ocr-agent-acceptance-plan-20260920.md)。历史 audit/ocr-agent-20260920-p0/NEXT.md 记录当时 v1 的下一步，框架相关方向已由本文替代；历史回执不改写。

## 当前优先级：本机演示

本机演示已拉起：冻结 `candidate-09`、隔离数据工作区和合成项目可在 `http://127.0.0.1:8765/` 查看（需要本地启动令牌）。[现场回执与截图](../audit/ocr-agent-20260924-demo-preview-01/SUMMARY.md)记录进程、页面和短检查；`demo_ready=true` 限于本机展示。助手面板可打开，但提示先配置主控连接；用户可在演示中指出需要调整的体验。

`production_qualified` 仍为 false。真实独立主控封存 24×3 次、干净 Windows、物理断网、跨用户 DPAPI、旧版完整回退、当前候选约 29 GB 全量搬迁、生产性能与长期稳定指标留待生产资格阶段，缺项保留 `not_tested/not_qualified`，不作为本机演示阻塞项。用户已取消新的四小时混合长轨，不再安排；旧 formal 轨原始 `failed` 和超目标性能回执仍保留，不改判通过。详见[验收计划](ocr-agent-acceptance-plan-20260920.md)与[阶段发布结果](ocr-agent-release-results.md)。

## 继续顺序

以下为 2026-09-21 的原始实施顺序，现已执行进展以最新 audit 为准；本轮先完成上面的本机演示。

1. 核对当前分支、工作区及 P0 task-state 回执，不重建 P0、不覆盖其他改动。契约继续使用 src/ocr_workbench/agent/contracts.py，夹具引用原 fixtures-final；不打开封存任务做框架调试。
2. **B00 优先**：隔离运行时副本验证锁定 LangGraph/saver 的 Windows 离线依赖、两节点工具循环、interrupt 重入、操作重放、任务先完成后挂起、重复/中断 resume、双库对账和 tracing 关闭。只使用模拟模型与微型合成任务，无需真实凭据。
3. B00 通过后做 B01 业务库/框架库边界与备份、B04 模型协议→图规范消息适配。B02 的共享业务提取可以不等 B00 完成。B03 保留 P0 严格验证并实现真实资源归属/授权。
4. B05 做独立主控探针；C01—C05 用 StateGraph 打通查询/读页/证据与会话。已有授权配置可用时，先跑 3—5 条开发题真实烟测；缺少配置继续模拟，不借用视觉连接产生费用。
5. D01—D06 实现业务幂等、提交/等待分离、Command 恢复、导出及取消。最小真实业务链路优先 get_workspace_context/read_page_result/process_pages/export_results/ask_user，随后补齐 12 工具。
6. 按 E/F/G 继续审校、完整评测和冻结包。真实模型与目标机器未测项单列，框架模拟通过不算产品资格通过。

## 不变约束

图负责下一节点和消息；业务 DB 负责实际操作效果、授权、任务和产物。双库无原子提交保证。interrupt 恢复会重跑节点，执行前重新核验业务记录、版本与 generation；不自动重发结果未知的模型 POST。

正常活跃 run 的 waiting_jobs 自动恢复；waiting_user 等对应回复；程序重启后的 interrupted 等显式继续。停止助手与取消本轮自建任务保持两种操作，复用任务不误停。

框架依赖不得未经隔离验证直接写入发布运行时。实验失败写证据并继续独立任务，不悄悄恢复 v1 自研主方案。运行状态与证据写入新的 audit run 目录；P0 冻结 audit 和计划任务定义不充当新运行回执。
