# 文档助手开发维护说明

架构依据：[ADR-008](ocr-agent-langgraph-adr-20260921.md)。开发阶段入口：[v2 执行计划](ocr-agent-next-20260921.md)。原 P0 契约、夹具与封存题保持冻结，开发回执写入 `audit/ocr-agent-20260921-langgraph`。

## 执行与持久化边界

LangGraph 负责节点调度、消息 reducer、挂起和下一执行位置。业务 DB 负责实际效果、资源授权、模型 POST 日志、预算、任务关联和 UI 事件。两库没有原子提交保证。业务提交成功但 checkpoint 落后时，写工具重入必须核对 operation 和调用关联，不能再次执行效果。

运行图包含 receive_inbox、prepare_context、model、validate_batch、execute_next、await_jobs、await_user、collect_results 和 finalize。prepare_context 在模型请求前将超阈值工具历史压缩并先写入正式 saver；必要时通过业务模型请求表持久记录语义摘要的预算和响应状态。摘要不能授予权限或宣布业务完成，最近用户约束、未完成调用与工具结果配对仍保留。消息使用稳定 ID 合并；整批参数验证失败时不执行其中任何调用。工具执行按原调用顺序配对；框架 `GraphInterrupt` 必须显式传播，不能被通用 Exception 包装为工具失败。

业务 schema 14 新增会话操作请求回执。模型请求发送前持久记账；received 可重放，rejected 重放确定错误，sent/unknown 禁止自动再发。未知 usage 保留预扣。参数修正最多两次新请求。

## 写效果和范围

`Operations.submit` 在同一短事务核对 generation、执行租约、fencing、项目写竞争、pending inbox、授权与输入版本，然后写业务效果和 job links。不能持有 SQLite 事务跨网络请求。已有手动入口与 Agent 尽量复用共享业务 helper，不增加任意 SQL、文件或网络工具。

选择快照包括页集、原件 hash、render 参数、图像版本和采用结果修订。初次懒渲染可以产生原图版本，后续变换不允许默默替换。inbox 的新 scope_revision 在安全边界切换，使旧 grants 不能授权后续新写入；已经提交的工作仍保留关联。

PDF 区域任务在实际创建事务扣量。重放已经存在的 child 不重扣；失败重试重新收费，暂停/中断恢复保留原记账。Agent 处理页不会附带未单独授权的自动模型结构补定位。视觉外发同时在入队、实际发送处核验独立视觉授权。

## 决策、取消和恢复

决策绑定 payload hash、generation、连接修订、scope 和有效期；部分导出还绑定结果版本和覆盖清单。普通追加消息只能替代澄清，不能当作预算、未知请求或部分导出的同意。

停止推进 generation，停止后续动作；取消自建任务不触及 reused。运行重启后显式继续，恢复 created 的 paused/interrupted 任务前检查项目、种类、版本和外发快照。复用的暂停任务返回 blocked 状态，不能无限等待并假装正在运行。

观察器续租、核对已关联任务并发送恢复通知，不调用模型进行进度轮询。已提交用户回复丢失唤醒时可以通过持久状态补偿。外层数据库故障保持观察循环，过期执行权必须 fencing。完整 claimed/applied resume-intent 崩溃矩阵仍是后续验收事项。

## 导出和 UI

新导出绑定实际结果 manifest，已登记调用重放使用原 manifest；范围缩小时新产物过滤历史范围外结果。文件先写 staging，原子发布目录，再写 ready；下载前核对清单、字节数和哈希。下载响应全程续租，断连释放。清理前先记录中间状态。

UI 状态来自业务事件和快照，SSE 依据 seq 续读；模型文字不决定完成状态。渲染采用普通文本，不执行模型 HTML。证据跳转重新检查所属项目、结果修订和图像版本。

## 可复现开发验证

当前隔离运行时为 `build/ocr-agent-20260921-langgraph/langgraph-probe/运行时 中文/service/python.exe`。这是开发副本，不是发布路径。它固定使用 LangGraph 1.2.11 和 checkpoint-sqlite 3.1.1；传递 wheel 清单与许可审查保存在本阶段 audit。

实验依赖锁已写入 `config/runtime-locks/agent.json` / `agent.txt`，41 个 wheel 绑定哈希；51 份完整许可证及来源见 `licenses/agent/manifest.json`。没有改写生产运行时。`scripts/agent_eval/freeze_framework.py` 可核对并重建这些依赖资料。

```powershell
& 'build/ocr-agent-20260921-langgraph/langgraph-probe/运行时 中文/service/python.exe' -B scripts/agent_eval/run_tests.py test_agent_graph test_agent_inbox_api test_agent_artifacts
```

runner 显式加载 src/tests，因为 embedded Python 不读取 PYTHONPATH。前端在 frontend 目录运行 `npm run build`。`record_incremental.py --help` 说明增量证据汇总入口，只汇总已有完整测试日志，不生成产品通过成绩。

浏览器测试使用受控本地后端和合成模型，优先外部 Chromium 加 `--disable-gpu --disable-gpu-compositing`，遵守全局 AGENTS 的截图稳定性规则。封存任务不用于开发调试。真实主控评测只有独立已授权配置存在时才执行，否则填写 not_tested。

## 升级与交付要求

改变图节点/状态语义时评估 graph/state/serializer 格式版本，旧 checkpoint 不兼容时保留原数据并明确拒绝自动恢复。不能通过 arbitrary thread/checkpoint/Command 的客户端接口绕过版本和业务核对。

候选包必须包含匹配源码、前端构建、依赖锁、迁移、配置、许可和文档；不能带开发 checkpoint、会话或凭据。默认禁用隐式 LangSmith/LangChain tracing，关闭 Agent 时不强制导入框架。打包后仍需独立验证双库恢复、原件/产物文件、中文路径迁移和离线依赖。

发布判断分别列工程、真实模型、内容质量和环境资格。当前仍是开发进行中，完整 72 项验收、四小时稳定和包内测试不能由已有单元测试数量代替。`audit_ui.py` 使用真实鉴权 HTTP、外部禁 GPU Edge、真实 PDF CPU 和本地视觉协议夹具；该原生 PDF 场景没有采用的结构表，因而正确拒绝 XLSX。另有真实本地 GLM OCR 20 页批次生成 XLSX，并通过历史界面实际下载与原工作簿哈希核对，见 `gpu-batch-02` 和 `gpu-ui-02`。`performance.py` 冻结源码后测量 10 万事件、100/1000 页分页、50 次会话和合成稳定轨；真实 GPU/OCR、视觉和双浏览器混合轨需要独立回执，不能拿合成吞吐替代。
