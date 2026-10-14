# 文档助手阶段发布结果

## 2026-09-24 当前阶段决定：先做本机演示

当前冻结候选为 `candidate-09`。按[最新发布状态](../audit/ocr-agent-20260921-langgraph/release-status.json)，当前源码后端 683 项（681 通过、2 跳过）、前端 51/51 与构建、本地 HTTP/React 下载、独立服务 60 秒短烟测均已有通过回执；09 第三次完整包审计为 `pass_with_qualification_limits`，前两次原始 `fail` 保留，07 完整 ZIP 加 09 累积差量仅证明双件逻辑重建。包和源码的具体哈希、路径及限制见[交付索引](../audit/ocr-agent-20260921-langgraph/delivery-index.md)。

本轮 `demo_ready=true`：冻结 `candidate-09` 已在本机隔离工作区启动，`http://127.0.0.1:8765/` 首页 200、健康状态 ready；合成项目与样本图片在实际页面可见，文档助手面板可打开，页面无脚本错误。见[现场演示回执与截图](../audit/ocr-agent-20260924-demo-preview-01/SUMMARY.md)。面板提示先配置主控连接，未执行真实主控对话；演示不暗示真实模型准确率。`production_qualified=false`、`phase_complete=false` 保持；真实独立主控 0/72、干净 Windows、物理断网、跨用户 DPAPI、旧版完整回退、09 全量物理搬迁和生产性能指标移至后续生产资格。用户已取消新的四小时混合长轨，不再排期；旧 formal 原始失败及 F06 `not_qualified` 保留，短烟测不充当长期稳定成绩。具体门槛见[分阶段验收计划](ocr-agent-acceptance-plan-20260920.md)。

以下为早期 `candidate-08` 阶段记录，保留当时证据与更正。其“新独立四小时轨仍待执行”和 08 为当前候选等表述均已被上面的 09 阶段决定替代，不作为现行待办或资格结论。

> 更正：candidate-08 formal-01 提前停止是助手误解用户要求，用户已澄清继续推进真实验收。原始失败回执保留；新独立四小时轨仍待执行。下文早先“因用户要求提前收尾”的表述以此更正为准。

> candidate-08 formal-01 因助手误操作在 6611.016/14400 秒提前终止，原始 failed，F06 性能与稳定性均 not_qualified。阶段工作已恢复；该回执留作失败历史，详见 [更正与交接](../audit/ocr-agent-20260921-langgraph/FINAL-HANDOFF-20260924.md)。

记录日期：2026-09-24（香港时间）。本阶段可提供带限制说明的 `candidate-08` 实验候选目录，以及 07 完整 ZIP 加 08 差量 ZIP 的双件逻辑交付；**阶段未完成，生产资格未取得**，不宣布任何真实主控模型组合通过。当前动态状态和候选/归档准确 SHA-256 见 [发布状态](../audit/ocr-agent-20260921-langgraph/release-status.json)与[交付索引](../audit/ocr-agent-20260921-langgraph/delivery-index.md)。冻结包内的 `candidate-status.json` 是构建时快照，不能代替包外审计及正式轨回执。

## 已取得的工程证据

- LangGraph 1.2.11 与 checkpoint-sqlite 3.1.1 在独立包内运行，业务 schema 14、12 项工具；默认关闭，实验入口显式启用。运行效果、授权、预算、事件和产物属于业务库，图恢复状态属于独立官方 saver，两库没有原子提交承诺。
- 本地合成主控覆盖工具配对、幂等、任务等待、显式继续、取消、未知模型请求处理、范围版本和部分导出。真实本地 GLM OCR 的 20 页 PDF 已生成 XLSX，历史界面实际下载哈希与原文件一致。测试方法和源码快照见开发回执；不把该负载解释为真实主控准确率。
- 取消问答修复后的[完整后端回归](../audit/ocr-agent-20260921-langgraph/full-backend-post-cancel-corrected-20260924.log)为 660 项，658 通过、2 跳过；最终差量工具另有 7 项定向测试通过。[前端测试](../audit/ocr-agent-20260921-langgraph/frontend-final-source-tests-20260923.log) 51/51，构建通过。工程回归不能代替真实模型准确率、成本和目标机验证。
- `candidate-08` 的[构建回执](../audit/ocr-agent-20260921-langgraph/bundle-build-08.json)记录 73,630 文件、29,104,348,303 字节，目录清单 SHA-256 为 `91a30c3ba499c104f4949b81e6793961a7b42f98c90f2ed86b87997de5b91938`；[独立包审计](../audit/ocr-agent-20260921-langgraph/bundle-audit-08/bundle-validation.json)为 `pass_with_qualification_limits`。D 盘 07 完整 ZIP SHA-256 为 `9754d2fbb861b88d66e443721d89f5f31ead9ee3dd15594ec45400360a1ed648`，F 盘 08 差量 ZIP SHA-256 为 `8564bbe8ad40719eeeeb97f3162ccf688c1205a87ab3de4a76e2490ff1af8600`；[双件独立验证](../audit/ocr-agent-20260921-langgraph/delta-independent-08.json)为 `pass`，逻辑清单可重建 08。**这不是 08 的独立完整 ZIP，也不是真实搬迁验证。**构建目录的 69,277 个模型和非 service 运行时资产是共享 hardlink，**不是独立物理副本**。
- `candidate-06` 的[完整约 29 GB 同机搬迁](../audit/ocr-agent-20260921-langgraph/full-relocation-06/full-relocation.json)和隔离启动通过且有限制；`candidate-08` 的完整搬迁尚未测试，不能沿用 06 的候选身份。
- 上述 660/658/2 后端日志及 51/51 前端日志、构建均早于包后新增的 E37/E40 导出源码与覆盖清单 UI，不能证明当前源码已全量回归。[导出完整性审计](../audit/ocr-agent-20260924-e37-e40-e44-export-integrity.md)仅有隔离合成 5 项通过，尚无新候选、前端重构建/测试或新 HTTP/真实 UI 下载实跑。[resume 强退窗口回执](../audit/ocr-agent-20260924-resume-crash-window.md)仅覆盖业务结果及 `tool_finished` 提交后、图 checkpoint 落后的一条窗口（1 项及邻近 2 项通过）；图领先 UI 投影等窗口仍缺。E37/E40/E44 与 E67/E68/F02 均保持 partial。

## 资格决定与保留限制

实验候选只能在明确授权的项目与独立实验工作区试用。真实主控配置的机器可读名单见 [支持状态](../audit/ocr-agent-20260921-langgraph/supported-configurations.json)，目前没有已验证组合：72 次封存评测执行数为 0，不打开封存题、不借用视觉连接凭据。另项目资源、临时 I/O、损坏 checkpoint 和模型错误场景有专项证据，但本地单用户 Bearer 不构成多租户身份边界。

`candidate-08` 的新三重复、14,400 秒正式混合轨原始回执 `E:\OCR Agent 测试\mixed-candidate08-formal-01\mixed-stability.json` 终态为 `failed`，因用户要求提前收尾，未取得性能或四小时资格。`candidate-07` 的原始 formal-01 回执 `E:\OCR Agent 测试\mixed-candidate07-formal-01\mixed-stability.json` 保持 `failed`：15 组基线比较完成 14 组，控制会话的 checkpoint 换段被拦截，四小时混合段未开始。[失败诊断与有界修复回执](../audit/ocr-agent-20260924-candidate07-cancel-rollover-fix.md)定位已取消的 `ask_user` 调用和待结澄清决定；源码修复通过 21 项相关测试，08 正式轨已按用户要求提前结束，不改变 07 原始失败结论。此前 `candidate-05` 的 [formal-02](../audit/ocr-agent-20260921-langgraph/mixed-candidate05-formal-02/mixed-stability.json) 因 checkpoint 空间风险受控中止，原始状态 `failed`，不可当作通过。干净 Windows、跨用户 DPAPI、物理断网、`candidate-08` 完整搬迁、旧版可执行程序回退与目标设备内容质量无本阶段完整实证，分别保留 `not_tested`。合成稳定轨、100 页原生提取、20 页 GPU 单批及短混合烟测不能相加当作最终四小时混合资格；若关键条件未获验证，不据此发布生产版。

## 操作和回退

普通入口默认关闭助手；“启动实验助手.cmd”才进入实验模式，默认独立工作区 `%LOCALAPPDATA%\OfflineOCR-Agent-Experimental\Workspace`。迁移前备份整个工作区、业务库和 saver，并保留原件、结果文件及与旧程序相容的旧工作区副本。旧程序不得直接打开 schema 14；回退使用旧包及对应旧备份，升级后原工作区原样保留。具体包/归档哈希和双库检查路径见交付索引。

使用方式见 [用户说明](ocr-agent-usage.md)，维护与异常处理见 [运维说明](ocr-agent-operations.md)，工程分工和图版本策略见 [开发维护说明](ocr-agent-development-notes.md)。
