"""Agent registration and compatibility exports for shared multimodal operations."""
from workbench_platform.application_services.operations.multimodal import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    registry.register(ToolSpec('multimodal_prepare', '对明确资料运行固定CPU工程诊断，保存本机模型验证回执；不下载或训练，不计业务分析交付。准备完成后继续实际业务分析。',
        Prepare, prepare, capability='multimodal', aliases=('准备', '人脸', '音频', '印章', 'CAT-Net'),
        effect='derive', lookup_operation=lookup_prepare, idempotent=True, input_kinds=('asset',), output_kinds=('job',)))
    for kind, name, description in (
        ('face', 'face_analyze', '人脸检测、比对或授权参考检索；search必须显式reference_ids且属于任务授权，不能扩大参考范围。'),
        ('audio', 'audio_transcribe', '原生音频转写、说话人和毫秒时间来源；保留不确定内容和未复核状态。'),
        ('seal', 'seal_extract', '原生印章区域与候选主体编号提取；未人工复核字段可进入草稿比对。'),
        ('catnet', 'catnet_analyze', '深度图像定位报告及热图，实际模型以结果登记为准；未校准响应只建议复核，不裁定真伪。'),
        ('document_forensics', 'document_forensics_analyze', '票据合同改字定位，JPEG及PNG真实深度分析，经典线索单列；未校准，不裁定真伪，可人工框选复核及固定导出。'),
        ('aigc_image', 'aigc_image_analyze', '整图 AI 生成相关信号的独立实验分析；与 SAFIRE 局部定位并列。未经校准只返回原始分数和无法判断，不输出生成概率、真实性结论或定位热图，不进入默认审计结论。')):
        registry.register(ToolSpec(name, description, Analyze, partial(analyze, kind), capability=kind,
            effect='derive', availability=partial(availability, kind=kind), lookup_operation=partial(lookup_analysis, kind),
            idempotent=True, input_kinds=('asset',), output_kinds=('job',)))
    for name, schema, handler, description, effect in (
        ('multimodal_read_result', Result, read, '分页读取人脸/音频/印章/深度取证/整图 AIGC 不可变结果快照、来源及复核状态。', 'read'),
        ('multimodal_export', Result, export, '导出固定快照的结果、时间字幕及热图，保留未复核标识。', 'derive'),
        ('multimodal_collect', Collect, collect, '归集可下载固定版本成果；require_review=true等待真实用户核准，未复核成果标为草稿。', 'derive'),
        ('multimodal_to_data_draft', Result, to_data, '印章候选确定性转Data草稿输入，保留原值/置信/区域来源和未复核状态，不能冒充核准。', 'derive'),
        ('multimodal_to_data_reviewed', Result, to_data_reviewed, '用户核准后沿原任务转换固定版本印章字段供正式Data核对；没有确认回执先等待用户。', 'derive'),
        ('multimodal_retry', Retry, retry, '仅重试本任务创建且终止状态明确的原生分析，在原job增加代次；携带当前版本/代次。', 'derive'),
        ('face_references_list', Empty, list_references, '只读本任务授权参考人脸，隐去特征向量；不默认查询整个项目参考库。', 'read'),
        ('multimodal_request_user_action', UserAction, request_action, '请求绑定当前不可变结果版本的真实用户核准，或明确参考库增删指令。', 'read'),
        ('multimodal_apply_user_action', ApplyAction, apply_action, '只执行已有绑定版本用户确认回执；模型不能提供确认内容或代签人工复核。', 'versioned')):
        registry.register(ToolSpec(name, description, schema, handler, capability='face' if name == 'face_references_list' else 'multimodal',
            aliases=('多模态', '人脸', '音频', '印章', 'CAT-Net'), effect=effect, idempotent=True,
            lookup_operation=partial(retry, lookup=True) if name == 'multimodal_retry' else
                lookup_user_action if name == 'multimodal_apply_user_action' else None,
            input_kinds=('result', 'job'), output_kinds=('result', 'artifact', 'asset')))
