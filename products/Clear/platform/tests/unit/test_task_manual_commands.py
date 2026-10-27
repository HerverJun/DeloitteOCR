"""Public task commands must satisfy the actual manual service input contract."""
import pytest

from packages.contracts.multimodal import ManualAnalysisCreate
from packages.contracts.tasks import TaskCreate
from workbench_platform.tasks.registry import command_for


@pytest.mark.parametrize('kind', ('face', 'audio', 'seal', 'catnet', 'document_forensics', 'aigc_image'))
def test_manual_command_is_accepted_without_changing_its_frozen_input(kind):
    asset = {'asset_id': 'assetManualInput', 'asset_revision': 3, 'blob_hash': 'a' * 64}
    options = {'device': 'cpu' if kind in {'face', 'seal'} else 'cuda',
               'action': 'detect' if kind == 'face' else 'analyze'}
    submitted = TaskCreate(workspace_id='wManualProject', tool_id=kind, action='analyze',
                           idempotency_key='manual-request-fixed', inputs=[asset], options=options)
    command = command_for(submitted.model_dump())
    accepted = ManualAnalysisCreate.model_validate(command['payload'])
    assert command['path'] == '/api/platform/v1/workspaces/wManualProject/multimodal/jobs'
    assert accepted.kind == kind
    assert accepted.request_id == submitted.idempotency_key
    assert accepted.inputs[0].model_dump() == asset
    assert accepted.options.device == options['device']
    assert accepted.options.action == options['action']
