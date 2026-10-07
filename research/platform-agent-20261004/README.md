# Clear 平台 Agent 框架调研

调研日期：2026-10-04，Asia/Hong_Kong。范围：官方文档、官方示例、固定提交源码、PyPI发行元数据，以及当前Clear平台/OCR/分类源码。只读分析；没有安装或运行新框架，没有请求真实主控，没有用GitHub活跃度替代功能验证。

交付：[平台Agent开发计划](../../docs/platform-automation/平台Agent开发计划-20261004.md)、[开发任务清单](../../docs/platform-automation/platform-agent-development-tasks-20261004.json)。全部获取来源、时间、文件路径和SHA256在 [source-index.json](source-index.json)，原文在 `snapshots/`。GitHub源码已固定提交；在线文档仅证明获取时的内容，不能自动视为某一锁定发行版的承诺。

## 1. 选型结论

推荐LangChain模型/工具组件配合显式LangGraph StateGraph。理由来自当前项目：已经有OCR LangGraph实现，平台工具多数是独立长任务，并且必须保留原生队列、GPU互斥、版本与请求身份。显式图能把模型决策与业务等待、恢复放在同一个框架内。

LangChain `create_agent` 本身也是LangGraph图，不是另一种与LangGraph对立的路线。它提供方便的通用循环，但项目还需要同对象写操作串行、异步任务终态收集、整批调用验证、停止与原生请求核对。已有代码中这些语义已经相当明确；没有必要为了换成一个更短的入口函数而重写所有行为。

本计划不要求把LangChain所有功能装进平台：首版生产依赖只选择用到的core/模型适配/graph/saver；`create_agent`用于理解和验证最小循环。如果实施验证表明用标准create_agent可以直接表达全部所需行为且明显更少维护，可在PA02记录一次具体差异并替换图工厂，但禁止同时保留两套独立运行循环，也不重新展开长周期框架选型。

## 2. 参考实例与可迁移设计

| 官方资料 | 实际发现 | 对项目的影响 |
|---|---|---|
| [LangChain overview](https://docs.langchain.com/oss/python/langchain/overview) 与 [agents](https://docs.langchain.com/oss/python/langchain/agents) | 模型、工具和运行机制组成Agent；create_agent是可配置入口 | 输入自然语言目标，工具结果进入后续模型上下文；不是只让AI写建议 |
| [LangGraph workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents) | 官方明确区分预定代码路径的workflow和动态决定过程/工具的agent | 平台现有BatchService是可复用业务工具，不等于已实现Agent |
| [LangChain tools](https://docs.langchain.com/oss/python/langchain/tools) | 函数说明和类型生成schema；ToolRuntime等运行时参数对模型隐藏 | 目录、输入输出、范围和handler一起注册；密钥、workspace等由服务端注入 |
| [LangChain SQL agent示例](https://docs.langchain.com/oss/python/langchain/sql-agent) | 先探查表和schema、执行、基于真实错误纠正 | 迁移“观察→行动→再观察”方式到ClearData；继续使用已有确定性清洗与比对算子 |
| [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | resume会重执行interrupt所在节点开头代码 | 任务提交必须幂等；持久保存operation ID后核对，不能以checkpoint代替业务去重 |
| [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers) | 每个super-step保存状态；有sync/async/exit持久化模式；SQLite适用于本地工作流 | 单机使用官方async SQLite saver；图历史与业务状态分工，版本和冷备需验证 |
| [LangChain自定义middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom) | 可以围绕模型与工具调用调整工具集、上下文和处理逻辑 | 借鉴每轮工具可见性与上下文裁剪，不假设所有middleware可直接挂到自定义StateGraph |
| [Deep Agents overview](https://docs.langchain.com/oss/python/deepagents/overview) | 内含计划、上下文管理、文件系统和子Agent等机制 | 计划和压缩值得参考；当前工具调度目标不需要其完整默认栈 |
| [PydanticAI function tools](https://ai.pydantic.dev/tools/) 与 [durable execution](https://ai.pydantic.dev/durable_execution/) | 类型化函数工具/依赖注入，持久执行另有集成方案 | 是合理替代框架；但本项目已有LangGraph，迁移成本目前大于已确认收益 |

旧 `langgraph/durable-execution` URL在本次实际获取时返回了标题为“Persistence”的页面，因此引用持久化细节以本次另取的checkpointers和interrupts页面为准。Pydantic网址返回了供机器阅读的文本；保存扩展名是html，但快照哈希对应实际获取内容。

## 3. 固定源码核对

| 源码 | 固定提交 | 核对到的行为 |
|---|---|---|
| [LangChain agent factory](https://github.com/langchain-ai/langchain/blob/57236d55d9ffc8634bda987861e6fce460aff268/libs/langchain_v1/langchain/agents/factory.py) | `57236d55d9ffc8634bda987861e6fce460aff268` | 导入StateGraph与ToolNode；create_agent接受模型、工具、middleware、checkpointer并返回编译图 |
| [LangGraph ToolNode](https://github.com/langchain-ai/langgraph/blob/9a0394d88b2211f299dcd69df92db3480c69ee61/libs/prebuilt/langgraph/prebuilt/tool_node.py) | `9a0394d88b2211f299dcd69df92db3480c69ee61` | 同步使用executor.map，异步使用asyncio.gather执行同批调用；GraphBubbleUp单独传播 |
| [Deep Agents graph](https://github.com/langchain-ai/deepagents/blob/f57c6f383b7018024ca5cde2dc565048ea83202a/libs/deepagents/deepagents/graph.py) | `f57c6f383b7018024ca5cde2dc565048ea83202a` | 默认组装文件系统/子Agent等middleware；execute需要实现SandboxBackendProtocol的后端 |

因此不能直接把默认并行ToolNode用于多个会修改同一ClearData会话的调用。项目采用显式dispatch：只读有限并行、写操作按资源串行，整批预校验后每次执行再次校验。GraphInterrupt必须继续交给框架，不能被“任何异常都转成工具错误”的包装器吞掉。

Deep Agents并非默认就能执行宿主机任意命令；本次源码说明其execute依赖sandbox backend。未选择它的原因是本项目不需要该套默认能力，而不是声称它不能注册业务工具。

## 4. 版本与许可观察

| 包 | 本次PyPI查询版本 | 当前OCR已有锁 |
|---|---|---|
| langchain | 1.4.3 | 没有完整langchain包作为旧Agent核心 |
| langchain-core | 1.6.6 | 1.6.3 |
| langgraph | 1.2.12 | 1.2.11 |
| langgraph-checkpoint-sqlite | 3.1.1 | 3.1.1 |
| langchain-openai | 1.6.7 | 原实现采用httpx协议适配 |
| langchain-anthropic | 1.7.5 | 原实现采用httpx协议适配 |

以上版本元数据要求的Python范围包含3.12，但这不证明它们与本项目全部依赖、Windows本机运行时和现有endpoint已兼容。PA02负责在F盘独立环境做最小验证并生成实际锁；本次没有安装或升级。GitHub仓库元数据对LangChain、LangGraph、Deep Agents、PydanticAI报告MIT；正式依赖层仍需按精确wheel及传递依赖记录随包许可。

文档包含新版本API示例，实际实施以锁定版本为准，尤其是事件流、interrupt参数、ToolRuntime注入和序列化。外部LangSmith追踪和云服务不列为本地首版依赖。

## 5. 项目对照中的关键事实

- OCR的 `agent/contracts.py` 有12项工具；`graph.py` 确实在模型、执行、结果之间动态循环。平台新Agent可复用模式，但不能直接使用其OCR特定Store和项目契约。
- 平台 `BatchService` 按 `data_compare/classification/ocr` 固定场景选择runner。现有自动化不足以兑现本次目标，但它背后的业务接口可以封装为工具。
- 平台当前OCR提交接口拒绝PDF，原生OCR有按页处理入口。PDF工具必须补适配与持久请求身份。
- ClearData仅接CSV/XLSX/Parquet等现有输入路径，OCR结果当前以原生结果/导出包归集；需要类型化表格/派生成果桥。
- ClearData资料集导入目前每次生成新的upload ID。Agent节点重放时不能直接重复调用这一HTTP路由，需要提取有幂等键的服务函数。
- 平台公开的取证schema只开放六项经典算法；源码catalog里有ocr/external_model条目不代表已发布。
- 分类预测可以用于派生处理子集，但不等于更新人工标签。当前禁止自动接受人工处置的规则不应被误用成“任何推理结果都必须人工确认后才能继续工具链”。
- 最新2026-10-03产品修订已经将审计流程降为高级功能。新Agent接入日常工具与成果主线，避免把旧复核工作流作为必经路径。

本次研究支持架构与开发范围选择，不提供未经实测的成功率、准确率、成本或性能承诺。
