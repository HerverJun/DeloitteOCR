"""Check the G01 user, operations, and development manuals against v2 scope."""
import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / 'audit/ocr-agent-20260921-langgraph'
DOCS = {
    'usage': ROOT / 'docs/ocr-agent-usage.md',
    'operations': ROOT / 'docs/ocr-agent-operations.md',
    'development': ROOT / 'docs/ocr-agent-development-notes.md',
    'release': ROOT / 'docs/ocr-agent-release-results.md',
}
REQUIRED = {
    'usage': {
        'configuration_and_scope': ['配置主控', '允许此地址读取当前项目', '图片外发另行确认'],
        'six_scenarios': ['找表或关键词', '处理指定 PDF 页', '检查财务表', '视觉审校疑点', '重试失败子集', '导出本轮结果'],
        'stop_recovery': ['停止助手', '停止并取消本轮自建任务', '重启后等待继续'],
        'budget_and_adoption': ['本轮预算与用量', '不会自动采用', '部分覆盖'],
        'artifacts_and_offline': ['保留产物', '清理此产物', '离线'],
        'qualification_list': ['supported-configurations.json', '当前名单为空'],
    },
    'operations': {
        'storage_and_credentials': ['workbench.sqlite3', 'agent-checkpoints.sqlite3', 'DPAPI'],
        'migration_and_rollback': ['schema 12→13→14', 'restore_backup', '回退旧程序'],
        'diagnosis': ['API 401', 'API 409', '主控结果未知', '产物不可下载'],
        'qualification': ['supported-configurations.json', '四小时混合稳定性'],
    },
    'development': {
        'graph_business_split': ['LangGraph 负责', '业务 DB 负责', '两库没有原子提交'],
        'context_and_checkpoint': ['prepare_context', '正式 saver', 'graph/state/serializer', '不能通过 arbitrary thread'],
        'replay_and_fencing': ['operation', 'fencing', 'sent/unknown 禁止自动再发'],
        'packaging': ['依赖锁', '许可证', '不能带开发 checkpoint'],
    },
    'release': {
        'qualification_decision': ['实验候选', '不宣布生产资格', '支持状态', 'not_tested'],
        'recovery_and_provenance': ['双库', '旧工作区副本', '交付索引'],
    },
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=AUDIT / 'documentation-review.json')
    args = parser.parse_args()
    evidence, missing = {}, []
    for label, document in DOCS.items():
        content = document.read_text('utf-8')
        lines = content.splitlines()
        checks = {}
        for topic, terms in REQUIRED[label].items():
            checks[topic] = {}
            for term in terms:
                locations = [index for index, line in enumerate(lines, 1) if term in line]
                checks[topic][term] = locations
                if not locations:
                    missing.append(f'{label}:{topic}:{term}')
        links = []
        for line in lines:
            for fragment in line.split('](')[1:]:
                target = fragment.split(')', 1)[0].split('#', 1)[0]
                if target and not target.startswith(('http:', 'https:', '/')):
                    resolved = (document.parent / target).resolve()
                    links.append({'target': target, 'exists': resolved.is_file()})
                    if not resolved.is_file():
                        missing.append(f'{label}:broken_link:{target}')
        evidence[label] = {
            'path': document.relative_to(ROOT).as_posix(),
            'sha256': hashlib.sha256(document.read_bytes()).hexdigest(),
            'lines': len(lines), 'topics': checks, 'relative_links': links,
        }
    supported = json.loads((AUDIT / 'supported-configurations.json').read_text('utf-8'))
    real = supported['verified_configurations']
    if real:
        missing.append('manual_says_empty_but_supported_configurations_is_nonempty')
    result = {
        'task': 'G01', 'status': 'pass_with_explicit_qualification_limits' if not missing else 'fail',
        'generated_utc': datetime.now(timezone.utc).isoformat(), 'documents': evidence,
        'real_controller_support_list': 'audit/ocr-agent-20260921-langgraph/supported-configurations.json',
        'verified_real_configurations': len(real), 'missing_or_broken': missing,
        'scope': 'Document presence, topic references, and local links; browser behavior and final package identity are separate evidence.',
        'limitations': ['No independent real controller configuration.', 'Clean Windows and cross-user qualifications remain not_tested.',
                        'The manual check does not prove the final candidate/archive identity; inspect package and delivery receipts separately.'],
    }
    target = args.output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'missing_or_broken': missing}, ensure_ascii=False))
    if missing:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
