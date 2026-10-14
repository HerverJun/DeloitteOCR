"""Known cache assignments are rewritten in memory without redirecting home."""
from pathlib import Path
import sys
import pytest
from modules.manual_multimodal.paddle_cache import checked_f,rewrite_cache_assignment,install_paddle_cache


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
