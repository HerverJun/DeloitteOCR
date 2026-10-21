"""Agent registration and compatibility exports for shared classification_management operations."""
from workbench_platform.application_services.operations.classification_management import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    for name, schema, handler, description, effect in [
        ('classification_read_annotations', Page, read_labels, '读取固定 handoff 的资料、标签、条件写入哈希与训练版本目录。', 'read'),
        ('classification_annotate', Annotate, annotate, '按已读取expected_hash批量标注固定资料，明确Agent作者，不冒称人工确认。', 'versioned'),
        ('classification_publish_dataset', Publish, publish, '发布仅含固定资料的不可变训练版本；每类需至少3个独立来源，沿用原生划分约束。', 'derive'),
        ('classification_train', Train, submit, '在范围核实后的固定训练版本运行现有原生训练器，等待完成并返回实际模型。', 'derive'),
        ('classification_read_training', TrainingRead, read_training, '读取真实训练状态、错误和模型ID/指标，供后续推理。', 'read'),
        ('classification_activate_model', ModelActivate, activate, '按已读取expected_active_model_id选择实际存在且通过原生包校验的活动模型。', 'versioned'),
        ('classification_export_model', ModelSelect, export_model, '下载通过原生校验的实际模型包并归集为平台可下载成果，附训练元数据。', 'derive'),
        ('classification_dispose_results', Disposition, dispose, '按所见分类结果引用确认类别、暂缓或重开，明确AI操作人，保留原预测与版本历史。', 'versioned'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
                                  lookup_operation={'classification_train': lookup, 'classification_annotate': annotate_lookup,
                                      'classification_publish_dataset': publish_lookup, 'classification_dispose_results': dispose_lookup,
                                      'classification_activate_model': activate_lookup, 'classification_export_model': export_model_lookup}.get(name),
                                  availability=lambda services: availability(services, 'classification')))
