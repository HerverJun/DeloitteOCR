# E44 同机启动清理边界回执（2026-09-24）

范围：仅本机临时合成 workspace 与源码 `create_app` 入口；未读取 P0 封存题、未调用真实主控、未接触 candidate08 正式稳定性目录。C 盘开始时可用约 6.57 GB，E 盘约 16.39 GB；本项无需复制 29 GB 候选包。

新增 `tests/test_agent_artifact_boundaries.py::ArtifactBoundaryTests.test_restart_legacy_export_cleanup_preserves_agent_artifact_and_staging_policy`。真实应用构造入口运行两次：第一次清理旧 `exports/export-old`，同时保留已发布且哈希可验证的 Agent 成品，以及活跃 run 的 `agent-artifacts/*.staging-*`。将 run 终止后第二次启动，暂存出现在孤儿清单，但没有被自动删除；显式隔离后可在隔离目录读回原内容，Agent 成品仍可验证。应用以 `start_queue=False, agent_enabled=False` 构造，故此证据不代表完整 Agent runtime 重启或目标包启动。

验证命令：`build/ocr-agent-20260921-langgraph/langgraph-probe/运行时 中文/service/python.exe -B scripts/agent_eval/run_tests.py test_agent_artifact_boundaries test_agent_artifacts`。结果：20 tests，OK，1 skipped；跳过的是当前 Windows 进程创建真实文件 symlink 报 WinError 1314 的既有用例。新增 E44 用例为 `ok`，相邻长中文路径、junction、路径穿越、磁盘满、产物租约及跨项目 API 回归为 `ok`。

结论：补齐 E44 的源码启动清理与 Agent 成品/暂存共存场景，可作为同机工程证据；E44 仍不能整体标记 pass，旧可执行包重启、完整 runtime 恢复和目标机资格未由本项证明。候选08正式混合轨的最终回执及真实主控 0/72 等独立缺口保持原状态，不改写历史映射或 release-status。
