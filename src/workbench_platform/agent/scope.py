"""Keep large task scopes authoritative in storage and bounded in prompts."""
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
    return output
