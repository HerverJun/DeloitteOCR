"""Run actual source-first regressions and save machine-readable evidence."""
import argparse
from contextlib import redirect_stderr,redirect_stdout
import importlib.metadata
import json
from pathlib import Path
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'scripts'),str(ROOT/'build/external-api-review/site-packages')]
from complex_table_run import AUDIT,save,sha


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',choices=['focused','full'],default='focused');parser.add_argument('--name',default=None);args=parser.parse_args()
    import ocr_workbench
    assert Path(ocr_workbench.__file__).resolve()==ROOT/'src/ocr_workbench/__init__.py'
    folder=AUDIT/'regression';folder.mkdir(parents=True,exist_ok=True)
    name=args.name or args.suite
    loader=unittest.defaultTestLoader
    patterns=['test_*.py'] if args.suite=='full' else ['test_complex_tables.py','test_table_matching.py','test_tableformer_next.py',
        'test_structure_workflow.py','test_pdf_table_tools.py','test_table_tool_lifecycle.py','test_external_review.py',
        'test_external_review_integration.py','test_exporting.py','test_pdf_export.py']
    suite=unittest.TestSuite(loader.discover(str(ROOT/'tests'),pattern=p) for p in patterns)
    before={p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'src/ocr_workbench').glob('*.py')}
    start=time.perf_counter();log=folder/(name+'.log')
    with log.open('w',encoding='utf-8') as stream,redirect_stderr(stream),redirect_stdout(stream):
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    after={p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'src/ocr_workbench').glob('*.py')}
    report={'suite':args.suite,'tests':result.testsRun,'passed':result.wasSuccessful() and before==after,'source_unchanged':before==after,
        'failures':[{'test':t.id(),'traceback':e} for t,e in result.failures],
        'errors':[{'test':t.id(),'traceback':e} for t,e in result.errors],
        'skipped':[{'test':t.id(),'reason':e} for t,e in result.skipped],
        'seconds':time.perf_counter()-start,'source_root':str(ROOT/'src'),'python':sys.version,
        'source_files':{p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'src/ocr_workbench').glob('*.py')},
        'test_files':{p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'tests').glob('test_*.py')},'log_sha256':sha(log)}
    save(folder/(name+'.json'),report)
    print(json.dumps({k:report[k] for k in ('tests','passed','seconds','failures','errors','skipped')},ensure_ascii=False),flush=True)
    raise SystemExit(not report['passed'])


if __name__=='__main__':main()
