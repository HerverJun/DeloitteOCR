"""New native metadata history needs its reader capability on admission/backup."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import pytest

from scripts.backup_workspace import create, restore, verify
from scripts.product_compatibility import DATA_COMPATIBILITY, check
from workbench_platform.reader_requirements import infer_reader_requirements, marker_requirements


@pytest.fixture
def native_metadata(tmp_path,monkeypatch):
    ocr=Path(__file__).resolve().parents[3]/'ocr'
    monkeypatch.syspath_prepend(str(ocr/'tests'));monkeypatch.syspath_prepend(str(ocr/'src'))
    from ocr_workbench.store import Store
    from ocr_workbench.screen_batches import ScreenBatches
    import test_screen_batches as batch_fixture
    from test_business_metadata_edits import MetadataEditTests
    root=tmp_path/'platform';root.mkdir()
    for name in ('workspaces.sqlite3','assets.sqlite3'):
        with closing(sqlite3.connect(root/name)):pass
    fixture=MetadataEditTests('runTest');fixture.root=root/'native/ocr';fixture.root.mkdir(parents=True)
    fixture.store=Store(fixture.root)
    fixture.project=fixture.store.project('Metadata current reader contract')['id']
    fixture.queue=batch_fixture.Queue(fixture.store)
    fixture.manager=ScreenBatches(fixture.store,fixture.queue,fixture.root,prepare=batch_fixture.prepare,select=batch_fixture.select)
    fixture.manager.defaults=lambda:{}
    state,item,result,index,aux,primary=fixture.prepare_conflict()
    fixture.manager.action(fixture.project,state['batch_id'],fixture.body('accepted',item,result,
        metadata_edits=[{'item_index':index,'before':primary,'after':aux,'choice':'auxiliary'}]))
    fixture.manager.stop()
    yield root,fixture,result['id']


def test_actual_metadata_requires_new_reader_and_backup_restore_retains_current_history(native_metadata,tmp_path):
    root,fixture,result_id=native_metadata
    assert 'screen-business-metadata-edit-v1' in infer_reader_requirements(root)['reader_features']
    legacy=deepcopy(DATA_COMPATIBILITY);legacy['reader_features'].remove('screen-business-metadata-edit-v1')
    with pytest.raises(ValueError):check(root,legacy)
    assert check(root,DATA_COMPATIBILITY)['compatible']
    current=fixture.store.result(result_id)
    backup=tmp_path/'backup';create(root/'native/ocr',backup,kind='ocr');verify(backup)
    assert 'screen-business-metadata-edit-v1' in marker_requirements(backup)['reader_features']
    destination=tmp_path/'restored';restore(backup,destination)
    from ocr_workbench.store import Store
    restored=Store(destination)
    assert restored.result(result_id)==current
    assert 'screen-business-metadata-edit-v1' in infer_reader_requirements(destination)['reader_features']


@pytest.mark.parametrize('tamper',['input_version','revision','log'])
def test_metadata_actual_record_binding_tamper_rejected(native_metadata,tamper):
    root,fixture,result_id=native_metadata
    with fixture.store.transaction() as db:
        row=db.execute('SELECT edited FROM results WHERE id=?',(result_id,)).fetchone();value=json.loads(row['edited'])
        if tamper=='input_version':value['screen']['metadata_review']['input_version_id']='foreign'
        elif tamper=='revision':value['screen']['metadata_review']['base_revision']=99
        else:value['screen']['metadata_edit_log'][0]['image_version']='foreign'
        db.execute('UPDATE results SET edited=?,revision=revision+1 WHERE id=?',(json.dumps(value),result_id))
    with pytest.raises(ValueError):infer_reader_requirements(root)
