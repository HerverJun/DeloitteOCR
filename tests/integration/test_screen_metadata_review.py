"""Portal review schema carries native source choices within the frozen image."""
import pytest
from pydantic import ValidationError
import test_screen_batches as fixtures
from workbench_platform.jobs.screen_batches import ScreenAction

bridge=fixtures.bridge


def arguments(**extra):
    return {'action':'review','request_id':'metadata-review-001','version_ids':['version0'],
        'result_id':'result0','expected_revision':0,'state':'retain',
        'expected_review_token':'a'*64,
        'metadata_edits':[{'item_index':0,'before':'单位：元','after':'单位：万元','choice':'manual'}],**extra}


@pytest.mark.parametrize('patch',[
    {'action':'pause'}, {'candidate_id':'another-result'}, {'version_ids':['version0','version1']},
    {'metadata_edits':[{'item_index':True,'before':'a','after':'b'}]},
    {'metadata_edits':[{'item_index':0,'before':'a','after':'b','choice':'guess'}]},
    {'expected_review_token':'bad-token'},
])
def test_metadata_review_contract_rejects_unbound_choices(patch):
    with pytest.raises(ValidationError):ScreenAction.model_validate(arguments(**patch))


@pytest.mark.parametrize('bridge',[1],indirect=True)
def test_pinned_metadata_edits_reach_the_same_native_review_and_foreign_image_is_rejected(bridge,monkeypatch):
    client,workspace,fixed,state,service,_=bridge
    from workbench_platform.jobs.screen_batches import ScreenSubmit
    service.submit(workspace,ScreenSubmit.model_validate(fixtures.arguments(fixed)))
    native=service.handoff._adapter(None);original=native._request;seen=[]
    def observe(method,path,body=None,**kwargs):
        if method=='POST' and path.endswith('/action'):seen.append(body)
        return original(method,path,body,**kwargs)
    monkeypatch.setattr(native,'_request',observe)
    monkeypatch.setattr(service.handoff,'_adapter',lambda context:native)
    route=f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches/batchA/action'
    response=client.post(route,headers=fixtures.LOCAL,json=arguments())
    assert response.status_code==200
    assert len(seen)==1 and seen[0]['metadata_edits']==arguments()['metadata_edits']
    assert seen[0]['expected_review_token']=='a'*64
    response=client.post(route,headers=fixtures.LOCAL,json=arguments(request_id='metadata-review-002',version_ids=['foreign-image']))
    assert response.status_code==400 and len(seen)==1
