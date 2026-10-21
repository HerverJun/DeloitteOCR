"""One discovery projection over executable ToolSpecs and declared capabilities."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


DECLARATIONS = (
    CapabilitySpec('documents', '文档解析与阅读', ('文档结构', '原生文档', 'DOCX', 'PPTX', 'OFD', '页块读取'), ('DocVortex 0.5.16 CPU worker',)),
    CapabilitySpec('ocr', '文档识别', ('OCR', '票据', '合同', 'PDF', '文字识别'), ('原生OCR服务',)),
    CapabilitySpec('data', '数据清洗与比对', ('台账', '表格', '核对', '清洗', '差异')),
    CapabilitySpec('classification', '图像分类', ('分类', '标注', '训练', '项目模型'), ('原生分类服务',)),
    CapabilitySpec('forensics', '基础图像取证', ('取证', '篡改线索', '热图'), ('本地取证运行库',)),
    CapabilitySpec('face', '人脸分析', ('人脸', '比对', '参考检索'), ('人脸模型及验证回执',)),
    CapabilitySpec('audio', '音频分析', ('音频', '转写', '录音', '时间定位'), ('音频模型及验证回执',)),
    CapabilitySpec('seal', '印章识别', ('印章', '公章', '候选字段', '草稿'), ('印章模型及验证回执',)),
    CapabilitySpec('catnet', '深度图像取证', ('SAFIRE', 'CAT-Net', 'CATNet', '深度取证'), ('实际深度模型及本机工程验证回执',)),
    CapabilitySpec('document_forensics', '文档篡改排查', ('改字', '金额篡改', '合同篡改', '文档取证'), ('SAFIRE模型及本机工程验证回执',)),
)


def metadata(spec):
    family = spec.name.split('_', 1)[0]
    capability = spec.capability or family
    declaration = next((item for item in DECLARATIONS if item.id == capability), None)
    return {'family': family, 'capability': capability,
            'aliases': list(dict.fromkeys((*getattr(declaration, 'aliases', ()), *spec.aliases))),
            'dependencies': list(dict.fromkeys((*getattr(declaration, 'dependencies', ()), *spec.dependencies)))}


def project(registry, services=None):
    declarations = {item.id: item for item in DECLARATIONS}
    declarations.update(registry.capabilities)
    # A newly registered adapter enters discovery without a second UI list.
    for spec in registry.tools.values():
        identifier = metadata(spec)['capability']
        declarations.setdefault(identifier, CapabilitySpec(identifier, identifier))
    output = []
    for identifier, declaration in declarations.items():
        tools = [registry.describe(name, services) for name, spec in registry.tools.items()
                 if metadata(spec)['capability'] == identifier]
        # Reading a catalog or old result does not prove inference is usable.
        primary = [tool for tool in tools if registry.tools[tool['name']].availability is not None]
        states = [tool['availability'].get('state', 'unavailable') for tool in primary or tools]
        state = 'ready' if 'ready' in states else 'preparable' if 'preparable' in states else 'unavailable'
        blockers = list(dict.fromkeys(str(tool['availability'].get('reason')) for tool in tools
                                    if tool['availability'].get('reason')))
        if not tools:
            blockers = ['尚未登记Agent执行处理器']
        output.append({'id': identifier, 'name': declaration.name, 'aliases': list(declaration.aliases),
            'binding': 'bound' if tools else 'pending', 'state': state,
            'tool_names': [tool['name'] for tool in tools],
            'dependencies': list(dict.fromkeys([*declaration.dependencies,
                *(dependency for tool in tools for dependency in tool['dependencies'])])),
            'blockers': blockers})
    return output
