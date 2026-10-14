import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest
from workbench_platform.data_api.service import DataError
from workbench_platform.multimodal.revisions import identified_rows, normalized_edits, reviewed_result
from workbench_platform.multimodal.exports import export_files


def example(kind='audio', count=65):
    field = 'segments' if kind == 'audio' else 'seals'
    rows = [{'start_ms': i * 1000, 'end_ms': (i + 1) * 1000, 'text': f'原始 {i}',
             'speaker': '说话人 1', 'page_number': i // 10 + 1} for i in range(count)]
    return {'job_id': 'mStable', 'kind': kind, 'status': 'succeeded', 'revision': 2,
            'inputs': [{'original_name': '录音.wav'}], 'result': {field: rows, 'output_files': {}}, 'review': None}


@pytest.mark.parametrize('kind', ['audio', 'seal'])
def test_cross_page_identity_and_legacy_edits(kind):
    item = example(kind)
    original = copy.deepcopy(item)
    field = 'segments' if kind == 'audio' else 'seals'
    rows = identified_rows(item['job_id'], field, item['result'][field])
    edits = [{'row_id': rows[index]['row_id'], 'text': f'校订 {index}'} for index in (0, 20, 64)]
    item['review'] = {'edits': normalized_edits(item, field, edits)}
    result = reviewed_result(item)
    for index, row in enumerate(result[field]):
        assert row['text'] == (f'校订 {index}' if index in (0, 20, 64) else f'原始 {index}')
    assert item['result'] == original['result']
    assert len({row['row_id'] for row in rows}) == 65
    assert identified_rows(item['job_id'], field, item['result'][field]) == rows
    legacy = normalized_edits(item, field, [{'index': 20, 'text': '旧修订'}])
    assert legacy[0]['row_id'] == rows[20]['row_id']
    for invalid in ([{'row_id': rows[20]['row_id'], 'index': 0, 'text': '错位'}],
                    [{'row_id': 'rMissing', 'text': '不存在'}],
                    [{'index': 0, 'text': 'a'}, {'row_id': rows[0]['row_id'], 'text': 'b'}]):
        with pytest.raises(DataError): normalized_edits(item, field, invalid)


def test_export_streams_media_and_preserves_raw_model_result(tmp_path, monkeypatch):
    item = example()
    row = identified_rows(item['job_id'], 'segments', item['result']['segments'])[20]
    item['review'] = {'edits': [{'row_id': row['row_id'], 'text': '第二页人工校正'}]}
    media = tmp_path / 'playback.wav'
    with media.open('wb') as stream:
        for _ in range(32): stream.write(b'audio' * 1024 * 16)
    raw = tmp_path / 'asr-original.json'; raw.write_text('["未经校正模型文本"]', encoding='utf-8')
    item['result']['output_files'] = {'playback.wav': {}, 'asr-original.json': {}}

    class Service:
        root = tmp_path
        def get(self, workspace, job): return copy.deepcopy(item)
        def output(self, workspace, job, name): return tmp_path / name, 'application/octet-stream'
        def file_hash(self, path):
            with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()

    def forbidden_read(*args, **kwargs): raise AssertionError('media must stream')
    monkeypatch.setattr(Path, 'read_bytes', forbidden_read)
    archive, files = export_files(Service(), 'wTest', 'mStable')
    assert isinstance(archive, Path) and all(isinstance(path, Path) for path in files.values())
    with zipfile.ZipFile(archive) as bundle:
        result = json.loads(bundle.read('result.json'))
        assert result['result']['segments'][20]['text'] == '第二页人工校正'
        assert result['result']['segments'][0]['text'] == '原始 0'
        assert '第二页人工校正' in bundle.read('transcript.srt').decode()
        csv_rows = list(csv.DictReader(io.StringIO(bundle.read('reviewed.csv').decode('utf-8-sig'))))
        assert csv_rows[20]['文字'] == '第二页人工校正'
        assert csv_rows[20]['结果ID'] == row['row_id']
        assert all(exported['结果ID'] == original['row_id'] for exported, original in
                   zip(csv_rows, result['result']['segments']))
        assert '第二页人工校正' not in bundle.read('asr-original.json').decode()
        manifest = json.loads(bundle.read('manifest.json'))
        assert manifest['playback.wav']['bytes'] == media.stat().st_size
        for name, identity in manifest.items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == identity['sha256']
