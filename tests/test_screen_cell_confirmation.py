"""Cell confirmations preserve source bindings, explicit scope and idempotency."""
from copy import deepcopy
import io

from PIL import Image
import pytest

from test_screen_local_candidates import candidate_fixture as base_candidate_fixture
from ocr_workbench.screen_batches import ScreenBatches
from ocr_workbench.screen_cell_review import list_groups, preview, confirm, review_summary
from ocr_workbench.store import Conflict


@pytest.fixture
def cell_fixture(tmp_path, monkeypatch):
    # Reuse local-candidate input pixels, with the provenance normally added
    # by the production screen candidate assembler before final publication.
    final_result = ScreenBatches._final_result
    def assembled_result(manager, batch, item, raw):
        return final_result(manager, batch, item, {**deepcopy(raw), 'origin': 'screen_batch'})
    monkeypatch.setattr(ScreenBatches, '_final_result', assembled_result)
    generator = base_candidate_fixture.__wrapped__(tmp_path)
    try:
        yield next(generator)
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


def test_confirm_identical_source_cells_keeps_originals_and_never_confirms_whole_images(cell_fixture):
    store, project, batch, versions, queue, manager = cell_fixture
    proposal = list_groups(manager, project, batch)
    assert proposal['automatic_confirmations'] == 0
    assert proposal['groups']
    group = proposal['groups'][0]
    assert len({item['source_sha256'] for item in group['members']}) == 2
    with Image.open(io.BytesIO(preview(manager, project, batch, group['group_id']))) as image:
        assert list(image.size) == [group['members'][0]['width'], group['members'][0]['height']]
    before = {item['result_id']: deepcopy(store.result(item['result_id'])) for item in group['members']}
    request = {'request_id': 'explicit-cell-confirm-001', 'group_id': group['group_id'],
               'after': '00123456789012345678', 'apply_to_all_listed_cells': True}
    assert confirm(manager, project, batch, request, allow_new=False) is None
    with pytest.raises(ValueError):
        confirm(manager, project, batch, {**request, 'apply_to_all_listed_cells': False})
    receipt = confirm(manager, project, batch, request)
    assert receipt['status'] == 'committed'
    assert receipt['whole_images_confirmed'] is False
    assert receipt['business_metadata_confirmed'] is False
    assert confirm(manager, project, batch, request) == receipt
    with pytest.raises(Conflict):
        confirm(manager, project, batch, {**request, 'after': 'different'})
    for member in group['members']:
        result = store.result(member['result_id'])
        assert result['original'] == before[result['id']]['original']
        assert result['revision'] == before[result['id']]['revision'] + 1
        cells = result['edited']['tables'][member['table']]['cells']
        cell = next(cell for cell in cells if (cell['row'], cell['column']) == (member['row'], member['column']))
        assert cell['text'] == request['after']
        summary = review_summary(manager, batch, member['input_version_id'])
        assert summary['confirmed_count'] >= 1 and summary['whole_image_confirmed'] is False


def test_result_revision_change_rejects_entire_proposed_cell_group(cell_fixture):
    store, project, batch, versions, queue, manager = cell_fixture
    group = list_groups(manager, project, batch)['groups'][0]
    member = group['members'][0]
    with store.transaction() as db:
        db.execute('UPDATE results SET revision=revision+1 WHERE id=?', (member['result_id'],))
    before = store.rows('SELECT id,edited,revision FROM results ORDER BY id')
    with pytest.raises(Conflict):
        confirm(manager, project, batch, {'request_id': 'stale-cell-confirm-001', 'group_id': group['group_id'],
            'after': '123.45', 'apply_to_all_listed_cells': True})
    assert store.rows('SELECT id,edited,revision FROM results ORDER BY id') == before
    assert not store.rows('SELECT * FROM screen_actions WHERE request_id=?', ('stale-cell-confirm-001',))
