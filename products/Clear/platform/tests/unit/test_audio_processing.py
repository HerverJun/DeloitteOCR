"""Real codec/stream checkpoints and deterministic boundary tests, no model load."""
import hashlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import sys
import subprocess
import time
import types
import wave

import pytest

MODULE = Path(__file__).resolve().parents[2] / 'modules/manual_multimodal'
sys.path.insert(0, str(MODULE))
import audio_processing as audio
from worker_protocol import WorkerControl, WorkerStopped


class Control:
    def __init__(self, stop_after_commit=False):
        self.commits = []
        self.stopped = False
        self.stop_after_commit = stop_after_commit

    def check(self):
        if self.stopped:
            raise WorkerStopped()

    def committed(self, path, chunk_index, processed_ms, total_ms, event='chunk_committed'):
        self.commits.append((event, processed_ms))
        if self.stop_after_commit:
            self.stopped = True
        self.check()


CODECS = pytest.mark.skipif(any(importlib.util.find_spec(name) is None for name in ('numpy','av','cv2')),
                           reason='bundled audio runtime required for codec tests')


def wav(path, seconds=35, stereo=False, rate=16000):
    import numpy as np
    values = (np.sin(np.arange(seconds * rate) * .03) * 16000).astype('<i2')
    if stereo:
        values = np.stack([values, np.zeros_like(values)], axis=1)
    with wave.open(str(path), 'wb') as target:
        target.setnchannels(2 if stereo else 1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(values.tobytes())


@CODECS
def test_stream_decode_resume_matches_uninterrupted_and_detects_corruption(tmp_path):
    source = tmp_path / 'source.wav'
    wav(source)
    resumed, whole = tmp_path / 'resumed', tmp_path / 'whole'
    resumed.mkdir()
    whole.mkdir()
    with pytest.raises(WorkerStopped):
        audio.decode_pcm(source, resumed, 'identity', None, Control(True), lambda *a, **k: None)
    checkpoint = json.loads((resumed / 'decode.json').read_text('utf-8'))
    assert 30000 <= checkpoint['metadata']['duration_ms'] < 35000
    partial_bytes = (resumed / 'analysis.s16le').stat().st_size
    # Simulate an uncommitted tail left by process termination.
    with (resumed / 'analysis.s16le').open('ab') as output:
        output.write(b'\x00\x00' * 100)
    pcm, metadata, reused = audio.decode_pcm(source, resumed, 'identity', None, Control(), lambda *a, **k: None)
    expected, _, _ = audio.decode_pcm(source, whole, 'identity', None, Control(), lambda *a, **k: None)
    assert not reused and metadata['duration_ms'] == 35000
    assert audio.file_hash(pcm) == audio.file_hash(expected)
    assert pcm.stat().st_size > partial_bytes
    assert audio.decode_pcm(source, resumed, 'identity', None, Control(), lambda *a, **k: None)[2]
    with pytest.raises(ValueError, match='已变化'):
        audio.decode_pcm(source, resumed, 'different', None, Control(), lambda *a, **k: None)
    with pcm.open('r+b') as stream:
        stream.write(b'bad!')
    rebuilt, _, reused = audio.decode_pcm(source, resumed, 'identity', None, Control(), lambda *a, **k: None)
    assert not reused and audio.file_hash(rebuilt) == audio.file_hash(expected)
    preserved = list((resumed / 'quarantine').glob('*/analysis.s16le'))
    assert preserved and preserved[0].read_bytes()[:4] == b'bad!'
    (resumed / 'decode.json').write_text('{broken', encoding='utf-8')
    rebuilt, _, reused = audio.decode_pcm(source, resumed, 'identity', None, Control(), lambda *a, **k: None)
    assert not reused and audio.file_hash(rebuilt) == audio.file_hash(expected)
    assert any(path.read_text('utf-8') == '{broken' for path in (resumed / 'quarantine').glob('*/decode.json'))


@CODECS
def test_original_channel_selection_and_playback(tmp_path):
    import numpy as np
    source = tmp_path / 'stereo.wav'
    wav(source, 1, True, rate=48000)
    selected = tmp_path / 'selected'
    selected.mkdir()
    pcm, metadata, _ = audio.decode_pcm(source, selected, 'id', 1, Control(), lambda *a, **k: None)
    assert np.max(np.abs(np.fromfile(pcm, dtype='<i2'))) == 0
    assert metadata['original_channels'] == 2 and metadata['selected_channel'] == 1
    audio.playback_and_figures(pcm, selected, metadata, Control())
    with wave.open(str(selected / 'playback.wav')) as stream:
        assert stream.getnframes() == 16000
        assert stream.getnchannels() == 1
    assert (selected / 'waveform.png').stat().st_size > 0
    assert (selected / 'spectrogram.png').stat().st_size > 0
    with pytest.raises(ValueError, match='声道不存在'):
        audio.decode_pcm(source, tmp_path, 'different', 2, Control(), lambda *a, **k: None)


def test_four_hour_windows_cover_exactly_with_context():
    windows = list(audio.windows(4 * 3600 * audio.RATE))
    assert len(windows) == 48
    assert windows[0]['start'] == 0
    assert windows[-1]['end'] == 4 * 3600 * audio.RATE
    assert all(left['end'] == right['start'] for left, right in zip(windows, windows[1:]))
    assert windows[1]['context_start'] == (300 - 2) * audio.RATE
    assert windows[1]['context_end'] == (600 + 2) * audio.RATE


def test_cross_boundary_tokens_owned_once_and_ids_stable():
    windows = list(audio.windows(601 * audio.RATE))
    first = [{'text': '甲乙丙丁。', 'sentence_info': [{'start': 299000, 'end': 301000,
        'text': '甲乙丙丁。', 'raw_text': '甲 乙 丙 丁',
        'timestamp': [[299000,299500],[299500,300000],[300000,300500],[300500,301000]]}]}]
    second = [{'text': '甲乙丙丁。', 'sentence_info': [{'start': 1000, 'end': 3000,
        'text': '甲乙丙丁。', 'raw_text': '甲 乙 丙 丁',
        'timestamp': [[1000,1500],[1500,2000],[2000,2500],[2500,3000]]}]}]
    left = audio.owned_segments(first, windows[0], 601000, 'id')
    right = audio.owned_segments(second, windows[1], 601000, 'id')
    assert ''.join(row['text'] for row in left + right) == '甲乙丙丁。'
    assert left[0]['end_ms'] == right[0]['start_ms'] == 300000
    assert left == audio.owned_segments(first, windows[0], 601000, 'id')
    assert left[0]['row_id'] == left[0]['segment_id'] != right[0]['row_id']
    first[0]['sentence_info'][0].pop('timestamp')
    with pytest.raises(ValueError, match='缺少可核验'):
        audio.owned_segments(first, windows[0], 601000, 'id')


@CODECS
def test_duration_and_size_limits_enforced(tmp_path, monkeypatch):
    source = tmp_path / 'long.wav'
    wav(source, 2)
    monkeypatch.setattr(audio, 'MAX_SECONDS', 1)
    with pytest.raises(ValueError, match='四小时'):
        audio.decode_pcm(source, tmp_path, 'id', None, Control(), lambda *a, **k: None)
    monkeypatch.setattr(audio, 'MAX_BYTES', 1)
    with pytest.raises(ValueError, match='4 GiB'):
        audio.decode_pcm(source, tmp_path, 'id', None, Control(), lambda *a, **k: None)


def test_protocol_rejects_stale_epoch_and_stops_on_parent_loss(monkeypatch):
    # Matching commands are the only messages accepted from the JSONL channel.
    monkeypatch.setattr(sys, 'stdin', io.StringIO('\n'.join([
        json.dumps({'command':'stop','worker_epoch':'old'}),
        json.dumps({'command':'start','worker_epoch':'new'}),
        json.dumps({'command':'continue','worker_epoch':'new'})]) + '\n'))
    control = WorkerControl({'runner': {'protocol':'clear-gpu-worker-v1','worker_epoch':'new'}}, lambda *a: None)
    control.wait('start')
    control.wait('continue')
    with pytest.raises(WorkerStopped):
        control.check()


@CODECS
def test_mocked_pipeline_reuses_committed_chunks_and_checks_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, 'CORE_SECONDS', 1)
    source = tmp_path / 'input.wav'
    wav(source, 2)
    models = tmp_path / 'models/audio'
    models.mkdir(parents=True)
    (models / 'model.pt').write_bytes(b'local bundled weights')
    out = tmp_path / 'out'
    out.mkdir()
    checkpoint = tmp_path / 'checkpoints'
    loads = []
    class Model:
        def parameters(self):
            return iter([types.SimpleNamespace(device='cpu')])
    class AutoModel:
        def __init__(self, **options):
            loads.append(options)
            self.model = self.vad_model = self.punc_model = Model()
        def generate(self, **options):
            assert options['pred_timestamp'] is True
            assert options['sentence_timestamp'] is True and options['return_raw_text'] is True
            return [{'text':'实际时间', 'sentence_info':[{'start':0,'end':2000,'text':'实际时间。',
                'raw_text':'实 际 时 间','timestamp':[[0,500],[500,1000],[1000,1500],[1500,2000]]}]}]
    monkeypatch.setitem(sys.modules, 'funasr', types.SimpleNamespace(AutoModel=AutoModel))
    monkeypatch.setitem(sys.modules, 'torch', types.SimpleNamespace(cuda=types.SimpleNamespace(is_initialized=lambda: False)))
    monkeypatch.setattr(audio, 'speaker_features', lambda *a: [])
    monkeypatch.setattr(audio, 'assign_speakers', lambda *a: {'embedding_count':0,'speaker_count':0,'device':'cpu'})
    monkeypatch.setattr(audio.importlib.metadata, 'version', lambda name: 'test-version')
    request = {'inputs':[{'path':str(source),'blob_hash':audio.file_hash(source)}],
        'models':str(tmp_path / 'models'), 'options':{'device':'cpu','channel':None},
        'runner':{'checkpoint_root':str(checkpoint)}}
    first = audio.run_audio(request, out, Control(), lambda *a, **k: None)
    assert first['performance']['chunks_reused'] == 0 and len(loads) == 2
    second = audio.run_audio(request, out, Control(), lambda *a, **k: None)
    assert second['performance']['chunks_reused'] == 2 and len(loads) == 2
    assert first['segments'] == second['segments']
    request['options']['device'] = 'cuda'
    switched = audio.run_audio(request, out, Control(), lambda *a, **k: None)
    assert switched['performance']['chunks_reused'] == 2 and len(loads) == 2
    assert switched['execution']['chunk_devices']['0']['asr'] == 'cpu'
    assert switched['execution']['requested_device'] == 'cuda'
    assert switched['execution']['fresh_inference_chunks'] == 0
    assert switched['execution']['device_execution'] == 'verified_checkpoint_reuse_only'
    chunk = checkpoint / 'chunk-00000.json'
    value = json.loads(chunk.read_text('utf-8'))
    value['payload']['segments'][0]['text'] = 'tampered'
    audio.atomic_json(chunk, value)
    request['options']['device'] = 'cpu'
    recovered = audio.run_audio(request, out, Control(), lambda *a, **k: None)
    assert recovered['performance']['chunks_reused'] == 1 and len(loads) == 4
    assert recovered['execution']['fresh_inference_chunks'] == 1
    assert recovered['segments'] == first['segments']
    assert any('tampered' in path.read_text('utf-8') for path in (checkpoint / 'quarantine').glob('*/chunk-00000.json'))
    # Rebuilding a damaged PCM decode must retain valid ASR checkpoints.
    with (checkpoint / 'analysis.s16le').open('r+b') as stream:
        stream.write(b'bad!')
    rebuilt = audio.run_audio(request, out, Control(), lambda *a, **k: None)
    assert rebuilt['performance']['chunks_reused'] == 2 and len(loads) == 4
    assert not rebuilt['performance']['decode_reused']
    assert rebuilt['segments'] == first['segments']
    chunk.write_text('{broken', encoding='utf-8')
    recovered = audio.run_audio(request, out, Control(), lambda *a, **k: None)
    assert recovered['performance']['chunks_reused'] == 1 and len(loads) == 6
    assert recovered['segments'] == first['segments']


@CODECS
def test_captured_paraformer_native_tokens_cross_real_five_minute_pcm_split(tmp_path,monkeypatch):
    """Replay real malformed sentence_info with aligned record tokens; no model load."""
    stamps = [[295620,295860],[295860,296039],[296039,296219],[296219,296400],
        [296400,296580],[296580,296760],[296760,296880],[296880,297119],[297119,297300],
        [297300,297420],[297420,297599],[297599,297840],[297840,297960],[297960,298200],
        [298200,298440],[298440,298559],[298559,298740],[298740,298920],[298920,299099],
        [299099,299219],[299219,299460],[299460,299700],[299700,300030],[300339,300520],
        [300520,300640],[300640,300760],[300760,300880],[300880,301000],[301000,301180],
        [301180,301300],[301300,301420],[301420,301599],[301599,302050]]
    text = '正是因为存在绝对正义，所以我们接受现实的相对正义。但是不要因为现实的相。'
    native = {'text':text,'timestamp':stamps,
        'raw_text':'正 是 因 为 存 在 绝 对 正 义 所 以 我 们 接 受 现 实 的 相 对 正 义 但 是 不 要 因 为 现 实 的 相',
        'sentence_info':[
            {'text':'正是因为存在绝对正义，','raw_text':'正是因为存在绝对正义',
             'start':295620,'end':297420,'timestamp':stamps[:10]},
            {'text':'所以我们接受现实的相对正义但。','raw_text':'所以我们接受现实的相对正义但',
             'start':297420,'end':300030,'timestamp':stamps[10:23]},
            {'text':'是不要因为现实的相。','raw_text':'是不要因为现实的相',
             'start':300339,'end':302050,'timestamp':stamps[23:]}]}
    assert len(native['sentence_info'][1]['raw_text']) != len(native['sentence_info'][1]['timestamp'])
    right = {'text':'接受现实的相对正义。但是不要因为现实的相。',
        'raw_text':'接 受 现 实 的 相 对 正 义 但 是 不 要 因 为 现 实 的 相',
        'timestamp':[[start-298000,end-298000] for start,end in stamps[14:]], 'sentence_info':[]}
    source = tmp_path / 'six-minutes.wav'
    wav(source,360)
    models = tmp_path / 'models/audio'
    models.mkdir(parents=True)
    (models / 'model.pt').write_bytes(b'captured-format-test-weights')
    out = tmp_path / 'out'
    out.mkdir()
    calls = []
    class Model:
        def parameters(self):
            return iter([types.SimpleNamespace(device='cpu')])
    class AutoModel:
        def __init__(self,**options):
            self.model = self.vad_model = self.punc_model = Model()
        def generate(self,**options):
            assert options['pred_timestamp'] is True
            assert options['sentence_timestamp'] is True
            assert options['return_raw_text'] is True
            assert options['return_spk_res'] is False
            index = len(calls)
            calls.append(len(options['input']))
            return [copy.deepcopy(native if index==0 else right)]
    monkeypatch.setitem(sys.modules,'funasr',types.SimpleNamespace(AutoModel=AutoModel))
    monkeypatch.setitem(sys.modules,'torch',types.SimpleNamespace(cuda=types.SimpleNamespace(is_initialized=lambda:False)))
    monkeypatch.setattr(audio,'speaker_features',lambda *a:[])
    monkeypatch.setattr(audio,'assign_speakers',lambda *a:{'device':'cpu','speaker_count':0,'embedding_count':0})
    monkeypatch.setattr(audio.importlib.metadata,'version',lambda name:'test-version')
    request = {'inputs':[{'path':str(source),'blob_hash':audio.file_hash(source)}],
        'models':str(tmp_path/'models'),'options':{'device':'cpu','channel':None},
        'runner':{'checkpoint_root':str(tmp_path/'checkpoints')}}
    result = audio.run_audio(request,out,Control(),lambda *a,**k:None)
    assert calls == [302 * audio.RATE,62 * audio.RATE]
    assert ''.join(row['text'] for row in result['segments']) == text
    assert all(row['alignment_source']=='record_native_token_timestamps' for row in result['segments'])
    assert len({row['row_id'] for row in result['segments']}) == len(result['segments'])
    assert any(row['start_ms']>=300000 for row in result['segments'])
    assert all(295000<=row['start_ms']<row['end_ms']<=303000 for row in result['segments'])
    assert result['performance']['total_chunks']==2
    original = json.loads((out/'asr-original.json').read_text('utf-8'))
    assert original[0]['records'][0] == native
    assert original[1]['records'][0] == right


@pytest.mark.parametrize('left_times,right_times,count,kind', [
    ((299850,300050),(299950,300150),1,'duplicate_token_removed'),
    ((299950,300150),(299850,300050),1,'boundary_token_recovered'),
    ((299750,299950),(300050,300250),2,None),
    ((299850,299950),(300000,300100),2,'uncertain_boundary_repetition_preserved'),
])
def test_boundary_timestamp_jitter_and_real_repeated_speech(left_times,right_times,count,kind):
    windows = list(audio.windows(601 * audio.RATE))
    saved = []
    for window, (start,end) in zip(windows[:2], (left_times,right_times)):
        offset = window['context_start'] // 16
        records = [{'text':'乙', 'sentence_info':[{'start':start-offset,'end':end-offset,
                    'text':'乙','raw_text':'乙','timestamp':[[start-offset,end-offset]]}]}]
        saved.append({'window':window,'payload':{
            'segments':audio.owned_segments(records,window,601000,'id'),
            'boundary_tokens':audio.boundary_tokens(records,window,601000,'id')}})
    segments, alignment = audio.reconcile_boundaries(saved,601000,'id')
    assert ''.join(row['text'] for row in segments) == '乙' * count
    if kind:
        assert kind in [event['kind'] for event in alignment['events']]
    else:
        assert not alignment['events']
    assert alignment['needs_review'] == (kind in {'boundary_token_recovered','uncertain_boundary_repetition_preserved'})


@pytest.mark.parametrize('case,expected_text,event_kind', [
    ('context_only','','unmatched_unowned_boundary_token'),
    ('different_words','乙已','overlapping_boundary_alternatives_preserved'),
])
def test_unmatched_boundary_hypotheses_are_flagged_without_inventing_or_removing_text(case,expected_text,event_kind):
    windows = list(audio.windows(601 * audio.RATE))[:2]
    saved = []
    for index, window in enumerate(windows):
        offset = window['context_start'] // 16
        if case=='context_only':
            rows = [] if index else [{'start':299950-offset,'end':300150-offset,
                    'text':'乙','raw_text':'乙','timestamp':[[299950-offset,300150-offset]]}]
        else:
            start,end = (299850,300050) if not index else (299950,300150)
            text = '乙' if not index else '已'
            rows = [{'start':start-offset,'end':end-offset,'text':text,'raw_text':text,
                     'timestamp':[[start-offset,end-offset]]}]
        records = [{'text':''.join(row['text'] for row in rows),'sentence_info':rows}]
        saved.append({'window':window,'payload':{
            'segments':audio.owned_segments(records,window,601000,'id'),
            'boundary_tokens':audio.boundary_tokens(records,window,601000,'id'),
            'asr_original':copy.deepcopy(records)}})
    original = copy.deepcopy(saved)
    segments, alignment = audio.reconcile_boundaries(saved,601000,'id')
    assert ''.join(row['text'] for row in segments)==expected_text
    assert alignment['needs_review']
    assert event_kind in {event['kind'] for event in alignment['events']}
    assert saved==original


@pytest.mark.skipif(importlib.util.find_spec('numpy') is None, reason='numpy required')
def test_global_speaker_names_are_shared_across_chunks(monkeypatch):
    import numpy as np
    monkeypatch.setitem(sys.modules, 'torch', types.SimpleNamespace(from_numpy=lambda value: value,
                                                                  manual_seed=lambda value: None))
    class ClusterBackend:
        def __call__(self, vectors):
            return np.asarray([9 if vector[0] > 0 else 4 for vector in vectors])
    monkeypatch.setitem(sys.modules, 'funasr.models.campplus.cluster_backend',
                       types.SimpleNamespace(ClusterBackend=ClusterBackend))
    segments = [{'segment_id': 'first', 'speaker':'未分配'},
                {'segment_id': 'other', 'speaker':'未分配'},
                {'segment_id': 'later', 'speaker':'未分配'}]
    features = [{'segment_id':'first','start_ms':0,'end_ms':1000,'embedding':[1,0]},
                {'segment_id':'other','start_ms':2000,'end_ms':3000,'embedding':[-1,0]},
                {'segment_id':'later','start_ms':305000,'end_ms':306000,'embedding':[1,0]}]
    metadata = audio.assign_speakers(segments, features, Control())
    assert [row['speaker'] for row in segments] == ['说话人 1','说话人 2','说话人 1']
    assert metadata['speaker_count'] == 2 and metadata['device'] == 'cpu'


@pytest.mark.skipif(importlib.util.find_spec('numpy') is None, reason='numpy required')
def test_large_speaker_pool_is_bounded_and_covers_short_appearances(monkeypatch):
    import numpy as np
    received = []
    monkeypatch.setitem(sys.modules, 'torch', types.SimpleNamespace(from_numpy=lambda value: value,
                                                                  manual_seed=lambda value: None))
    class ClusterBackend:
        def __call__(self, vectors):
            received.append(len(vectors))
            return np.asarray([int(vector[1] > vector[0]) for vector in vectors])
    monkeypatch.setitem(sys.modules, 'funasr.models.campplus.cluster_backend',
                       types.SimpleNamespace(ClusterBackend=ClusterBackend))
    features, segments = [], []
    for index in range(20000):
        identifier = f'row{index}'
        segments.append({'segment_id':identifier,'speaker':'未分配'})
        # Very short new voice appearances between timeline-uniform samples.
        feature = [0,1] if index in (311, 7001, 13903, 19007) else [1,0]
        features.append({'segment_id':identifier,'start_ms':index * 720,'end_ms':index * 720 + 700,
                         'embedding':feature})
    metadata = audio.assign_speakers(segments, features, Control())
    assert received and received[0] <= 1200
    assert metadata['embedding_count'] == 20000 and metadata['sampling_limit'] == 1200
    assert all(segments[index]['speaker'] == '说话人 2' for index in (311,7001,13903,19007))
    assert segments[0]['speaker'] == segments[19999]['speaker'] == '说话人 1'


def test_embedded_worker_imports_siblings_and_stops_before_model_load(tmp_path):
    worker = MODULE / 'worker.py'
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'kind':'audio','runner':{
        'protocol':'clear-gpu-worker-v1','worker_epoch':'embedded-test'}}), encoding='utf-8')
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    with (tmp_path / 'worker.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-B', str(worker), str(request)],
            cwd=tmp_path, env=env, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
        try:
            event = tmp_path / 'runner-event.json'
            deadline = time.monotonic() + 10
            while not event.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.025)
            assert event.exists(), (tmp_path / 'worker.log').read_text('utf-8')
            assert json.loads(event.read_text('utf-8'))['event'] == 'ready'
            process.stdin.write((json.dumps({'command':'stop','worker_epoch':'embedded-test'}) + '\n').encode())
            process.stdin.flush()
            assert process.wait(timeout=10) == 75
            assert json.loads(event.read_text('utf-8'))['event'] == 'yielded'
            assert not (tmp_path / 'result.json').exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
    probe = 'import runpy; runpy.run_path(' + repr(str(worker)) + '); import audio_processing, worker_protocol; print("siblings-ok")'
    completed = subprocess.run([sys.executable, '-B', '-c', probe], cwd=tmp_path,
                               env=env, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == 'siblings-ok'
