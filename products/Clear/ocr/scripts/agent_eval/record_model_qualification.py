"""Record unavailable real-controller evaluation without reading sealed tasks."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--reason', required=True, help='Concrete reason why no real evaluation was executed; never a success claim')
    args = parser.parse_args()
    audit = args.audit.resolve()
    if not audit.is_dir():
        parser.error('Existing audit run is required')
    evidence = {}
    for relative in ['audit/ocr-agent-20260920-p0/evaluation-split.json', 'docs/ocr-agent-acceptance-plan-20260920.md',
                     'audit/ocr-agent-20260921-langgraph/connection-probe-results.json', 'audit/ocr-agent-20260921-langgraph/provider-contract-results.json']:
        path = ROOT / relative
        evidence[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    timestamp = datetime.now(timezone.utc).isoformat()
    evaluation = {'task_id': 'F05', 'report_status': 'complete_not_tested', 'qualification_status': 'not_tested', 'updated_utc': timestamp,
        'reason': args.reason, 'configuration_count': 0, 'real_requests': 0, 'real_usage': None, 'real_cost': None,
        'planned_sealed_tasks': 24, 'planned_repetitions': 3, 'planned_runs': 72, 'executed_runs': 0,
        'success_numerator': None, 'success_denominator': 0, 'confidence_interval': None,
        'early_development_smoke': 'not_tested', 'sealed_questions_opened_for_this_report': False,
        'sealed_score_generated': False, 'visual_credentials_reused': False, 'evidence_sha256': evidence,
        'release_effect': 'No real controller, protocol/model combination or S01-S06 autonomous success rate is qualified. Synthetic engineering checks remain separate.',
        'next_action': 'Use an independently configured, explicitly authorized controller: two-turn probe, 3-5 development smoke tasks, then the frozen 24 x 3 evaluation with usage and failure scoring.'}
    configurations = {'updated_utc': timestamp, 'task_id': 'F05', 'status': 'no_verified_real_configurations', 'verified_configurations': [],
        'implemented_protocols': [{'protocol': protocol, 'local_contract_evidence': 'provider-contract-results.json',
                                   'real_model_qualification': 'not_tested', 'supported_real_models': []}
                                  for protocol in ('openai_chat_completions', 'anthropic_messages')],
        'reason': args.reason, 'evaluation': 'model-evaluation.json', 'experimental_only': True}
    for name, value in [('model-evaluation.json', evaluation), ('supported-configurations.json', configurations)]:
        (audit / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print('F05 report recorded: no real configuration qualified; sealed questions unopened.')


if __name__ == '__main__':
    main()
