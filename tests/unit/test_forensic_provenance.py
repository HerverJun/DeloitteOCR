"""Active-code checks and publication identities reject silent legacy fallback."""
from contextlib import nullcontext
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from workbench_platform.forensics.service import (
    LEGACY_TOOLBOX_FILES,source_identity,verify_toolbox_source,validate_report_toolbox)
from workbench_platform.forensics.artifacts import publication_versions,publish
from workbench_platform.forensics import artifacts
from workbench_platform.artifacts.store import ArtifactUnavailable
from workbench_platform.batches.store import fingerprint
from workbench_platform.data_api.service import DataError


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path):
    root=tmp_path/'source'
    toolbox=root/'modules/image_forensics/forensic_toolbox';toolbox.mkdir(parents=True)
    for name in sorted(LEGACY_TOOLBOX_FILES|{'geometry.py'}):
        (toolbox/name).write_text('__version__ = "0.1.1"\n' if name=='__init__.py' else '# '+name+'\n',encoding='utf-8')
    worker=root/'modules/image_forensics/worker.py';worker.write_text('# fixed worker\n',encoding='utf-8')
    upstream=[{'name':name,'sha256':'a'*64} for name in sorted(LEGACY_TOOLBOX_FILES)]
    derived={'version':'0.1.1','base_version':'0.1.0','coordinate_version':'exif-pixel-edges-v1',
        'worker_sha256':sha(worker),'files':[{'name':path.name,'sha256':sha(path)} for path in sorted(toolbox.glob('*.py'))]}
    return root,{'source':'read-only upstream','files':upstream,'derived':derived}


def report_for(provenance,execution):
    return {'runtime':{'toolbox':{'version':execution['version'],'path':execution['runtime_path']}},
        'toolbox_source':provenance,'toolbox_execution':execution,'evidence':[{'version':execution['version']}]}


def test_valid_derived_code_uses_active_hashes_and_keeps_original_identity(source):
    root,provenance=source
    original=copy.deepcopy(provenance)
    execution=verify_toolbox_source(provenance,root)
    assert execution['kind']=='derived' and execution['version']=='0.1.1'
    assert execution['source_identity_sha256']==fingerprint(source_identity(provenance))
    assert execution['runtime_path']==str((root/'modules/image_forensics/forensic_toolbox').resolve())
    assert provenance==original
    validate_report_toolbox(report_for(provenance,execution),execution)


@pytest.mark.parametrize('change', ['missing_record','missing_file','wrong_hash','wrong_version','worker_changed',
    'missing_worker_hash','duplicate','extra_file','unsafe_name','null_derived','empty_derived'])
def test_bad_derived_record_never_falls_back_to_upstream(source,change):
    root,provenance=source
    toolbox=root/'modules/image_forensics/forensic_toolbox'
    # The upstream section is made valid for its registered files to prove that
    # an explicitly bad derived declaration cannot silently select it instead.
    provenance['files']=[{'name':name,'sha256':sha(toolbox/name)} for name in sorted(LEGACY_TOOLBOX_FILES)]
    active=provenance['derived']
    if change=='missing_record':active['files']=[value for value in active['files'] if value['name']!='geometry.py']
    elif change=='missing_file':(toolbox/'geometry.py').unlink()
    elif change=='wrong_hash':active['files'][0]['sha256']='b'*64
    elif change=='wrong_version':active['version']='0.1.2'
    elif change=='worker_changed':(root/'modules/image_forensics/worker.py').write_text('# changed worker',encoding='utf-8')
    elif change=='missing_worker_hash':active.pop('worker_sha256')
    elif change=='duplicate':active['files'].append(active['files'][0].copy())
    elif change=='extra_file':(toolbox/'unregistered.py').write_text('# unregistered code',encoding='utf-8')
    elif change=='unsafe_name':active['files'][0]['name']='../worker.py'
    elif change=='null_derived':provenance['derived']=None
    elif change=='empty_derived':provenance['derived']={}
    with pytest.raises((DataError,OSError)):verify_toolbox_source(provenance,root)


def test_real_legacy_six_file_manifest_and_missing_runtime_metadata_remain_supported(tmp_path):
    root=tmp_path/'legacy';toolbox=root/'modules/image_forensics/forensic_toolbox';toolbox.mkdir(parents=True)
    for name in sorted(LEGACY_TOOLBOX_FILES):
        (toolbox/name).write_text('__version__ = "0.1.0"\n' if name=='__init__.py' else '# legacy\n',encoding='utf-8')
    provenance={'files':[{'name':name,'sha256':sha(toolbox/name)} for name in sorted(LEGACY_TOOLBOX_FILES)]}
    execution=verify_toolbox_source(provenance,root)
    assert execution['kind']=='legacy' and execution['version']=='0.1.0'
    validate_report_toolbox({'runtime':{'python':'3.12'}},execution)
    assert publication_versions({'tools':['metadata'],'report':{}})==('forensic-toolbox-0.1.0',fingerprint(['metadata']))


@pytest.mark.parametrize('change', ['missing','wrong_path','wrong_version','wrong_tool_version'])
def test_report_must_confirm_actual_toolbox_path_and_version(source,change):
    root,provenance=source;execution=verify_toolbox_source(provenance,root)
    report=report_for(provenance,execution)
    if change=='missing':report.pop('runtime')
    elif change=='wrong_path':report['runtime']['toolbox']['path']=str(root/'another-toolbox')
    elif change=='wrong_version':report['runtime']['toolbox']['version']='0.1.0'
    elif change=='wrong_tool_version':report['evidence'][0]['version']='0.1.0'
    with pytest.raises(DataError):validate_report_toolbox(report,execution)


def test_new_artifact_metadata_binds_tools_and_verified_derived_source(source):
    root,provenance=source;execution=verify_toolbox_source(provenance,root)
    item={'tools':['noise','jpeg'],'report':report_for(provenance,execution)}
    version,rule=publication_versions(item)
    assert version=='forensic-toolbox-0.1.1'
    assert rule==fingerprint({'tools':item['tools'],'toolbox_source_identity':execution['source_identity_sha256']})
    assert rule!=fingerprint(item['tools'])
    changed=copy.deepcopy(item);changed['report']['toolbox_source']['derived']['worker_sha256']='b'*64
    with pytest.raises(ArtifactUnavailable):publication_versions(changed)
    changed=copy.deepcopy(item);changed['report'].pop('toolbox_execution')
    with pytest.raises(ArtifactUnavailable):publication_versions(changed)


def test_ready_historic_bundle_is_returned_without_relabeling(monkeypatch):
    existing={'bundle_id':'historic','files':[{'name':'report.json','artifact_id':'historicArtifact'}]}
    center=SimpleNamespace(lock=nullcontext(),store=SimpleNamespace(publication_lock=lambda _:nullcontext()))
    service=SimpleNamespace(get=lambda *_:{'status':'succeeded','report':{}},app=SimpleNamespace(state=SimpleNamespace(artifact_center=center)))
    monkeypatch.setattr(artifacts,'get',lambda *_:existing)
    def fail(_):raise AssertionError('A ready historic bundle must not be relabeled')
    monkeypatch.setattr(artifacts,'publication_versions',fail)
    assert publish(service,'workspace','historic') is existing
