"""Bounded-memory, resumable four-hour audio processing using bundled FunASR."""
import hashlib
import copy
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
import wave

RATE = 16000
MAX_SECONDS = 4 * 3600
MAX_BYTES = 4 * 1024 ** 3
CORE_SECONDS = 300
CONTEXT_SECONDS = 2
FORMAT = 'clear-long-audio-v1'


class CheckpointIdentityMismatch(ValueError):
    pass


class CheckpointCorruption(ValueError):
    pass


def file_hash(path, control=None):
    with Path(path).open('rb') as stream:
        if control is None:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
        hasher = hashlib.sha256()
        while True:
            control.check()
            content = stream.read(4 * 1024 ** 2)
            if not content:
                return hasher.hexdigest()
            hasher.update(content)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.05 * (attempt + 1))


def local_directory(path):
    path = Path(path).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('Audio outputs/checkpoints must be on F:')
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('Audio output/checkpoint path must not contain links')
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_checkpoint(path, identity):
    try:
        value = json.loads(Path(path).read_text('utf-8'))
    except (ValueError, OSError) as error:
        raise CheckpointCorruption('音频检查点 JSON 无法读取') from error
    if not isinstance(value, dict) or not value.get('format') or not value.get('identity'):
        raise CheckpointCorruption('音频检查点缺少格式或身份')
    if value['format'] != FORMAT or value['identity'] != identity:
        raise CheckpointIdentityMismatch('音频检查点的原件、模型或参数已变化，不能继续旧结果')
    return value


def quarantine(folder, paths, reason):
    """Preserve damaged files by same-volume rename before rebuilding them."""
    destination = local_directory(folder / 'quarantine' / (str(time.time_ns()) + '-' + uuid.uuid4().hex[:8]))
    preserved = []
    for path in paths:
        if path.exists():
            if not path.absolute().is_relative_to(folder.absolute()):
                raise ValueError('损坏检查点隔离路径越界')
            os.replace(path, destination / path.name)
            preserved.append(path.name)
    atomic_json(destination / 'reason.json', {'reason': str(reason), 'files': preserved,
                                             'quarantined_at': time.time()})
    return destination


def resources(torch=None):
    result = {}
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                    'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            result.update(working_set_bytes=counters.WorkingSetSize,
                          peak_working_set_bytes=counters.PeakWorkingSetSize,
                          peak_commit_bytes=counters.PeakPagefileUsage)
    if torch is not None and torch.cuda.is_initialized():
        result.update(peak_cuda_allocated_bytes=torch.cuda.max_memory_allocated(),
                      peak_cuda_reserved_bytes=torch.cuda.max_memory_reserved())
    return result


def decode_pcm(source, folder, identity, channel, control, progress):
    """Append resampled int16 PCM and commit every 30 seconds; seek on resume."""
    import av
    import numpy as np
    source = Path(source)
    if source.stat().st_size > MAX_BYTES:
        raise ValueError('音频原件超过 4 GiB 上限')
    checkpoint = folder / 'decode.json'
    pcm = folder / 'analysis.s16le'
    previous = None
    committed = 0
    hasher = hashlib.sha256()
    if checkpoint.exists():
        try:
            previous = validate_checkpoint(checkpoint, identity)
            expected_state = previous.get('state_sha256')
            state = {key: value for key, value in previous.items() if key != 'state_sha256'}
            if fingerprint(state) != expected_state:
                raise CheckpointCorruption('音频解码检查点元数据校验失败')
            committed = previous['samples']
            if type(committed) is not int or not 0 < committed <= RATE * MAX_SECONDS:
                raise CheckpointCorruption('音频解码检查点样本数量无效')
            if not pcm.is_file() or pcm.stat().st_size < committed * 2:
                raise CheckpointCorruption('音频解码检查点文件缺失或长度不足')
            with pcm.open('rb') as stream:
                remaining = committed * 2
                while remaining:
                    content = stream.read(min(4 * 1024 ** 2, remaining))
                    hasher.update(content)
                    remaining -= len(content)
                    control.check()
            if hasher.hexdigest() != previous['pcm_sha256']:
                raise CheckpointCorruption('音频解码检查点内容校验失败')
            if previous['complete']:
                return pcm, previous['metadata'], True
        except CheckpointIdentityMismatch:
            raise
        except (CheckpointCorruption, KeyError, TypeError, OSError) as error:
            quarantine(folder, [checkpoint, pcm], error)
            previous, committed, hasher = None, 0, hashlib.sha256()
    count = committed
    origin = previous['origin'] if previous else None
    timeline = previous['timeline'] if previous else []
    last_commit = count
    with av.open(str(source)) as container, pcm.open('r+b' if pcm.exists() else 'w+b') as target:
        stream = next((s for s in container.streams if s.type == 'audio'), None)
        if stream is None:
            raise ValueError('文件不包含音轨')
        original_rate = stream.codec_context.sample_rate
        channels = stream.codec_context.channels
        if channel is not None and (type(channel) is not int or not 0 <= channel < channels):
            raise ValueError('所选原音声道不存在（声道编号从 0 开始）')
        target.truncate(committed * 2)
        target.seek(committed * 2)
        resampler = av.audio.resampler.AudioResampler(format='s16', layout='mono', rate=RATE)
        selector = av.audio.resampler.AudioResampler(format='fltp', layout=stream.codec_context.layout,
                                                    rate=original_rate) if channel is not None else None
        seeking = False
        if committed and origin is not None and previous.get('timestamps_available'):
            try:
                container.seek(int((origin + max(0, committed / RATE - 5)) * av.time_base),
                               backward=True, any_frame=False)
                seeking = True
            except (av.error.FFmpegError, ValueError):
                container.seek(0)
        decoded_position = 0
        timestamps_available = True
        def metadata():
            return {'original_sample_rate': original_rate, 'original_channels': channels,
                    'analysis_sample_rate': RATE, 'duration_ms': count // 16, 'time_offset_ms': 0,
                    'selected_channel': channel, 'playback': 'playback.wav',
                    'transform': {'time_origin': 'first_decoded_audio_sample', 'first_pts_seconds': origin,
                        'scale': 1, 'offset_ms': 0,
                        'channel_mapping': 'all_original_channels_downmixed' if channel is None else f'original_channel_{channel}',
                        'discontinuities': timeline},
                    'processing': ['流式解码为 16kHz 单声道 PCM；保留原音时间间隔',
                                   '混合全部原声道' if channel is None else f'仅分析原声道 {channel}']}
        def commit(complete=False):
            nonlocal last_commit
            target.flush()
            os.fsync(target.fileno())
            value = {'format': FORMAT, 'identity': identity, 'samples': count, 'origin': origin,
                     'timeline': timeline, 'pcm_sha256': hasher.hexdigest(), 'complete': complete,
                     'timestamps_available': timestamps_available, 'metadata': metadata()}
            value['state_sha256'] = fingerprint(value)
            atomic_json(checkpoint, value)
            last_commit = count
            progress('音频解码', processed_ms=count // 16,
                     total_ms=round(container.duration / 1000) if container.duration else None)
            control.committed(checkpoint, -1, count // 16, metadata()['duration_ms'], 'decode_committed')
        def write(content):
            nonlocal count
            raw = content.astype('<i2', copy=False).tobytes()
            target.write(raw)
            hasher.update(raw)
            count += len(content)
        def append(output):
            nonlocal origin, decoded_position, timestamps_available
            control.check()
            samples = output.to_ndarray().reshape(-1)
            timestamp = float(output.pts * output.time_base) if output.pts is not None and output.time_base else None
            timestamps_available = timestamps_available and timestamp is not None
            if origin is None:
                origin = timestamp if timestamp is not None else 0.0
            if timestamp is None and seeking:
                raise ValueError('恢复解码后缺少时间戳；请建立新任务重新解码')
            position = round((timestamp - origin) * RATE) if timestamp is not None else decoded_position
            decoded_position = position + len(samples)
            if max(position + len(samples), count) > RATE * MAX_SECONDS:
                raise ValueError('音频超过四小时上限')
            # Samples already committed belong to a previous decode pass.
            if position < committed:
                skipped = min(len(samples), committed - position)
                samples = samples[skipped:]
                position += skipped
                if not len(samples):
                    return
            gap = position - count
            if gap > 2:
                timeline.append({'at_ms': position / 16, 'gap_ms': gap / 16,
                                 'handling': 'preserved_as_silence'})
                while gap:
                    control.check()
                    length = min(gap, RATE * 10)
                    write(np.zeros(length, dtype=np.int16))
                    gap -= length
            elif gap < -2:
                removed = min(len(samples), -gap)
                samples = samples[removed:]
                timeline.append({'at_ms': count / 16, 'overlap_ms': removed / 16,
                                 'handling': 'trimmed_duplicate_timestamps'})
            write(samples)
            if count - last_commit >= RATE * 30:
                commit()
        def outputs(frame):
            if selector is None:
                yield frame
            else:
                for planar in selector.resample(frame):
                    selected = av.AudioFrame.from_ndarray(planar.to_ndarray()[channel:channel + 1].copy(),
                                                          format='fltp', layout='mono')
                    selected.sample_rate = planar.sample_rate
                    selected.pts, selected.time_base = planar.pts, planar.time_base
                    yield selected
        for frame in container.decode(stream):
            for selected in outputs(frame):
                for output in resampler.resample(selected):
                    append(output)
        if selector is not None:
            for selected in outputs(None):
                for output in resampler.resample(selected):
                    append(output)
        for output in resampler.resample(None):
            append(output)
        if not count:
            raise ValueError('音轨为空')
        commit(True)
    return pcm, metadata(), False


def playback_and_figures(pcm, out, metadata, control):
    import cv2
    import numpy as np
    temporary = out / 'playback.wav.new'
    with wave.open(str(temporary), 'wb') as target, pcm.open('rb') as source:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(RATE)
        while True:
            control.check()
            content = source.read(1024 * 1024)
            if not content:
                break
            target.writeframesraw(content)
    os.replace(temporary, out / 'playback.wav')
    samples = np.memmap(pcm, mode='r', dtype='<i2')
    try:
        width, height = 1000, 180
        canvas = np.full((height, width, 3), 250, dtype=np.uint8)
        boundaries = np.linspace(0, len(samples), width + 1, dtype=int)
        for x in range(width):
            control.check()
            values = samples[boundaries[x]:max(boundaries[x] + 1, boundaries[x + 1])]
            if len(values):
                low, high = float(values.min()) / 32768, float(values.max()) / 32768
                cv2.line(canvas, (x, int(height / 2 - min(.98, high) * height * .46)),
                         (x, int(height / 2 - max(-.98, low) * height * .46)), (70, 130, 45), 1)
        starts = np.linspace(0, max(0, len(samples) - 512), width, dtype=int)
        windows = np.stack([np.pad(samples[i:i + 512].astype(np.float32) / 32768,
                                     (0, max(0, 512 - len(samples[i:i + 512])))) for i in starts])
        spectrum = 20 * np.log10(np.maximum(np.abs(np.fft.rfft(windows * np.hanning(512), axis=1)), 1e-6)).T
        normalized = np.clip((spectrum + 70) / 90 * 255, 0, 255).astype('uint8')[::-1]
        images = {'waveform.png': canvas, 'spectrogram.png': cv2.applyColorMap(
            cv2.resize(normalized, (width, 256)), cv2.COLORMAP_VIRIDIS)}
        for name, image in images.items():
            ok, content = cv2.imencode('.png', image)
            if not ok:
                raise ValueError('音频图像编码失败')
            content.tofile(out / name)
    finally:
        samples._mmap.close()


def windows(total_samples):
    core = CORE_SECONDS * RATE
    context = CONTEXT_SECONDS * RATE
    for index, start in enumerate(range(0, total_samples, core)):
        end = min(total_samples, start + core)
        yield {'index': index, 'start': start, 'end': end,
               'context_start': max(0, start - context), 'context_end': min(total_samples, end + context)}


def text_spans(row):
    """Align existing model timestamp units; never manufacture word timings."""
    text = str(row.get('text', ''))
    timestamps = row.get('timestamp') or []
    tokens = str(row.get('raw_text', '')).split()
    for candidates in (tokens, re.findall(r'[\u3400-\u9fff]|[A-Za-z0-9]+(?:[\'-][A-Za-z0-9]+)*|[^\W_]', text)):
        if len(candidates) != len(timestamps):
            continue
        spans, cursor = [], 0
        for token in candidates:
            position = text.casefold().find(token.casefold(), cursor)
            if position < 0:
                break
            spans.append((position, position + len(token)))
            cursor = position + len(token)
        if len(spans) == len(timestamps):
            return text, timestamps, spans
    return text, timestamps, None


def aligned_sentence_rows(record):
    """Prefer aligned record tokens over potentially shifted FunASR sentences.

    The bundled FunASR punctuation adapter can attach the first character of the
    next sentence to the previous sentence without moving its timestamp. Its
    record-level spaced raw_text and native timestamps remain aligned. Rebuild
    readable sentences from those units; preserve original records separately.
    """
    text, stamps, spans = text_spans(record)
    if spans is None or not spans:
        return record.get('sentence_info', [])
    rows = []
    first = 0
    for index, span in enumerate(spans):
        end = spans[index + 1][0] if index + 1 < len(spans) else len(text)
        piece = text[span[0]:end]
        if index + 1 < len(spans) and not re.search(r'[，。！？；,.!?;]', piece):
            continue
        group = stamps[first:index + 1]
        if not all(isinstance(stamp, (list, tuple)) and len(stamp) >= 2 for stamp in group):
            return record.get('sentence_info', [])
        rows.append({'text':text[spans[first][0]:end].strip(),
                     'raw_text':' '.join(text[start:stop] for start,stop in spans[first:index + 1]),
                     'start':group[0][0],'end':group[-1][1], 'timestamp':group,
                     'alignment_source':'record_native_token_timestamps'})
        first = index + 1
    return rows


def row_tokens(row, window, identity, row_index, duration_ms):
    text, stamps, spans = text_spans(row)
    if spans is None:
        return []
    offset = window['context_start'] // 16
    result = []
    for index, (stamp, span) in enumerate(zip(stamps, spans)):
        if not stamp or len(stamp) < 2:
            continue
        start, end = max(0, offset + int(stamp[0])), min(duration_ms, offset + int(stamp[1]))
        if start >= end:
            continue
        piece = text[span[0]:spans[index + 1][0] if index + 1 < len(spans) else len(text)]
        result.append({'token_id': 'tok' + fingerprint([identity, window['index'], row_index, index, start, end])[:32],
                       'start_ms': start, 'end_ms': end, 'text': piece, 'chunk_index': window['index']})
    return result


def boundary_tokens(records, window, duration_ms, identity):
    rows = [row for record in records for row in aligned_sentence_rows(record)]
    edges = [window['start'] // 16, window['end'] // 16]
    return [token for index, row in enumerate(rows)
            for token in row_tokens(row, window, identity, index, duration_ms)
            if any(abs((token['start_ms'] + token['end_ms']) / 2 - edge) <= CONTEXT_SECONDS * 1000
                   for edge in edges)]


def owned_segments(records, window, duration_ms, identity):
    offset = window['context_start'] // 16
    core_start, core_end = window['start'] // 16, window['end'] // 16
    segments = []
    row_index = -1
    for record in records:
        for row in aligned_sentence_rows(record):
            row_index += 1
            start, end = max(0, int(row['start']) + offset), min(duration_ms, int(row['end']) + offset)
            if start >= end or end <= core_start or start >= core_end:
                continue
            text = str(row.get('text', ''))
            tokens = row_tokens(row, window, identity, row_index, duration_ms)
            selected_tokens = [token for token in tokens if core_start <=
                               (token['start_ms'] + token['end_ms']) / 2 < core_end]
            if start < core_start or end > core_end:
                text, stamps, spans = text_spans(row)
                if spans is None:
                    raise ValueError('跨块句段缺少可核验的文字时间对齐，不能静默截断或重复转写')
                selected = [i for i, stamp in enumerate(stamps) if stamp and
                            core_start <= offset + (stamp[0] + stamp[1]) / 2 < core_end]
                if not selected:
                    continue
                first, last = selected[0], selected[-1]
                text = text[spans[first][0]:spans[last + 1][0] if last + 1 < len(spans) else len(text)].strip()
                start, end = max(core_start, offset + int(stamps[first][0])), min(core_end, offset + int(stamps[last][1]))
            if start >= end or not text.strip():
                continue
            segment = {'segment_id': 'seg' + fingerprint([identity, window['index'], len(segments), start, end])[:32],
                       'start_ms': start, 'end_ms': end, 'text': text, 'speaker': '未分配',
                       'chunk_index': window['index']}
            if row.get('alignment_source'):
                segment['alignment_source'] = row['alignment_source']
            segment['row_id'] = segment['segment_id']
            segment['_tokens'] = selected_tokens
            segments.append(segment)
    if not segments and any(record.get('text') for record in records):
        if not any(aligned_sentence_rows(record) for record in records):
            raise ValueError('模型返回文字但没有可定位句段，不能伪造时间戳')
    return sorted(segments, key=lambda value: (value['start_ms'], value['end_ms']))


def reconcile_boundaries(saved, duration_ms, identity):
    """Align only equal, temporally overlapping tokens from adjacent contexts.

    An ordered dynamic alignment distinguishes repeated words at different times.
    Uncertain repetitions stay in the transcript with a review event. Original
    model records and the unmodified chunk checkpoints remain available.
    """
    segments = copy.deepcopy([row for value in saved for row in value['payload']['segments']])
    events = []
    removals = set()
    existing = {token['token_id'] for row in segments for token in row.get('_tokens', [])}
    def normalized(token):
        return re.sub(r'[\W_]+', '', token['text'], flags=re.UNICODE).casefold()
    def score(left, right):
        if not normalized(left) or normalized(left) != normalized(right):
            return 0
        overlap = min(left['end_ms'], right['end_ms']) - max(left['start_ms'], right['start_ms'])
        shortest = min(left['end_ms'] - left['start_ms'], right['end_ms'] - right['start_ms'])
        difference = abs((left['start_ms'] + left['end_ms'] - right['start_ms'] - right['end_ms']) / 2)
        if overlap <= 0 or overlap / shortest < .5 or difference > 250:
            return 0
        return 2 + overlap / shortest - difference / 1000
    for left_chunk, right_chunk in zip(saved, saved[1:]):
        if left_chunk['window']['end'] != right_chunk['window']['start']:
            continue
        boundary = left_chunk['window']['end'] // 16
        left = [token for token in left_chunk['payload'].get('boundary_tokens', [])
                if abs((token['start_ms'] + token['end_ms']) / 2 - boundary) <= CONTEXT_SECONDS * 1000]
        right = [token for token in right_chunk['payload'].get('boundary_tokens', [])
                 if abs((token['start_ms'] + token['end_ms']) / 2 - boundary) <= CONTEXT_SECONDS * 1000]
        # Context contains only a few seconds. Bound malformed model output rather
        # than allocate an unbounded alignment matrix.
        if len(left) > 256 or len(right) > 256:
            events.append({'kind':'alignment_capacity_exceeded','boundary_ms':boundary,'needs_review':True})
            continue
        table = [[0.0] * (len(right) + 1) for _ in range(len(left) + 1)]
        for i in range(len(left) - 1, -1, -1):
            for j in range(len(right) - 1, -1, -1):
                match = score(left[i], right[j])
                table[i][j] = max(table[i + 1][j], table[i][j + 1],
                                  match + table[i + 1][j + 1] if match else 0)
        i = j = 0
        matched_left, matched_right = set(), set()
        while i < len(left) and j < len(right):
            match = score(left[i], right[j])
            if match and abs(table[i][j] - match - table[i + 1][j + 1]) < 1e-6:
                a, b = left[i], right[j]
                matched_left.add(i)
                matched_right.add(j)
                a_owned = a['token_id'] in existing and a['token_id'] not in removals
                b_owned = b['token_id'] in existing and b['token_id'] not in removals
                if a_owned and b_owned:
                    removals.add(b['token_id'])
                    events.append({'kind':'duplicate_token_removed','boundary_ms':boundary,
                        'kept_token_id':a['token_id'],'removed_token_id':b['token_id'],
                        'text':a['text'],'needs_review':False})
                elif not a_owned and not b_owned:
                    identifier = 'seg' + fingerprint([identity, boundary, a['token_id'], b['token_id'], 'boundary-recovered'])[:32]
                    segments.append({'segment_id':identifier,'row_id':identifier,'start_ms':a['start_ms'],
                        'end_ms':a['end_ms'],'text':a['text'].strip(),'speaker':'未分配',
                        'chunk_index':a['chunk_index'],'_tokens':[copy.deepcopy(a)],'boundary_recovered':True})
                    existing.add(a['token_id'])
                    events.append({'kind':'boundary_token_recovered','boundary_ms':boundary,
                        'kept_token_id':a['token_id'],'other_token_id':b['token_id'],
                        'text':a['text'],'needs_review':True})
                i += 1
                j += 1
            elif table[i + 1][j] >= table[i][j + 1]:
                i += 1
            else:
                j += 1
        for i, a in enumerate(left):
            if i in matched_left:
                continue
            for j, b in enumerate(right):
                if j in matched_right:
                    continue
                difference = abs((a['start_ms'] + a['end_ms'] - b['start_ms'] - b['end_ms']) / 2)
                if difference > 250 or a['token_id'] not in existing or b['token_id'] not in existing:
                    continue
                if normalized(a) == normalized(b):
                    events.append({'kind':'uncertain_boundary_repetition_preserved','boundary_ms':boundary,
                        'left_token_id':a['token_id'],'right_token_id':b['token_id'],
                        'text':a['text'],'needs_review':True})
                else:
                    overlap = min(a['end_ms'],b['end_ms']) - max(a['start_ms'],b['start_ms'])
                    shortest = min(a['end_ms']-a['start_ms'],b['end_ms']-b['start_ms'])
                    if overlap > 0 and overlap / shortest >= .5:
                        events.append({'kind':'overlapping_boundary_alternatives_preserved','boundary_ms':boundary,
                            'left_token_id':a['token_id'],'right_token_id':b['token_id'],
                            'left_text':a['text'],'right_text':b['text'],'needs_review':True})
        for side, tokens, matched in (('left',left,matched_left),('right',right,matched_right)):
            for index, token in enumerate(tokens):
                if index not in matched and token['token_id'] not in existing:
                    # A context-only hypothesis has no accepted owning-side match.
                    # Preserve it in evidence for review instead of adding text or
                    # silently implying that the seam was verified as complete.
                    events.append({'kind':'unmatched_unowned_boundary_token','boundary_ms':boundary,
                        'side':side,'token_id':token['token_id'],'text':token['text'],
                        'start_ms':token['start_ms'],'end_ms':token['end_ms'],'needs_review':True})
    result = []
    for segment in segments:
        tokens = segment.pop('_tokens', [])
        kept = [token for token in tokens if token['token_id'] not in removals]
        if tokens and not kept:
            continue
        if kept and len(kept) != len(tokens):
            segment['text'] = ''.join(token['text'] for token in kept).strip()
        if kept:
            segment['start_ms'] = max(0, kept[0]['start_ms'])
            segment['end_ms'] = min(duration_ms, kept[-1]['end_ms'])
        if segment['text'].strip():
            result.append(segment)
    return sorted(result, key=lambda row:(row['start_ms'],row['end_ms'],row['row_id'])), {
        'algorithm':'ordered_equal_token_overlap_alignment_v1','context_seconds':CONTEXT_SECONDS,
        'minimum_overlap_fraction':.5,'maximum_midpoint_difference_ms':250,
        'needs_review':any(event['needs_review'] for event in events),'events':events}


def serializable(value):
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    if hasattr(value, 'detach'):
        return value.detach().cpu().tolist()
    if hasattr(value, 'tolist'):
        return value.tolist()
    return value


def model_devices(models, requested):
    execution = {}
    for name, model in models.items():
        parameter = next(model.parameters(), None)
        actual = str(parameter.device) if parameter is not None else 'unknown'
        if (requested == 'cuda' and not actual.startswith('cuda')) or (requested == 'cpu' and actual != 'cpu'):
            raise RuntimeError(f'{name} 模型实际设备 {actual} 与请求 {requested} 不符')
        execution[name] = actual
    return execution


def speaker_features(model, samples, segments, window, control):
    import numpy as np
    from funasr.models.campplus.utils import sv_chunk
    rows = []
    for segment in segments:
        control.check()
        start = max(0, segment['start_ms'] * 16 - window['context_start'])
        end = min(len(samples), segment['end_ms'] * 16 - window['context_start'])
        if end - start < RATE // 2:
            continue
        clips = sv_chunk([[segment['start_ms'] / 1000, segment['end_ms'] / 1000, samples[start:end]]])
        # Bounded 16 x 1.5s speaker batches, even for uninterrupted long speech.
        for beginning in range(0, len(clips), 16):
            control.check()
            batch = clips[beginning:beginning + 16]
            records = model.generate(input=[clip[2] for clip in batch], fs=RATE, batch_size=len(batch))
            embeddings = np.concatenate([serializable(record['spk_embedding']) for record in records], axis=0)
            if len(embeddings) != len(batch):
                raise ValueError('CAM++ 特征数量与音频片段不一致')
            for clip, embedding in zip(batch, embeddings):
                vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
                if not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-12:
                    raise ValueError('CAM++ 返回无效特征')
                rows.append({'segment_id': segment['segment_id'], 'start_ms': round(clip[0] * 1000),
                             'end_ms': round(clip[1] * 1000), 'embedding': vector.tolist()})
    return rows


def speaker_sample_indices(vectors, times, limit=1200):
    """Time-stratified coverage plus local voice changes, bounded independent of N."""
    import numpy as np
    count = len(vectors)
    if count <= limit:
        return np.arange(count, dtype=int)
    # 60% spread over the whole timeline, 40% at the strongest local changes in
    # timeline strata. This preserves short speaker appearances between evenly
    # spaced samples without letting one noisy interval consume all samples.
    uniform_count = limit * 3 // 5
    targets = np.linspace(times[0], times[-1], uniform_count)
    right = np.minimum(np.searchsorted(times, targets), count - 1)
    left = np.maximum(0, right - 1)
    nearest = np.where(abs(times[left] - targets) <= abs(times[right] - targets), left, right)
    selected = set(nearest.tolist())
    changes = np.zeros(count, dtype=np.float32)
    changes[1:] = 1 - np.einsum('ij,ij->i', vectors[1:], vectors[:-1])
    strata = max(1, (limit - uniform_count) // 2)
    edges = np.linspace(times[0], times[-1] + 1, strata + 1)
    novelty = []
    for beginning, end in zip(edges[:-1], edges[1:]):
        low, high = np.searchsorted(times, [beginning, end])
        if high <= low:
            continue
        indices = np.arange(low, high)
        priority = np.lexsort((indices, -changes[indices]))
        novelty.extend(indices[priority[:2]].tolist())
    selected.update(novelty)
    # Sparse silence strata leave vacancies; fill uniformly over feature order.
    for index in np.linspace(0, count - 1, limit, dtype=int):
        if len(selected) >= limit:
            break
        selected.add(int(index))
    return np.asarray(sorted(selected), dtype=int)


def assign_speakers(segments, features, control):
    import numpy as np
    import torch
    from funasr.models.campplus.cluster_backend import ClusterBackend
    if not features:
        return {'embedding_count': 0, 'speaker_count': 0, 'device': 'cpu'}
    control.check()
    features = sorted(features, key=lambda row: (row['start_ms'], row['end_ms'], row['segment_id']))
    vectors = np.asarray([row['embedding'] for row in features], dtype=np.float32)
    vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    sample_indices = speaker_sample_indices(vectors, np.asarray([row['start_ms'] for row in features]))
    # Fixed seeds make retry/reopen independent of incidental process RNG state.
    np.random.seed(0)
    torch.manual_seed(0)
    sampled_labels = np.asarray(ClusterBackend()(torch.from_numpy(vectors[sample_indices])), dtype=int)
    control.check()
    if len(sampled_labels) != len(sample_indices):
        raise ValueError('全录音聚类结果数量不一致')
    cluster_labels = sorted(set(int(label) for label in sampled_labels if label >= 0))
    centers = []
    for label in cluster_labels:
        center = vectors[sample_indices[sampled_labels == label]].mean(axis=0)
        centers.append(center / max(float(np.linalg.norm(center)), 1e-12))
    minimum_cosine, minimum_margin, minimum_vote_share = .55, .05, .6
    labels = np.full(len(features), -1, dtype=int)
    confidence = np.zeros(len(features), dtype=np.float32)
    if centers:
        centers = np.asarray(centers, dtype=np.float32)
        for beginning in range(0, len(vectors), 512):
            control.check()
            similarity = vectors[beginning:beginning + 512] @ centers.T
            best = np.argmax(similarity, axis=1)
            scores = similarity[np.arange(len(best)), best]
            second = np.partition(similarity, -2, axis=1)[:, -2] if len(centers) > 1 else np.full(len(best), -1)
            accepted = (scores >= minimum_cosine) & (scores - second >= minimum_margin)
            labels[beginning:beginning + len(best)] = np.where(accepted,
                np.asarray(cluster_labels)[best], -1)
            confidence[beginning:beginning + len(best)] = np.where(accepted, scores, 0)
    votes = {}
    order = {}
    totals = {}
    scores_by_segment = {}
    for feature, label, score in zip(features, labels, confidence):
        weight = max(1, feature['end_ms'] - feature['start_ms'])
        totals[feature['segment_id']] = totals.get(feature['segment_id'], 0) + weight
        if label < 0:
            continue
        label = int(label)
        order[label] = min(order.get(label, feature['start_ms']), feature['start_ms'])
        counts = votes.setdefault(feature['segment_id'], {})
        counts[label] = counts.get(label, 0) + weight
        weighted = scores_by_segment.setdefault(feature['segment_id'], {})
        weighted[label] = weighted.get(label, 0) + weight * float(score)
    names = {label: f'说话人 {index + 1}' for index, label in enumerate(sorted(order, key=lambda key: (order[key], key)))}
    for segment in segments:
        counts = votes.get(segment['segment_id'])
        if counts:
            label = max(counts, key=lambda key: (counts[key], -order[key]))
            if counts[label] / totals[segment['segment_id']] >= minimum_vote_share:
                segment['speaker'] = names[label]
                segment['speaker_confidence'] = round(scores_by_segment[segment['segment_id']][label] / counts[label], 4)
    return {'embedding_count': len(features), 'speaker_count': len(names), 'device': 'cpu',
            'algorithm': 'CAM++ time-stratified/voice-change sample + FunASR ClusterBackend + cosine centroids',
            'seed': 0, 'sampling_limit': 1200, 'sampled_embeddings': len(sample_indices),
            'sampling': '60% timeline-uniform, 40% local-voice-change strata',
            'assignment_batch_size': 512, 'minimum_cosine': minimum_cosine,
            'minimum_margin': minimum_margin, 'minimum_segment_vote_share': minimum_vote_share,
            'unassigned_embeddings': int(np.count_nonzero(labels < 0)),
            'threshold_status': 'uncalibrated_requires_business_review'}


def run_audio(request, out, control, progress):
    import numpy as np
    source = request['inputs'][0]
    root = Path(request['models']) / 'audio'
    runner = request.get('runner') or {}
    folder = local_directory(runner.get('checkpoint_root') or out / 'audio-checkpoints')
    phase_times = {}
    started = time.monotonic()
    progress('核验音频与模型身份')
    control.check()
    if Path(source['path']).stat().st_size > MAX_BYTES:
        raise ValueError('音频原件超过 4 GiB 上限')
    if file_hash(source['path'], control) != source['blob_hash']:
        raise ValueError('音频原件内容身份不符')
    model_files = sorted(path for path in root.rglob('*') if path.is_file() and
                         not any(part.startswith('.') for part in path.relative_to(root).parts))
    hashes = {path.relative_to(root).as_posix(): file_hash(path, control) for path in model_files}
    # Explicit CPU retries may retain already committed GPU chunks. Device history
    # is stored per chunk; neither epoch, business generation nor load size changes
    # the underlying source/algorithm identity.
    content_options = {key: value for key, value in request['options'].items() if key != 'device'}
    identity = fingerprint({'format': FORMAT, 'source': source['blob_hash'], 'models': hashes,
        'options': content_options, 'core_seconds': CORE_SECONDS, 'context_seconds': CONTEXT_SECONDS,
        'module_sha256': file_hash(__file__), 'runtime_sha256': file_hash(__import__('sys').executable)})
    manifest = folder / 'identity.json'
    if manifest.exists():
        try:
            validate_checkpoint(manifest, identity)
        except CheckpointIdentityMismatch:
            raise
        except CheckpointCorruption as error:
            quarantine(folder, [manifest], error)
    # Check intact older records before introducing/rebuilding the manifest so
    # a damaged identity file cannot authorize another model's checkpoint set.
    if not manifest.exists():
        for candidate in [folder / 'decode.json', *folder.glob('chunk-*.json')]:
            if candidate.exists():
                try:
                    validate_checkpoint(candidate, identity)
                except CheckpointIdentityMismatch:
                    raise
                except CheckpointCorruption:
                    pass
        atomic_json(manifest, {'format': FORMAT, 'identity': identity})
    phase_times['identity_seconds'] = round(time.monotonic() - started, 3)
    started = time.monotonic()
    pcm, metadata, decode_reused = decode_pcm(source['path'], folder, identity,
        request['options'].get('channel'), control, progress)
    phase_times['decode_seconds'] = round(time.monotonic() - started, 3)
    started = time.monotonic()
    progress('生成回听音轨、波形与频谱')
    playback_and_figures(pcm, out, metadata, control)
    phase_times['figures_seconds'] = round(time.monotonic() - started, 3)
    total_samples = pcm.stat().st_size // 2
    chunks = list(windows(total_samples))
    saved, pending = [], []
    for window in chunks:
        path = folder / f'chunk-{window["index"]:05d}.json'
        if path.exists():
            try:
                value = validate_checkpoint(path, identity)
                payload = value.get('payload')
                if (value.get('window') != window or not isinstance(payload, dict) or
                    fingerprint(payload) != value.get('payload_sha256') or
                    not all(isinstance(payload.get(key), list) for key in ('segments','speaker_features','asr_original'))):
                    raise CheckpointCorruption('音频分块检查点完整性校验失败')
                saved.append(value)
                continue
            except CheckpointIdentityMismatch:
                raise
            except (CheckpointCorruption, KeyError, TypeError, OSError) as error:
                quarantine(folder, [path], error)
        pending.append(window)
    pipeline = speaker = torch = None
    execution = {}
    if pending:
        control.check()
        progress('加载转写、VAD、标点和 CAM++ 模型')
        started = time.monotonic()
        from funasr import AutoModel
        import torch
        if request['options']['device'] == 'cuda':
            if not torch.cuda.is_available():
                raise RuntimeError('请求 GPU 推理但 CUDA 不可用')
            torch.cuda.reset_peak_memory_stats()
        pipeline = AutoModel(model=str(root / 'paraformer-zh'), vad_model=str(root / 'fsmn-vad'),
            punc_model=str(root / 'ct-punc'), device=request['options']['device'],
            disable_update=True, disable_pbar=True, trust_remote_code=False)
        speaker = AutoModel(model=str(root / 'campplus'), device=request['options']['device'],
            disable_update=True, disable_pbar=True, trust_remote_code=False)
        execution = model_devices({'asr': pipeline.model, 'vad': pipeline.vad_model,
            'punctuation': pipeline.punc_model, 'speaker': speaker.model}, request['options']['device'])
        phase_times['model_load_seconds'] = round(time.monotonic() - started, 3)
    data = np.memmap(pcm, mode='r', dtype='<i2')
    batch_size = int(runner.get('audio_batch_size_s', 30))
    if batch_size not in (15, 30):
        raise ValueError('音频推理批量必须为 15 或 30 秒')
    try:
        for window in pending:
            control.check()
            started = time.monotonic()
            progress('转写与 CAM++ 特征', processed_ms=window['start'] // 16,
                     total_ms=metadata['duration_ms'], chunk_index=window['index'], total_chunks=len(chunks))
            samples = data[window['context_start']:window['context_end']].astype(np.float32) / 32768
            records = pipeline.generate(input=samples, fs=RATE, batch_size_s=batch_size,
                sentence_timestamp=True, pred_timestamp=True, return_spk_res=False, return_raw_text=True)
            asr_seconds = time.monotonic() - started
            control.check()
            try:
                segments = owned_segments(records, window, metadata['duration_ms'], identity)
            except ValueError:
                atomic_json(out / f'asr-unmatched-chunk-{window["index"]:05d}.json',
                            {'window': window, 'records': serializable(records)})
                raise
            feature_start = time.monotonic()
            features = speaker_features(speaker, samples, segments, window, control)
            payload = {'segments': segments, 'speaker_features': features, 'asr_original': serializable(records),
                       'boundary_tokens': boundary_tokens(records, window, metadata['duration_ms'], identity),
                       'timing': {'asr_seconds': round(asr_seconds, 3),
                                  'speaker_seconds': round(time.monotonic() - feature_start, 3)},
                       'resources': resources(torch), 'execution': execution,
                       'audio_batch_size_s': batch_size}
            value = {'format': FORMAT, 'identity': identity, 'window': window,
                     'payload_sha256': fingerprint(payload), 'payload': payload}
            path = folder / f'chunk-{window["index"]:05d}.json'
            atomic_json(path, value)
            saved.append(value)
            control.committed(path, window['index'], window['end'] // 16, metadata['duration_ms'])
    finally:
        data._mmap.close()
    saved.sort(key=lambda value: value['window']['index'])
    segments, boundary_alignment = reconcile_boundaries(saved, metadata['duration_ms'], identity)
    features = [row for value in saved for row in value['payload']['speaker_features']]
    final_path = folder / 'speakers.json'
    features_identity = fingerprint([value['payload_sha256'] for value in saved])
    started = time.monotonic()
    progress('全录音匿名说话人聚类', processed_ms=metadata['duration_ms'], total_ms=metadata['duration_ms'])
    final = None
    if final_path.exists():
        try:
            final = validate_checkpoint(final_path, identity)
            if final.get('features_identity') != features_identity or fingerprint(final['payload']) != final.get('payload_sha256'):
                raise CheckpointCorruption('全录音说话人检查点完整性校验失败或分块已重算')
            payload = final['payload']
            segments, clustering = payload['segments'], payload['clustering']
        except CheckpointIdentityMismatch:
            raise
        except (CheckpointCorruption, KeyError, TypeError, OSError) as error:
            quarantine(folder, [final_path], error)
            final = None
    if final is None:
        clustering = assign_speakers(segments, features, control)
        payload = {'segments': segments, 'clustering': clustering}
        atomic_json(final_path, {'format': FORMAT, 'identity': identity, 'features_identity': features_identity,
                                'payload_sha256': fingerprint(payload), 'payload': payload})
    phase_times['global_clustering_seconds'] = round(time.monotonic() - started, 3)
    control.check()
    atomic_json(out / 'asr-original.json', [{'chunk_index': value['window']['index'],
        'context_start_ms': value['window']['context_start'] // 16,
        'records': value['payload']['asr_original']} for value in saved])
    executions = {str(value['window']['index']): value['payload']['execution'] for value in saved}
    return {'algorithm': 'FunASR Paraformer + FSMN-VAD + ct-punc + CAM++',
        'device': request['options']['device'], 'funasr': importlib.metadata.version('funasr'),
        'torch': importlib.metadata.version('torch'), 'model_hashes': hashes,
        'segments': segments, 'speaker_segments': [{'segment_id': row['segment_id'], 'row_id': row['row_id'],
            'start_ms': row['start_ms'], 'end_ms': row['end_ms'], 'speaker': row['speaker']} for row in segments],
        'audio': metadata, 'outcome': 'transcribed' if segments else 'no_speech',
        'boundary_alignment': boundary_alignment,
        'execution': {'requested_device': request['options']['device'], 'chunk_devices': executions,
                      'fresh_inference_chunks': len(pending),
                      'device_execution': 'fresh_inference' if pending else 'verified_checkpoint_reuse_only',
                      'global_clustering': clustering, 'identity': identity},
        'performance': {'phases': phase_times, 'total_chunks': len(chunks), 'chunks_reused': len(chunks) - len(pending),
            'decode_reused': decode_reused, 'core_seconds': CORE_SECONDS, 'context_seconds': CONTEXT_SECONDS,
            'chunk_timings': [value['payload']['timing'] for value in saved], 'resources': resources(torch)},
        'limitations': ['说话人编号为本录音内的匿名分组，不代表身份；重叠语音需人工核对',
                        '时间戳为模型估计，引用前需回听所选声道',
                        '5 分钟核心块前后各 2 秒上下文；邻边相同 token 按原生时间重叠对齐',
                        *(['部分邻边文字已恢复或存在不确定重复，请按边界复核记录回听'] if boundary_alignment['needs_review'] else [])]}
