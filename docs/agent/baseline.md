# 平台 Agent 实施基线

实施授权：2026-10-04 用户要求完成《平台Agent开发计划-20261004》。独立运行记录在 `F:/OCR/output/platform-agent-dev-20261004/state.json`，原任务 JSON 保留为规格。

本次复用的三仓源码均在 `F:/OCR/output/clear-automation-dev-20261002/worktrees`，实际 Git 元数据位于各仓 `.git`。平台 HEAD 为 `cabba39e255c35ff1ac001b6401f7d699bc4cb35`；OCR 为 `fc53aabe77ef9139691a0880d7dfa4f4b9e037b1`；分类完整身份见运行目录 `baseline/repositories.json`。盘点时三仓工作区均干净；规划仓已有 README、计划和调研修改予以保留。Git 所有者差异用单命令 `-c safe.directory=...` 解决，未写 C 盘全局配置。

| 能力 | 实际入口 | 实施前状态 |
|---|---|---|
| 数据会话及处理 | DataWorkbenchService.create/submit/operation/resume_operation/page/download | 可用；原生请求幂等已存在，资料集上传需补幂等 |
| 资料与不可变集合 | AssetRegistry.stage_verified/list、AssetSetStore.create | 可用；工具仅传 ID，路径由服务端解析 |
| 数据成果归集 | ArtifactCenter.publish_data_export | 可用；保留真实结果与输入来源 |
| 图片 OCR | OcrHandoff、NativeOcrClient | 可用；PDF 不能提交到图片接口 |
| 分类 | ClassificationHandoff、NativeClassificationClient | 可用；需补预测子集工具 |
| PDF 页处理 | OCR document/application_services | 原生可用；平台按页请求去重需新增 |
| 取证 | ForensicService | 已发布六算法；不支持硬取消，不给真伪裁定 |
| 成果桥接 | 现有结果与导出接口 | 缺少通用类型化桥，不能宣传已完成 |
| 主控 | 平台独立连接 | 尚未配置；真实 API 成绩为空 |

合成夹具在运行目录 `fixtures`，包括两份 CSV/XLSX、两个多工作表工作簿及 C01–C08 预期。独立读回核对编号 `000001`、24 位编号及文字公式 `=SUM(1,2)`。图片、PDF、原生推理夹具随后随工具接入完善；预期规则不算实际业务运行证据。

Agent 依赖从 F 盘已有 OCR control 运行时读取，复制独立最小依赖层，锁定现有已验证版本作为平台候选；未修改 OCR/分类推理环境。不会复制未变的模型大包。框架、模拟运行和真实用户目标分别登记。
