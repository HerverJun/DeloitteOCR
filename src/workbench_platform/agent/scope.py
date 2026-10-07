"""Keep large task scopes authoritative in storage and bounded in prompts."""
from pathlib import Path
from packages.contracts.agent import AgentRef
from packages.contracts.models import AssetSetItem,SourceLocator
from .store import digest


def set_summary(fixed):
    return {'asset_set_id':fixed.asset_set_id,'revision':fixed.revision,'manifest_hash':fixed.manifest_hash,
            'item_count':len(fixed.items)}


def fixed_scope(ctx):
    if not ctx.selected_assets:
        return None
    registry,sets,_=ctx.services.asset_library
    items=[]
    for identifier in dict.fromkeys(ctx.selected_assets):
        asset=registry.get(ctx.workspace_id,identifier)
        locator=SourceLocator(schema_version='1.0',kind='file',asset_id=identifier,
            asset_revision=asset.revision,blob_hash=asset.blob_hash,precision='unknown')
        items.append(AssetSetItem(schema_version='1.0',item_id='i'+digest([identifier,asset.revision,asset.blob_hash])[:32],
            asset_id=identifier,asset_revision=asset.revision,blob_hash=asset.blob_hash,source_locator=locator))
    key='s'+digest([ctx.workspace_id,[(item.asset_id,item.asset_revision,item.blob_hash) for item in items]])[:40]
    return sets.create(ctx.workspace_id,items,asset_set_id=key)


def scope_summary(ctx):
    fixed=fixed_scope(ctx)
    output={'workspace_id':ctx.workspace_id,'selected_asset_count':len(ctx.selected_assets),
        'scope':'selected' if ctx.selected_assets else 'workspace',
        'selected_assets':list(ctx.selected_assets[:10]),'preview_only':len(ctx.selected_assets)>10}
    if fixed:
        output['selected_asset_set']={key:value for key,value in set_summary(fixed).items() if value is not None}
        output['selected_asset_set']['reference']=AgentRef(kind='asset_set',workspace_id=ctx.workspace_id,
            object_id=fixed.asset_set_id,revision=fixed.revision,sha256=fixed.manifest_hash).model_dump(mode='json')
        hint=screen_fastpath(ctx,fixed)
        if hint:output['ocr_screen_batch_fastpath']=hint
    return output


def screen_fastpath(ctx,fixed):
    """Read-only, live binding validation; never starts a service or guesses a link."""
    services=ctx.services
    if getattr(services,'job_service',None) is None or getattr(services,'screen_batches',None) is None:
        return None
    try:
        from workbench_platform.application_services.native import availability
        from workbench_platform.jobs.ocr_handoff import IMAGE_EXTENSIONS
        if not 1<=len(fixed.items)<=1000:return None
        registry=services.asset_library[0]
        for item in fixed.items:
            asset=registry.get(ctx.workspace_id,item.asset_id)
            if (asset.revision,asset.blob_hash)!=(item.asset_revision,item.blob_hash):return None
            if (not asset.media_type.startswith('image/') or
                Path(asset.original_name).suffix.lower() not in IMAGE_EXTENSIONS-{'.pdf'}):return None
        if availability(services,'ocr')['state']!='ready':return None
        links=[link for link in services.workspace_store.list_links(ctx.workspace_id)
            if link.app_id=='ocr' and link.role!='viewer']
        if len(links)!=1:return None
        link=links[0]
        bound=services.job_service._context(ctx.workspace_id,'ocr',link.link_id)
        if bound.module_project_id!=link.module_project_id:return None
        adapter=services.job_service._adapter(bound)
        state=adapter._request('GET','/api/state')
        if not any(project.get('id')==bound.module_project_id for project in state.get('projects',[])):
            return None
        if not adapter.capabilities().recognition:return None
        return {'verified':True,'link_id':link.link_id,'project_id':bound.module_project_id,
            'asset_set_id':fixed.asset_set_id,'asset_set_revision':fixed.revision,
            'basis':'live-managed-project-and-fixed-input-set','requires_prepare':False}
    except (ValueError,KeyError,AttributeError,TypeError,OSError):
        return None
