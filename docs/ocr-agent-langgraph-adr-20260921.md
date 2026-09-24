# ADR-008：以 LangGraph 承担 OCR Agent 执行编排

日期：2026-09-21；状态：用户已同意调整开发计划；框架尚未安装或接入。

对应[主计划 v2](ocr-agent-development-plan-20260920.md)、[35 项任务](ocr-agent-development-tasks-20260920.json)、[72 项验收](ocr-agent-acceptance-plan-20260920.md)。本 ADR 替代 v1 的自研执行调度、checkpoint 和重复消息/摘要存储设计；P0 ADR-001—007 中的工具契约、业务权限、版本、取消和数据保全要求继续有效。P0 冻结资料不修改。

## 1. 选择与责任边界

采用 LangGraph StateGraph，以 Python 库方式嵌入现有 FastAPI 服务。单主控在模型与工具之间动态循环；专用节点验证并执行工作台工具，不引入通用 shell、文件系统或任意网络能力。无需 DSH、DeepAgents、LangGraph Server 或 LangSmith 云服务。

| 层 | 唯一责任 | 不承担的责任 |
| --- | --- | --- |
| LangGraph | 图节点/条件边、状态 reducer、执行 checkpoint、interrupt/resume、图流事件 | 不取代 OCR 队列、GPU 锁、业务授权、版本校验与副作用去重 |
| 薄运行适配器 | 应用生命周期、thread 执行互斥、租约/generation、budget、invoke/resume 路由 | 不再实现第二套模型/工具 step 调度循环 |
| 业务 SQLite | 用户目标/决策、操作记账、任务关联、产物、稳定 UI 事件 | 不保存第二份能独立驱动恢复的图消息和摘要历史 |
| 现有 OCR 服务 | 原生提取、推理、结构与财务检查、视觉审校、修订、导出 | 不知道模型如何规划，不依赖聊天窗口在线 |

保留现有 12 个 Pydantic 工具 schema，并在执行前运行原有整批校验。JSON Schema 不表达的跨字段校验不能因改用框架工具装饰器而丢失。首版不以通用 ToolNode 默认并发执行写操作；可借用其接口模式，不把并发策略交给默认值。

## 2. 图状态与节点

state 保存：messages、summary/reference manifest、当前 run/generation、pending calls、current call cursor、operation/job refs、wait kind/decision ID、预算引用与图/状态版本。连接、队列、密钥由服务端运行上下文提供，不进 checkpoint。

模型节点保持现有 httpx 协议适配，返回可序列化规范消息；没有必要为 StateGraph 强制实现全套 LangChain 模型类。后续只有具体适配收益经过验证才增加供应商集成包。

节点分工：

1. model：按当前状态和授权工具请求主控，完整响应验证后返回。
2. validate_batch：复用 P0 全批验证，决定各调用的执行分类。
3. execute_next：执行短工具或幂等提交长任务，保存业务引用。
4. await_jobs：读取任务终态；未完成则 interrupt；恢复后再次读取真实 DB。
5. await_user：发布/读取具体 decision，interrupt 等对应回复；不包含实际提交或采用动作。
6. collect_results：按原 call 顺序形成完整工具结果，保持多调用配对。
7. finalize：检查请求覆盖和产物真实性，再结束当前目标。

框架决定节点调度，模型决定业务工具选择；图允许直接回答、连续检索、局部重试、跨多个工具回合，不能只验证一条硬编码顺序。

## 3. Checkpoint 与业务提交之间的缝隙

初始选择官方 async SQLite saver，独立 agent-checkpoints.sqlite3。每个项目会话由服务端映射稳定 thread ID，同 thread 串行 invoke/resume。UI/run 状态是投影，恢复权威是图 checkpoint 加业务操作核对。

副作用提交和 checkpoint 保存不是同一事务。execute_next 在业务库里原子建立 operation 与 task/job link，重放时复用已存在的结果。即使启用同步 checkpoint，也必须测试提交后而 checkpoint 未保存的故障窗口。

消息使用稳定 message ID 和对应 reducer；事件/预算/恢复也有稳定键。中断节点从开头重新执行，不能在 interrupt 前直接调用 OCR、扣费或应用修订。模型 POST 单独保存发送尝试和规范响应，未确定回包的尝试重放时暂停，不自动付费重发。

图 checkpoint 不持有权威授权：每次实际执行重新核验项目、版本、已批准 decision、配置和 generation。前端或模型不能提交任意 Command(goto/update/resume)，所有 resume 经后端映射到当前合法 interrupt。

任务唤醒采用持久 resume intent，状态至少区分 pending/claimed/applied，并绑定 thread、checkpoint/interrupt、run generation。不能先把唤醒消费后才尝试恢复而永久丢失；崩溃后根据图当前挂起状态和业务结果核对是否补发。重复通知不得额外执行模型回合或业务副作用。

checkpoint 缺失/损坏时停止自动续跑，显示可核对任务和产物；不得从 UI 自然语言猜状态并重新提交。可在确认已有业务效果后建立新会话继续目标。保留明确错误与恢复路径，不修改历史 checkpoint 掩盖损坏。

## 4. 依赖、离线与版本策略

前次元数据查询候选为 LangGraph 1.2.11、checkpoint-sqlite 3.1.1；它们仅是 B00 的候选，不是已兼容的发布锁。LangGraph 依赖 langchain-core、checkpoint、prebuilt、sdk 等，SQLite 包还有 aiosqlite/sqlite-vec 等传递项；离线交付必须验证完整依赖链，不能声称只加一个纯 Python 文件。

B00 在隔离目录下载并锁定候选 wheel/hash/license，使用实际 Windows Python 3.12 服务运行时副本，检查导入、冷启动、运行、中文路径、退出和全断网。元数据与实际依赖以选定发行版重查为准；源码快照 main 的行为不能直接当发布 wheel 已通过。

官方 async SQLite saver 提醒生产写性能限制。单机低并发适用性由 B00/F06 压测决定；失败不自动引入数据库服务器。最多两套有明确依据的官方兼容组合，仍失败则记录阻碍和替代 ADR，继续工具/界面等独立工作，不偷偷退回自研框架。

禁用 tracing/遥测配置，不继承开发机 LangSmith tracing 环境；用网络观测确认启动/本地测试不出站。LangSmith 是可选服务，不能仅因为装了框架就给项目正文增加外发接收方。

checkpoint 序列化只允许应用声明的可序列化类型，不启用不可信 pickle 回退；主控 Key、服务对象与原图字节不入 state。框架升级前核对 graph/state/serializer 版本，测试旧 thread/interrupt 恢复；不兼容时明确阻止自动续跑。

## 5. 已有成果与任务变更

P0 A01—A03 已完成：12 个工具契约、策略、基线、测试和 60 条夹具任务沿用。封存集不因框架变更重新生成或读取调试；B00 使用独立微型合成状态与模拟模型。

新增 B00 框架验证；B01 改为业务元数据与 checkpoint 分治；C01 改为 StateGraph 实现；C05 改为官方 saver/上下文接入；D02/D05 改为 interrupt 唤醒和重放核对；C03 映射框架事件为应用 SSE；F/G 增加图恢复、版本升级、checkpoint 清理与包依赖检查。

agent_messages/agent_summaries 两张待建表取消，增加 agent_model_requests 请求恢复日志。它只存请求级恢复所需的数据，不组成第二套可编辑会话历史。agent_calls/operations/jobs/decisions/artifacts 仍有独立业务审计作用。

## 6. 证据

已读固定源码提交 `ed384f3a124660db6dccd6c53eaad48e1457e0b5`；来源索引在 [本地快照](../research/ocr-agent-langgraph-20260921/source-index.json)。

- [官方 README](https://github.com/langchain-ai/langgraph/blob/ed384f3a124660db6dccd6c53eaad48e1457e0b5/README.md)：独立使用、持久执行和中断能力。
- [interrupt 源码说明](https://github.com/langchain-ai/langgraph/blob/ed384f3a124660db6dccd6c53eaad48e1457e0b5/libs/langgraph/langgraph/types.py)：恢复从节点开头重执行、依赖 checkpointer。
- [ToolNode 源码](https://github.com/langchain-ai/langgraph/blob/ed384f3a124660db6dccd6c53eaad48e1457e0b5/libs/prebuilt/langgraph/prebuilt/tool_node.py)：工具执行与并发机制。
- [AsyncSqliteSaver](https://github.com/langchain-ai/langgraph/blob/ed384f3a124660db6dccd6c53eaad48e1457e0b5/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py)：接口与性能限制说明。

本 ADR 仅完成选型与开发设计，B00 的安装、运行和兼容性检查尚未执行。
