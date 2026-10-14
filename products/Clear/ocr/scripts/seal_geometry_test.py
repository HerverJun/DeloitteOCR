"""Lock evaluated code, policy and actual adoption rule before sealed test runs."""
import argparse
import json
import sys
from pathlib import Path
from geometry_eval_common import ROOT,mapping_code_lock,sha,write_json
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.table_matching import default_policy


def main():
    p=argparse.ArgumentParser();p.add_argument('--development-report',type=Path,required=True);p.add_argument('--validation-report',type=Path,required=True)
    p.add_argument('--development-replay',type=Path,required=True);p.add_argument('--validation-replay',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise SystemExit('Selection lock is immutable')
    code=mapping_code_lock();policy=default_policy();reports={}
    for split,report_path,replay_path in [('development',a.development_report,a.development_replay),('validation',a.validation_report,a.validation_replay)]:
        report=json.loads(report_path.read_text('utf-8'));lock_path=replay_path/'run-lock.json';lock=json.loads(lock_path.read_text('utf-8'))
        if not report['complete'] or report['split']!=split or report['replay_lock_sha256']!=sha(lock_path):raise ValueError('Evaluation report is incomplete or unrelated')
        if lock['code']!=code or lock['policy']!=policy:raise ValueError('Final implementation must be evaluated before test sealing')
        reports[split]={'sha256':sha(report_path),'results':{method:groups['all'] for method,groups in report['groups'].items()}}
    write_json(a.output,{'version':2,'code':code,'policy':policy,'selection':'N1/N2 experimental; no strategy meets production thresholds on validation',
        'adoption_rule':{'engine':'paddlevl','selection':'only successful PaddleOCR-VL text/tables, no per-sample engine selection','revision':0},
        'reports':reports,'test_labels_examined_for_selection':False,'production_default':False})
    print(a.output)


if __name__=='__main__':main()
