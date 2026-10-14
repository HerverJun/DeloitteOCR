# P0 架构与契约冻结（2026-09-20）

范围：A01—A03。契约版本 `ocr-agent-v1`，策略版本 `ocr-agent-policy-v1`。
可执行实现仅包括严格数据验证、整批调用验证、结果配对和有界结果校验；没有注册业务工具、API、数据库迁移、模型调用或助手 UI。

## ADR-001 实施边界与已有成果

在当前脏工作树上创建 `codex/ocr-agent`，保留所有已有成果；实施前的 534 个源码、配置、测试、文档和夹具文件已复制到 `build/ocr-agent-20260920-p0/baseline-source/`，哈希在相应 audit 的 baseline.json。不从旧 HEAD 重建工作区。原计划及任务 JSON 保持设计初始状态，完成状态单列 task-state.json。

## ADR-002 严格数据契约

采用 Pydantic 2；现场打包运行时与现有 service lock 均为 2.13.5，新增显式直接依赖 pin，不改变现有传递锁。系统 Anaconda 版本不同，不用于发布判断。JSON Schema 为导出接口，Pydantic after validators 补充互斥来源、唯一页集、call/result 配对等跨字段约束，不能单凭 JSON Schema 宣称业务授权已实现。

12 个工具固定白名单；模型输入拒绝多余字段、bool/string/float 页码及任意 project_id、credential_ref、路径、操作 key、授权结果。资源 ID 的实际项目归属、授权和版本检查由 B03/B02 实现；P0 不提供假实现。现有 revision 从 0 开始，generation 和 page_number 从 1 开始。

`inspect_table.action` 显式区分 `read_checks` 与 `generate_candidates`：后者是有记账和任务关联的写动作。视觉模型 ID/引擎 ID 只引用已有能力，不能替换为网络地址。工具总输出按 UTF-8 编码计 16 KiB；大内容分页并返回引用。单次 100 页、整轮 1000 页、400 引擎任务的总预算不可用分批绕过。

## ADR-003 结果、范围与授权

查询默认采用结果；`run_result` 必须明确 result_id 且 B03 验证本轮关联。导出选择显式 result/revision/version 清单或 run manifest，不能混用；默认部分失败需用户决定。模型指定 allow 不是授权，必须核对用户请求的 scope grant。

保留 Store.complete 的 `INSERT OR IGNORE` 初次选择行为，重识别不能覆盖已有采用。结构/视觉建议沿用现有采用前置条件；不能为了处理新未采用结果绕过它。无可靠坐标只能引用页面，不伪造局部裁剪。修订采用仍由现有 UI 展示具体差异并校验 revision。

首次项目正文发送主控时绑定角色、地址、配置版本和范围，后续同范围复用；视觉图像单独绑定视觉角色。OCR 正文、模型摘要不产生授权。默认主控只有文字；不自动读取视觉连接的凭据，不启动探活。已有批准动作不重复打断用户。

## ADR-004 状态、协议与取消

Session 只表示项目会话；Run 表示目标；Call 表示协议；Operation 表示业务副作用。Call ID 不充当跨回合幂等 key。一次响应完整解析并验证全部工具之后才能执行，错误/拒绝/截断不会执行半个动作。顺序一一配对 result；供应商 opaque/reasoning/signature 块由后续适配器原样保留，不能作为成功依据。

长任务先原子写 operation/job link，再发事件并等待；不把 queued 当完成。generation/fencing 防止迟到结果继续写入。普通追加消息在步骤安全边界消费；停止助手保留已提交任务，停止并取消只取消 created 且可取消任务。启动仅本地核对，不自动发模型或恢复 OCR；恢复必须重验版本、配置、授权、预算。

## ADR-005 API、SSE 与持久化

20 条方法/路径声明见导出的 contracts.json；其中会话/消息/取消/恢复/回复已提供请求 schema，其余连接/产物的业务请求在 B05/D04 细化，必须保持共同重放/鉴权约束。GET 不含 Key。使用带 Bearer 的 fetch SSE；会话 seq 单调、snapshot through_seq、generation 校验，状态和事件同事务。慢客户端断开补读，401 停止重连。DB 游标作为真值，不能依赖订阅通知不丢失。

schema 当前仍为 12；13 仅为 B01 候选号，实施前重查。迁移用 SQLite backup API 和单事务，禁止仅复制活动主文件或直接降版本。新产物目录 agent-artifacts 与旧下载后删除的 exports 隔离；原子发布、租约、30 天保留和 pinned 保护在 D04/D06 实现。无网络等待跨越 SQLite、maintenance 或 GPU 锁。

## ADR-006 功能开关与默认预算

`config/agent-policy.json` 的 agent_enabled=false；P0 无加载此配置的生产入口。后续功能关闭必须阻止新 run，仍允许原 OCR；无连接时可见入口由 UI 后续实现，不联网。新增 connect=10s、read=60s（总计 180s）、未知模型保守上下文 16k、输出预留 2k、SSE heartbeat=15s、缓冲=100、事件分页=100/max500 均为开发默认值，F06 校准；调整需新策略版本。缺真实 usage 显示估算，缺价格显示未知。

## ADR-007 夹具与评分证据

FX01—FX08 全部合成，仅供工程与主控编排评测，不充抵真实扫描质量。每族有分开的 development/holdout 文档组；任务模板分组互斥，36/24 按 S01—S06 各 6/4 分配。生成器知晓的模板来源透明记录；封存输出不供提示调试，读取/调试通过 exposure 命令记账并作废该封存项，须建立新批次补充，禁止改名洗白。

评分器规格固定真实 server state/产物/版本/引用的确定断言，并单列解释 rubric；允许多条合理工具路径。高影响错误一票否决，连接失败和人工干预保留分母。整体 ≥65/72、各场景 ≥10/12、首次有效工具 ≥98%、引用 100%、高影响错误 0；P0 不运行模型、不生成成功率。
