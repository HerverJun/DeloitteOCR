# 通用图像分类工作台 v0.2

可在本机运行的图像分类平台，包含图片导入与标注、数据版本、GPU 训练、模型库、批量分类和人工复核。支持按项目定义类别，适用于文档、截图、商品等整图单标签分类。

技术栈：React / TypeScript / Ant Design + FastAPI + SQLite + PyTorch / timm + ONNX Runtime。使用预训练编码器提取特征，训练线性分类头，再导出包含编码器的完整 ONNX 模型。

## 直接运行

本机已安装独立 `.venv`、编译前端，并准备了真实公开基准 Flowers102 与明确标记的合成图形演示项目。若服务已经运行，直接打开 **http://127.0.0.1:8765**。

重新启动，在 PowerShell 中执行：

```powershell
cd E:\project\image-classification-platform
.\scripts\start.ps1
```

启动脚本运行 API 和后台任务进程。按 Ctrl+C 停止。默认只监听本机，不向局域网开放。接口文档位于 http://127.0.0.1:8765/docs 。

## 已实现

| 功能 | 当前能力 |
| --- | --- |
| 项目 | 创建项目、定义类别、项目隔离、项目切换 |
| 图片与标注 | 目录登记、上传、缩略图、搜索、类别筛选、批量标注、来源分组 |
| 数据版本 | 不可变清单、内容 SHA256、分组防泄漏、保留历史验证/测试划分 |
| 训练 | ResNet18 / DINOv2 / MobileNetV4 / RepViT、224 / 336 输入、冻结编码器、AdamW / 验证集正则化搜索、特征缓存、GPU / CPU |
| 模型库 | 准确率 / 宏 F1 / 接受覆盖率 / 置信区间、混淆矩阵、ONNX 一致性验证、模型包下载、默认模型切换 |
| 分类与复核 | GPU / CPU 批处理、进度、暂停/恢复/取消、失败项重试、人工标签修订、CSV / JSONL 导出 |
| 恢复 | SQLite 持久化、任务租约、崩溃后保留已提交结果、逐项错误记录 |
| 运行环境 | GPU、依赖、磁盘信息；可独立执行 ONNX 分类命令 |

## 从业务图片开始

1. 新建项目，填写类别 ID 和中文名称；一个项目采用固定类别集。
2. 在“图像与标注”中导入目录或上传图片。目录的第一层子目录名可匹配类别 ID 或中文名称。
3. 检查标签，并为同一文档、同一商品或同一来源的相关图片设置相同来源组，避免训练/测试泄漏。
4. 发布数据版本。首版每类至少需要 3 个独立内容/来源组才能划分；实际效果评估需要更多有代表性的样本。
5. 在“训练与实验”中选择数据版本和编码器。先用 ResNet18 建立基线，再比较 DINOv2。
6. 查看模型评估，创建批量分类任务，人工复核并导出结果。默认全部预测待复核。

详细步骤、重新安装、存储和备份见 [运行与开发指南](docs/08-running-and-development.md)。实际接口契约见 [OpenAPI](docs/openapi.json)，后续开发项见 [后续迭代](docs/09-next-iterations.md)。

## 已完成的验证

- Python 数据处理、API、任务恢复等测试，以及真实浏览器操作验证；详见 `reports/development-validation.json`。
- RTX 4070 Ti SUPER：ResNet18 224、DINOv2 224、DINOv2 336 的真实预训练权重训练、完整 ONNX 导出与 CUDA 推理均通过。
- ONNX 与 PyTorch 在 batch 1 / 3 / 16 / 32 上通过数值、类别和决策一致性校验，剖析记录确认 CUDA 节点实际执行。
- 15 万条任务记录的建立、分页索引和崩溃恢复验证通过；这不是 15 万张真实图片的端到端性能验收。
- 独立预测命令已验证不导入 PyTorch；本机 CUDA DLL 仍来自已安装的 GPU 环境。

已完成 Oxford Flowers102 真实图像评测：官方 8,189 张，隔离 6 张跨集合重复图后，训练 1,019 / 验证 1,015 / 测试 6,149。四种骨干、三种子 AdamW 与验证集正则化搜索共 16 个实验。详见 [公开评测与 v0.2 结果](docs/11-public-benchmark-and-v02.md)。公开花卉识别成绩不代表文档或工业业务验收。

## 当前边界

v0.2 面向单机单用户，不含账号权限、多人协作、远程服务、对象检测、OCR、多标签分类或在线自动训练。业务来源组由用户整理；公开数据导入脚本具备 SHA256、感知哈希与人工复核记录，尚无通用近重复审核界面。自动接受阈值处于试验阶段。训练中断需重新创建任务，批量分类可从已提交批次恢复。

`docs/01` 至 `docs/07`、`configs/`、`schemas/` 和 `sources/` 保留原始调研与开发准备材料；其中标记为 proposed / planned 的内容属于设计，实际 v0.2 行为以本文、运行指南和实现为准。旧 `reports/environment.json` 是安装前环境，新的独立 GPU 环境记录为 `reports/project-environment.json`。

## 轻量 CPU 推理

`dist/inference-cpu-mobilenetv4.zip` 与 `dist/inference-cpu-repvit.zip` 提供独立推理源码、模型和锁定依赖；解压后按包内 README 创建 Python 3.12 环境。推理不需要 PyTorch、timm、CUDA 或 Web 服务。该包并非包含 Python 解释器的单文件程序。

公开数据位于 `E:/image-platform-data/public/flowers102`；复现脚本和测速口径见 [评测报告](docs/11-public-benchmark-and-v02.md)。后台 worker 与直接评测脚本共用租约，运行评测脚本前应停止交互 worker 并清空活动任务。

## 从零开始的实机教学

[教学文件与操作说明](deliverables/花卉三分类教学/使用说明.md)包含中文配音录屏、90 张 Flowers102 公开照片、实际训练的模型包及分类结果。从空项目走通导入、标注、数据冻结、训练、独立图片分类、人工复核、导出与刷新恢复。教学只覆盖 3 类小样本，不替代完整公开基准或业务验收。

Git、干净环境 CI、Windows 安装和包含外部原图的备份恢复步骤见 [工程化交付指南](docs/12-git-ci-install-backup.md)。大体积教学视频与模型包保留在本机交付目录，不纳入 Git；新克隆仓库应运行数据获取和教学脚本自行生成这些产物。

CSV / JSONL 保留原始预测 `label_id`，并增加导出时的 `current_annotation_label_id` 和优先采用当前标注的 `effective_label_id`。后者可能回退到未经人工复核的预测；仅需已标注数据时应筛选非空的当前标注字段。复现脚本及测试范围见 [教学验收说明](deliverables/花卉三分类教学/测试验收说明.md)。
