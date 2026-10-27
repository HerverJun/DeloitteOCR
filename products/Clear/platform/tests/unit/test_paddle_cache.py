"""Known cache assignments are rewritten in memory without redirecting home."""
from pathlib import Path
import hashlib
import json
import os
import sys
import uuid
import pytest
from modules.manual_multimodal.paddle_cache import checked_f,rewrite_cache_assignment,install_paddle_cache
from modules.manual_multimodal import paddle_cache


@pytest.fixture
def managed_paddle(monkeypatch,tmp_path):
    program=tmp_path/'程序 包'
    (program/'app/platform').mkdir(parents=True)
    package=program/'assets/program/bundles/ocr/runtimes/ppocr/Lib/site-packages/paddle'
    for name in ('dataset','utils'):
        (package/name).mkdir(parents=True)
        (package/name/'__init__.py').write_text('',encoding='utf-8')
    (package/'__init__.py').write_text('',encoding='utf-8')
    sources={
        'paddle.dataset.common': "import os\nHOME='unchanged'\nDATA_HOME=os.path.join(os.environ['PADDLE_HOME'],'dataset')\nAFTER=DATA_HOME\n",
        'paddle.utils.download': "import os\nWEIGHTS_HOME=os.path.join(os.environ['PADDLE_HOME'],'hapi','weights')\nAFTER=WEIGHTS_HOME\n",
    }
    files={};pins={};declarations=[]
    for fullname,source in sources.items():
        relative=paddle_cache.SPECIFICATIONS[fullname][0]
        path=package/relative
        raw=source.encode();path.write_bytes(raw);files[fullname]=path
        pins[fullname]=hashlib.sha256(raw).hexdigest()
        declarations.append({'member':path.relative_to(program).as_posix(),
            'patched_sha256':pins[fullname],'patched_bytes':len(raw),'version':'3.3.1',
            'resource_id':'program_assets','environment':'PADDLE_HOME'})
    manifest=program/'manifests/vendor-compatibility.json';manifest.parent.mkdir()
    manifest.write_text(json.dumps({'format':'clear-vendor-compatibility-v1',
        'patch_set':'paddle-managed-cache-v1','changes_home_or_userprofile':False,
        'files':declarations}),encoding='utf-8')
    paths={name:str(program/name) for name in ('assets','runtimes','tools')}
    paths.update(program=str(program),**{name:str(tmp_path/name) for name in
        ('data','cache','temp','paddle_temp','secrets','backup','postgres')})
    config=tmp_path/'clear.install.json'
    config.write_text(json.dumps({'schema_version':1,'installation_id':str(uuid.uuid4()),
        'mode':'single','paths':paths,'resources':{'platform_source':'app/platform','program_assets':'assets/program'},
        'network':{'transport':'loopback-http','listen':'127.0.0.1','public_origin':'http://127.0.0.1:18889',
            'portal_port':18889,'worker_port':18890,'postgres_port':15432}}),encoding='utf-8')
    monkeypatch.setenv('CLEAR_INSTALL_CONFIG',str(config))
    prior=tmp_path/'data/worker/runtime/inherited-paddle-home'
    monkeypatch.setenv('PADDLE_HOME',str(prior))
    monkeypatch.setattr(paddle_cache,'REVIEWED_BUILD_PATCHES',pins)
    monkeypatch.syspath_prepend(str(package.parent))
    assert not any(name=='paddle' or name.startswith('paddle.') for name in sys.modules)
    yield {'cache':tmp_path/'data/worker/runtime/paddle-cache','files':files,
        'manifest':manifest,'prior_paddle_home':str(prior)}
    for name in list(sys.modules):
        if name=='paddle' or name.startswith('paddle.'):sys.modules.pop(name)


def test_managed_fixed_patch_is_verified_and_imported_without_ast_rewrite(monkeypatch,managed_paddle):
    case=managed_paddle
    before={path:path.read_bytes() for path in (*case['files'].values(),case['manifest'])}
    original_home={name:os.environ.get(name) for name in ('HOME','USERPROFILE','CODEX_HOME')}
    def no_rewrite(*args,**kwargs):
        raise AssertionError('A verified fixed-build source must not be rewritten again')
    monkeypatch.setattr(paddle_cache,'rewrite_cache_assignment',no_rewrite)
    adapter=install_paddle_cache(case['cache'])
    try:
        import paddle.dataset.common as dataset
        import paddle.utils.download as weights
        assert dataset.DATA_HOME==str((case['cache']/'dataset').resolve())
        assert weights.WEIGHTS_HOME==str((case['cache']/'hapi/weights').resolve())
        assert os.environ['PADDLE_HOME']==str(case['cache'].resolve())
        identity=adapter.identity()
        assert identity['verified_build_patch'] and identity['build_patch_manifest_sha256']
        assert all(row['imported'] and not row['memory_ast_rewrite'] for row in identity['modules'])
    finally:adapter.close()
    assert os.environ['PADDLE_HOME']==case['prior_paddle_home']
    assert original_home=={name:os.environ.get(name) for name in original_home}
    assert before=={path:path.read_bytes() for path in before}


def test_managed_manifest_cannot_authorize_an_unreviewed_changed_source(managed_paddle):
    case=managed_paddle
    path=case['files']['paddle.dataset.common']
    path.write_bytes(path.read_bytes()+b'# unreviewed change\n')
    manifest=json.loads(case['manifest'].read_text('utf-8'))
    row=manifest['files'][0]
    row.update(patched_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),patched_bytes=path.stat().st_size)
    case['manifest'].write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='reviewed fixed-build patch'):
        install_paddle_cache(case['cache'])
    assert os.environ['PADDLE_HOME']==case['prior_paddle_home']


def test_managed_source_is_rechecked_at_import(managed_paddle):
    case=managed_paddle;adapter=install_paddle_cache(case['cache'])
    try:
        path=case['files']['paddle.dataset.common']
        path.write_bytes(path.read_bytes()+b'# changed after validation\n')
        with pytest.raises(ValueError,match='source changed'):
            import paddle.dataset.common
    finally:adapter.close()


def test_managed_rejects_an_unregistered_paddle_home(monkeypatch,managed_paddle):
    monkeypatch.setenv('PADDLE_HOME','C:/unregistered-paddle-cache')
    with pytest.raises(ValueError,match='registered write roots'):
        install_paddle_cache(managed_paddle['cache'])


def test_managed_patch_manifest_is_mandatory(managed_paddle):
    managed_paddle['manifest'].unlink()
    with pytest.raises(FileNotFoundError):
        install_paddle_cache(managed_paddle['cache'])


@pytest.mark.parametrize('module,source,variable',[
 ('paddle.dataset.common',"import os\nHOME='unchanged-home'\nDATA_HOME=os.path.join(HOME,'.cache','paddle','dataset')\nAFTER=DATA_HOME\n",'DATA_HOME'),
 ('paddle.utils.download',"import os.path as osp\nWEIGHTS_HOME=osp.expanduser('~/.cache/paddle/hapi/weights')\nAFTER=WEIGHTS_HOME\n",'WEIGHTS_HOME')])
def test_only_known_assignment_rhs_changes_and_downstream_reads_it(module,source,variable,tmp_path):
    original=source.encode();target=tmp_path/'known-cache'
    namespace={};exec(rewrite_cache_assignment(original,module,target,'unchanged-vendor.py'),namespace)
    assert namespace[variable]==str(target) and namespace['AFTER']==str(target)
    if module.endswith('common'):assert namespace['HOME']=='unchanged-home'
    assert original==source.encode() and not target.exists()


@pytest.mark.parametrize('source',[
 "DATA_HOME='other-path'", "DATA_HOME=os.path.join(HOME,'.cache','paddle','dataset')\nDATA_HOME='again'",
 "if True:\n DATA_HOME=os.path.join(HOME,'.cache','paddle','dataset')",
 "DATA_HOME: str=os.path.join(HOME,'.cache','paddle','dataset')",'SOME_OTHER_VARIABLE=1'])
def test_unknown_or_ambiguous_vendor_assignment_fails_closed(source,tmp_path):
    with pytest.raises(ValueError):rewrite_cache_assignment(source.encode(),'paddle.dataset.common',tmp_path/'cache','vendor.py')


def test_non_f_cache_does_not_create_a_directory():
    with pytest.raises(ValueError):checked_f('C:/codex-forbidden-paddle-cache',create=True)


def test_adapter_checks_both_sources_before_import_and_never_writes_vendor(monkeypatch,tmp_path):
    package=tmp_path/'site/paddle';(package/'dataset').mkdir(parents=True);(package/'utils').mkdir()
    (package/'__init__.py').write_text('',encoding='utf-8')
    (package/'dataset/__init__.py').write_text('',encoding='utf-8');(package/'utils/__init__.py').write_text('',encoding='utf-8')
    common=package/'dataset/common.py';common.write_text("import os\nHOME='unchanged'\nDATA_HOME=os.path.join(HOME,'.cache','paddle','dataset')\n",encoding='utf-8')
    download=package/'utils/download.py';download.write_text("import os.path as osp\nWEIGHTS_HOME=osp.expanduser('~/.cache/paddle/hapi/weights')\n",encoding='utf-8')
    before={path:path.read_bytes() for path in (common,download)}
    monkeypatch.syspath_prepend(str(package.parent))
    assert not any(name.startswith('paddle') for name in sys.modules)
    adapter=install_paddle_cache(tmp_path/'cache')
    try:
        import paddle.dataset.common as dataset
        import paddle.utils.download as weights
        assert dataset.DATA_HOME==str((tmp_path/'cache/dataset').resolve())
        assert weights.WEIGHTS_HOME==str((tmp_path/'cache/hapi/weights').resolve())
        assert all(row['imported'] for row in adapter.identity()['modules'])
        assert not adapter.identity()['vendor_files_modified'] and not adapter.identity()['home_environment_modified']
        with pytest.raises(ValueError):install_paddle_cache(tmp_path/'second-cache')
    finally:
        adapter.close()
        for name in list(sys.modules):
            if name=='paddle' or name.startswith('paddle.'):sys.modules.pop(name)
    assert before=={path:path.read_bytes() for path in before}
