# E40 隔离长会话代表场景（2026-09-24）

合成临时工作区中，真实 `Artifacts.export` 发布 TXT 后记录工具事件；同源 `compact_messages`、`summary_sources`、`summary_message` 保留旧 `artifact_ref`。旧 run 完成，同会话新 run 与新应用 lifespan 可读取历史事件，并通过项目绑定的 HTTP API 下载原字节。另一项目 404；到期下载 400（保留期提示）；文件缺失/字节损坏 400 且记录分别落为 `missing`/`corrupt`；没有新增虚构产物或遗留下载租约。

边界：这是短时合成函数级裁剪/摘要与新 run/应用打开，未进行真实长时间模型对话或目标机验收。到期而尚未由维护清理时，DB `state` 仍是 `ready`；下载由 `expires` 拒绝，前端也按 `expires` 显示过期并禁用下载，最终 `expired` 状态须维护清理完成才落库。故不能把“过期 DB 立即标记 expired”视为已证明。E40 仅局部工程证据，F02 不整体判 pass。

具名测试：`test_agent_artifact_long_session.LongSessionArtifactTests.test_old_reference_download_after_compaction_summary_and_session_reopen` 与四个邻近发布、过期租约、缺失损坏、项目绑定 HTTP 回归，5 tests OK，3.069 秒。未启动 GPU、Edge、真实主控或正式数据测试。
