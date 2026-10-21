---
name: classification-training-preparation
description: 准备图片分类项目、固定资料、条件标注和不可变数据版本，并按用户目标调用已有训练器及读取真实模型产出。
compatibility: Clear Workbench；需要已安装分类原生服务及可用训练器，已有模型推理和新模型训练要区分。
metadata:
  host: clear-workbench
  requires-tools: classification_prepare classification_import_assets classification_read_annotations classification_annotate classification_publish_dataset classification_train
---

围绕用户目标准备或训练图片分类，使用原生项目、标注和训练工具。先读 `references/labels-and-splits.md`，交付时可读 `assets/training-record-template.md`。训练准备本身不是必须训练的授权；用户只要准备数据时完成准备即可。

普通审计或分类推理优先使用已有发布模型，不能因项目缺少模型便默认发起训练。用户明确需要训练时，才按以下步骤准备、固定数据版本、调用已有训练器并读取真实模型；采用或切换模型同样须对应用户目标。没有适用模型且用户未要求训练时，说明具体缺口或澄清需求。

1. 读取资料范围和明确类别。`classification_prepare` 或目录工具查询实际项目、标签和可用模型。新项目没有模型时不能宣称已可推理；标签业务含义无法确定时向用户提问。
2. `assets_create_set` 固定授权图片，再 `classification_import_assets` 接入已查询原生项目。读取 handoff_id、固定映射和资料哈希，不直接用平台 ID 冒作原生 ID。
3. `classification_read_annotations` 获取真实标签目录、当前 label_id/group_id 和 expected_hash。用 `classification_annotate` 仅写明确样本，保留 Agent 作者；预测或建议不冒作人工标注。版本冲突重读并使用新调用身份，不重发改变参数的旧操作。
4. `classification_publish_dataset` 生成固定资料的不可变训练版本。每类至少三个独立来源，来源分组避免同源泄漏。资料不足时交付具体缺口，不能复制样本凑数。
5. 用户需要训练且参数已明确时 `classification_train` 使用真实 dataset_id 和受控 encoder/device/epochs。等待原任务；`classification_read_training` 读取实际错误、指标和模型 ID。没有模型产出时不能说训练完成或虚构权重包。
6. 若目标包括采用模型，先读取实际 active_model_id，再 `classification_activate_model` 做条件切换。需要交付时 `classification_export_model` 下载真实通过包校验的模型，保留训练版本。
7. 推理使用已查询模型和固定资料集，分页 `classification_read_results`；按预测生成子集时保留模型/阈值/来源。`classification_dispose_results` 明确 AI 作者和操作状态。最后交付真实数据版本、训练状态或可下载模型，说明未训练/失败/低指标等限制。

训练器和正式模型执行使用已有专业服务。通用文件工具、终端与 Python 可用于授权输入整理、统计和输出；不能借辅助脚本绕过模型采用、正式版本或资料授权。普通推理无需审计计划，完成后直接说明真实结果与下载。
