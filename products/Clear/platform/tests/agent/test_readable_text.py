"""Text output has exact UTF-8 bytes and explicit character pagination."""
from types import SimpleNamespace

from packages.contracts.agent import AgentRef
from packages.contracts.models import AssetSetItem, SourceLocator
from workbench_platform.artifacts.center import ArtifactCenter
from workbench_platform.artifacts.text_outputs import publish_text_result, text_page
from workbench_platform.assets.registry import AssetRegistry
from workbench_platform.assets.sets import AssetSetStore


def test_long_unicode_text_pages_reconstruct_full_text_without_silent_truncation():
    text = ('这是第 ００１ 行；金额 10.00 😀\r\n' * 600) + '末尾'
    first = text_page(text)
    assert first['text_truncated'] and first['text_total_chars'] == len(text)
    assert first['text_next_offset'] == 8000
    pieces, offset = [], 0
    while offset is not None:
        page = text_page(text, offset=offset)
        pieces.append(page['text'])
        offset = page['text_next_offset']
    assert ''.join(pieces) == text
    assert text_page('完整文字')['text_truncated'] is False


def test_text_sidecar_reuses_fixed_result_and_downloads_exact_full_utf8(tmp_path):
    fixtures, blobs = tmp_path / 'fixtures', tmp_path / 'blobs'
    fixtures.mkdir(); blobs.mkdir()
    (fixtures / 'image.png').write_bytes(b'fixed-image')
    registry = AssetRegistry(tmp_path / 'assets.sqlite3', blobs, {'fixtures': fixtures})
    sets = AssetSetStore(registry)
    source = registry.register('workspace', 'fixtures', 'image.png', media_type='image/png', storage_mode='managed')
    locator = SourceLocator(schema_version='1.0', kind='file', asset_id=source.asset_id,
                            asset_revision=1, blob_hash=source.blob_hash, precision='exact')
    member = AssetSetItem(schema_version='1.0', item_id='member', asset_id=source.asset_id,
                         asset_revision=1, blob_hash=source.blob_hash, source_locator=locator)
    center = ArtifactCenter(tmp_path / 'artifacts', registry, sets, None)
    ctx = SimpleNamespace(workspace_id='workspace', operation_id='odelivery', services=SimpleNamespace(artifact_center=center))
    ref = AgentRef(kind='result', workspace_id='workspace', object_id='result', revision=2,
                   application='ocr', parent_id='handoff', sha256='a' * 64)
    text = '完整原文 00001，10.00\r\n' * 1000
    verified = {'snapshot': {'available': True, 'edited': {'text': text}},
                'source_asset': member.model_dump(mode='json'), 'locator': {'page_number': 5}}
    output = publish_text_result(ctx, ref, verified=verified)
    replay = publish_text_result(ctx, ref, verified=verified)
    assert replay['delivery']['bundle_id'] == output['delivery']['bundle_id']
    assert output['readable_text']['text_total_chars'] == len(text)
    assert output['readable_text']['text_truncated'] is True
    bundle = output['delivery']['bundle_id']
    artifact = center.file('workspace', bundle, 'ocr-text.txt')
    with center.store.stage_download('workspace', artifact.artifact_id,
            expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
        assert path.read_bytes() == text.encode('utf-8')
    verified['snapshot']['edited']['text'] = '人工修订'
    revised = publish_text_result(ctx, ref.model_copy(update={'revision': 3}), verified=verified)
    assert revised['delivery']['bundle_id'] != bundle
    assert ctx.operation_id == 'odelivery'
