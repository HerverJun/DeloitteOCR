"""Close the run only after the new bundle and every archive byte are verified."""
from collections import Counter
import json
from pathlib import Path
import shutil
from complex_table_run import ROOT, BUILD, AUDIT, save, sha, update


def main():
    delivery=Path('E:/DeloitteOCR-ComplexTables-20260919')
    package=json.loads((AUDIT/'archive-checksums.json').read_text('utf-8'))
    archive=json.loads((AUDIT/'archive-verification.json').read_text('utf-8'))
    bundle=json.loads((delivery/'multimodal-bundle-verification.json').read_text('utf-8'))
    smoke=json.loads((BUILD/'packaged-smoke-03/receipt.json').read_text('utf-8'))
    assert package['source_bytes_verified'] and archive['passed'] and bundle['passed'] and smoke['passed']
    assert package['manifest_sha256']==archive['bundle_manifest_sha256']==bundle['manifest_sha256']
    assert sha(delivery/'bundle/manifest.json')==package['manifest_sha256']
    assert Path(package['archive']).is_file() and Path(package['archive']).stat().st_size==package['archive_bytes']
    manifest=json.loads((delivery/'bundle/manifest.json').read_text('utf-8'))
    forbidden=[r['path'] for r in manifest['files'] if Path(r['path']).suffix.lower() in {'.sqlite','.db','.dpapi','.secret'}
        or Path(r['path']).name=='.env' or any(x in Path(r['path']).parts for x in ['complex-tables-20260919-01','node_modules'])]
    assert not forbidden,forbidden
    source=json.loads((AUDIT/'regression/full-final-01.json').read_text('utf-8'))
    assert all(sha(ROOT/p)==h for p,h in source['source_files'].items())
    save(AUDIT/'packaged-smoke.json',smoke)
    save(AUDIT/'bundle-verification.json',bundle)
    save(AUDIT/'delivery-receipt.json',{'engineering':'passed','formal_quality':'not_met','real_service':'not_measured',
        'version':'0.13.0rc1','schema':12,'delivery':str(delivery),'bundle':str(delivery/'bundle'),
        'archive':package,'archive_verification':'archive-verification.json','bundle_verification':'bundle-verification.json',
        'packaged_smoke':'packaged-smoke.json','excluded_sensitive_paths':forbidden,
        'old_bundle_preserved':'E:/DeloitteOCR-Intranet-20260918/bundle','production_source_unchanged_since_regression':True})
    entries=[]
    exclude={'artifact-index.json','progress.json','NEXT.md'}
    paths=[p for p in AUDIT.rglob('*') if p.is_file() and p.name not in exclude]
    for folder in ['dataset','runs','raw','derived']:
        paths += [p for p in (BUILD/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    paths += [BUILD/'sources.jsonl']
    for p in sorted(set(paths)):
        entries.append({'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)})
    save(AUDIT/'artifact-index.json',{'files':entries,'scope':'retained reports, labels, public source inputs, transformed images and per-item runs; no inferred quality passes',
        'excluded_mutable_ledgers':sorted(exclude),'regression_source_lock':'regression/full-final-01.json',
        'model_dependency_locks':str(delivery/'bundle/locks'),'runtime_verified_by_manifest':package['manifest_sha256']})
    update('D01','done',[f'audit/{AUDIT.name}/artifact-index.json',f'audit/{AUDIT.name}/final-seal.json'],
        engineering='passed',next_step='Evidence is frozen; expand data only when the recorded retry trigger is satisfied.')
    update('D04','done',[f'audit/{AUDIT.name}/delivery-receipt.json',f'audit/{AUDIT.name}/archive-checksums.json',
        f'audit/{AUDIT.name}/archive-verification.json',f'audit/{AUDIT.name}/packaged-smoke.json'],engineering='passed',
        next_step='Completed experimental delivery. See gaps.json for subsequent independent quality, real-service and target-device validation.')
    state=json.loads((AUDIT/'progress.json').read_text('utf-8'))
    assert len(state['tasks'])==31
    for task in state['tasks']:
        assert task['status'] in {'done','done_with_gaps'} and task['engineering']=='passed'
        assert task['evidence'] and all((ROOT/p).exists() for p in task['evidence'])
        task['next_command']='No remaining engineering work in this run; follow gaps.json retry conditions for additional validation.'
    state['outcome']={'engineering':'passed','formal_quality':'not_met','real_service':'not_measured',
        'tasks':dict(Counter(t['status'] for t in state['tasks'])),'delivery':str(delivery),'archive':package['archive']}
    save(AUDIT/'progress.json',state)
    (AUDIT/'NEXT.md').write_text('# 已完成实验交付\n\n'
        '31 项均有真实证据，状态为 done 或 done_with_gaps。工程完成；正式质量 not_met；真实 API、人工效率、目标设备 GPU not_measured。\n\n'
        f"交付 ZIP：{package['archive']}\nSHA256：{package['sha256']}\n\n"
        '无本任务遗留运行进程；最终浏览器与服务均已关闭。不得重新覆盖旧实验和新交付物。\n'
        '接续仅在 gaps.json 中的新数据、授权服务或目标设备条件成立后另建 run；不复用已曝光样本充当独立封存组。\n'
        '核验入口：delivery-receipt.json、archive-verification.json、artifact-index.json。\n','utf-8')
    # Verification receipts live next to the ZIP; adding them inside would change its sealed bytes.
    companion=delivery/'final-evidence'
    if companion.exists():raise ValueError('Final evidence already published')
    shutil.copytree(AUDIT,companion)
    (delivery/'交付校验.txt').write_text(f"DeloitteOCR 0.13.0rc1 / schema 12\nZIP: {package['archive']}\nSHA256: {package['sha256']}\n"
        f"文件数: {archive['files']}；每条解压 CRC 与 SHA256、精确清单通过。\n"
        '工程通过；正式质量未达标；真实 API 与目标 GPU 未测。详见 final-evidence。\n','utf-8-sig')
    print(json.dumps(state['outcome'],ensure_ascii=False))


if __name__=='__main__':main()
