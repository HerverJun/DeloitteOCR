# E68 checkpoint 领先恢复投影的隔离故障窗（2026-09-24）

范围：临时业务 SQLite + 官方 LangGraph SQLite saver，合成 native 页任务，子进程 `os._exit(93)`；没有真实模型请求、GPU、Edge、正式轨、P0 封存题或目标机验收。

代码边界：常规 `execute_next`/`await_jobs` 的 `finish_call` 在业务事务内一并写结果与 `tool_finished`，节点返回后图才 checkpoint；`finalize` 的完成状态、最终答复和 `run_state` 同样先于最终图 checkpoint。因此这些路径没有“完成 checkpoint 已提交但对应工具/完成事件未提交”的独立窗口。真实可分离窗口是 `AgentRuntime._resume_interrupted`：官方 saver `aupdate_state` 已持久写入新 generation，随后才由 `AgentStore.transition(..., queued, ...:resume)` 提交业务状态和 UI 事件；两库没有跨库原子性。

新具名测试 `test_agent_checkpoint_ahead.CheckpointAheadTests.test_crash_after_saver_update_before_resume_event_reprojects_once` 在后一行入口强退子进程。重开时 checkpoint 为当前 generation、无 interrupt、`next=await_jobs`，业务状态仍为 `interrupted` 且没有 `:resume` 事件；再次显式恢复后状态到 `waiting_jobs`，`queued` 恢复事件恰好一次，认证的会话快照及分页事件 API 均可见进度。模型请求仍 1、document stage 仍 1，旧副作用不重复；再调用恢复被状态检查拒绝。该测试证明此窄窗可核对补齐，不宣称任意图节点已前进而任意 UI 事件丢失均可恢复。

原始局部失败保留：第一次三项具名运行，新测试在创建 HTTP app 时因测试 bundle 缺 `config/engines.json` 报 `FileNotFoundError`，另两个相邻测试通过；此时未进入 HTTP/E68 投影断言。补临时配置后，同三项在 6.508 秒运行，3 tests OK。其余真实崩溃窗、并发及目标机资格未测；E68/F02 不整体判 pass。
