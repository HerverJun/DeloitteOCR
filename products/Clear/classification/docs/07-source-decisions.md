# 来源与选型追溯

## 原始资料

实际读取文件为 `C:/Users/A/Desktop/少样本文档图像分类框架_GitHub调研与技术选型.docx`，文档署日期 2026-09-18。用户提供的路径在“框架”后多了一层目录分隔；已找到同名主题文件并读取全部段落、表格与超链接，未修改原文件。

全文提取见 `sources/source-extract.md`，文件 SHA256、提取统计与外链见 `sources/source-provenance.json`。原文没有嵌入图片，本次使用结构化文本与表格内容分析，未评估原文排版质量。

## 原文结论如何进入平台设计

| 原文结论 | 本准备包中的落实 |
| --- | --- |
| PyTorch 加 timm，冻结编码器与线性头 | 首版训练主线，P0 比较 DINOv2 与 ResNet18 |
| ONNX Runtime 正式推理 | 推理与训练隔离，固定模型包和预处理契约 |
| 十几万张与断点恢复 | SQLite 任务账本、租约、有界队列、逐项提交 |
| 固定类别编号与定义 | 稳定 label_id、标签版本、模型内连续索引 |
| 近重复与来源分组 | 清单包含 group_id，划分前合并关联来源 |
| 每轮有限人工预算 | 复核池、抽样批次、人工确认修订、新数据版本 |
| 其他与待复核分离 | 类别与决策状态独立，不把坏图或低分塞入其他 |
| 预处理与编码器变化使缓存失效 | 特征缓存键绑定全部特征契约 |
| 先验证再做界面 | P0/P1/P2 先于完整 Web 工作台 |

## 本准备包新增的工程决定

Web 形态、FastAPI、React、SQLite、任务租约、API 草案和数据库实体是针对通用平台的设计建议，不是原文已验证结论。将训练前划分细化为约 60/20/20，是为显式留出阈值与模型选择数据；原文只明确约 20% 独立评估并允许分组交叉验证。

A4000/A6000 来自原文的目标环境；4070 Ti SUPER、Windows 与磁盘容量来自当前机器只读检查。未把当前环境状态推断为其他机器的状态。

## 官方材料补充核验

2026-09-18 访问以下官方来源。完整 URL、抓取时间与内容哈希见 `sources/official-checks.json`；保存的原始响应仅用于来源复核。

| 来源 | 本次核验内容 | 使用方式 |
| --- | --- | --- |
| [timm 发布接口](https://api.github.com/repos/huggingface/pytorch-image-models/releases/latest) | 返回 v1.0.29，发布日期 2026-08-28 | 可作为版本候选，未测试安装和导出 |
| [DINOv2 模型卡](https://github.com/facebookresearch/dinov2/blob/main/MODEL_CARD.md) | ViT-S 384 维、patch 14、约 21M、冻结特征下游用法、Apache-2.0 | 支持候选设计，不证明本业务精度 |
| [ORT CUDA 要求](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html) | CUDA/cuDNN 主版本兼容，1.27 起默认 GPU 包 CUDA 13 | 安装时必须选定实际版本组合 |
| [LightlyTrain 许可文本](https://github.com/lightly-ai/lightly-train/blob/main/LICENSE) | 仓库 LICENSE 为 AGPL-3.0 | 继续作为备选，正式引入前核对具体分发与商业条款 |

另访问了 [PyTorch 安装页](https://pytorch.org/get-started/locally/)，页面包含动态安装选择器及不同版本文本，未据静态页面猜测“最新稳定版”作为锁定版本。安装以实际支持矩阵、包元数据和干净环境验证为准。

没有重新核验原文全部 12 个仓库的提交时间，也没有声称跑过其中的训练示例。旧仓库更新时间只能作为复核线索。

## 首版不引入的依赖

LightlyTrain 保留为减少训练开发的备选；只有默认 PyTorch/timm 路线成本明显不合理时再试。DINOv3 条件性对照，权重访问与自定义许可需单独记录。DiT 和 OpenCLIP 依据真实错误类型决定是否比较。

PaddleClas、MMPreTrain、EasyFSL 与 Ultralytics 可以借鉴方法或作为已有团队栈的替代，但当前没有证据支持同时引入。向量数据库、复杂元学习、自监督预训练、分布式训练和 TensorRT 不进入首版依赖。

代码许可证、预训练权重条款和数据授权分别登记；转换 ONNX 不改变来源条款。这里只记录选型差异，正式发布时以锁定版本的原始许可文件和交付范围为准。
