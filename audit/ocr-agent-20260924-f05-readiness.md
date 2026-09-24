# F05 包外评测前置（2026-09-24）

本次只新增 `scripts/agent_eval/score_attempts.py` 和 `tests/test_agent_eval_score_attempts.py`，属于 09 冻结后的本机包外评测工具；没有进入 candidate-09、差量包或产品源码一致性声明。P0 冻结契约、开发/封存数据和历史回执均未修改。

离线组件按原 `scoring-contract.json` 检查开发任务断言、终态、解释分、介入、高影响状态、工具首次有效率、引用分母、usage 和写入追溯，并强制唯一排程和独立 workspace。缺排程、usage 或关键计数会拒绝评分。返回的 `qualification_status` 恒为 `not_attested`，因此门槛计算不能自动授予模型支持资格。

验证命令：`python -m pytest tests/test_agent_eval_score_attempts.py -q`；结果：6 passed。测试仅读取冻结开发集 `development/tasks.json` 和评分契约；测试观测值在内存构造，只证明评分边界，不属于模型运行、独立状态证据或合格回执。没有读取24条封存任务或真值，没有请求真实模型。

实际 F05 仍为 `not_tested`：真实请求 0，封存执行 0/72，已验证主控配置 0。要进行真实评测，还需独立授权的主控配置、逐次隔离的执行器、从 DB/事件/文件提取状态与引用的独立适配器，以及有身份记录的解释评分；上述环节不能由内存测试观测值替代。

后续包外新增 `scripts/agent_eval/observe_artifact.py`：只读核对独立工作区 SQLite 的 run/session/operation/call/artifact 绑定、`artifact_ready` 事件、发布 receipt 和真实文件的 SHA-256/格式，仅产出 `artifact.format` 与 `artifact.hash_verified`。缺证据或不一致即拒绝；不从任务预期值构造观测。合成落盘夹具与原评分器合计 12 项通过（`python -m pytest tests/test_agent_eval_observe_artifact.py tests/test_agent_eval_score_attempts.py -q`）。测试没有调用产品导出入口，其他断言的独立观测、逐次执行器和解释评分仍缺；这些工具未进入冻结 candidate-09，不能折算真实评测。

另有包外 `scripts/agent_eval/run_development_attempt.py`：仅接受开发集 task ID，在每次调用建立新工作区，实际创建项目、导入开发文档、创建 selection/session/run，并用固定模拟主控驱动真实 AgentRuntime 的一个 `get_workspace_context` 工具回合。两次 S01-01 开发探针的独立 `execution.json` 分别在 `%TEMP%/f05-execution-probe/S01-01-r1-46558c7bf2da567b` 与 `S01-01-r2-7a4fd65f300eac26`，均记录 completed、两条 received 模型请求和一条 tool_finished 事件；先前两次 setup 失败回执仍保留于同目录。针对性测试 13 通过、1 跳过（系统 Python 缺产品依赖）；真实路径由现有 service Python 的两次 CLI 探针覆盖。此开发探针使用工作树源码和文档运行时，不是冻结 candidate-09 的真实主控评测，也没有任务成功评分。完整 72 次执行、更多状态取证、解释评分及真实授权连接仍缺。

最后新增包外 `scripts/agent_eval/attempt_ledger.py`，只接受显式 24 项、六场景各四项的任务 roster，在每题三次的 72 个不同空目录写入不可覆盖的排程；单次原始失败回执只首写，供应商错误与缺失尝试仍占原 72 分母。开发任务子集测试 `python -m pytest tests/test_agent_eval_attempt_ledger.py tests/test_agent_eval_score_attempts.py tests/test_agent_eval_run_development_attempt.py -q` 为 12 通过、1 跳过（原执行探针依赖项缺失）。ledger 不读封存任务、不调用模型、不计算资格，结果恒 `not_attested`；它尚未接通真实主控、全部独立状态观测或完整 72 次执行。
