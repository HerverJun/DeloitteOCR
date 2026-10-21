"""Write versioned exports to disk; media files are never buffered in memory."""
import csv
import os
import shutil
import uuid
import zipfile
from workbench_platform.batches.store import encoded
from workbench_platform.data_api.service import DataError
from .revisions import reviewed_result


def export_files(service, workspace, job, *, destination=None):
    item = service.get(workspace, job)
    if item['status'] != 'succeeded':
        raise DataError('分析尚未完成', 409)
    destination = destination or service.root / job / 'exports' / ('g'+str(item.get('generation',1))+'v' + str(item['revision']))
    destination.mkdir(parents=True, exist_ok=True)
    result = reviewed_result(item)
    field = 'segments' if item['kind'] == 'audio' else 'seals'
    rows = result.get(field, [])
    files = {}

    def write(name, text, encoding='utf-8'):
        path = destination / name
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temp.write_text(text, encoding=encoding)
        os.replace(temp, path)
        files[name] = path

    write('result.json', encoded({**item, 'result': result}))
    if rows:
        filename = 'reviewed.csv' if item.get('review') else 'draft.csv'
        path = destination / filename
        temporary = path.with_name(filename + '.' + uuid.uuid4().hex + '.tmp')
        with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['序号', '文字', '说话人', '主体', '编号', '原件', '页码', '开始毫秒', '结束毫秒', '结果ID', '复核状态'])
            for index, row in enumerate(rows):
                writer.writerow([index + 1, row.get('text', ''), row.get('speaker', ''), row.get('subject', ''),
                    row.get('number', ''), item['inputs'][0]['original_name'], row.get('page_number', ''),
                    row.get('start_ms', ''), row.get('end_ms', ''), row.get('row_id', ''), '已复核' if item.get('review') else '未复核'])
        os.replace(temporary, path)
        files[filename] = path
    if item['kind'] == 'audio':
        write('transcript.txt', '\n'.join(f"{r.get('speaker', '')}: {r['text']}" for r in rows))

        def stamp(ms, comma):
            ms = int(ms)
            return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d}{comma}{ms%1000:03d}'

        for extension, comma in [('srt', ','), ('vtt', '.')]:
            text = ('WEBVTT\n\n' if extension == 'vtt' else '') + '\n\n'.join(
                f"{i+1}\n{stamp(r['start_ms'], comma)} --> {stamp(r['end_ms'], comma)}\n{r['text']}"
                for i, r in enumerate(rows))
            write('transcript.' + extension, text)
    for name in result.get('output_files', {}):
        files[name] = service.output(workspace, job, name)[0]
    manifest = {name: {'bytes': path.stat().st_size, 'sha256': service.file_hash(path)}
                for name, path in files.items()}
    write('manifest.json', encoded(manifest))
    archive = destination / 'results.zip'
    required = sum(path.stat().st_size for path in files.values()) + 64 * 1024**2
    if shutil.disk_usage(destination).free < required:
        raise DataError('F 盘可用空间不足以生成导出包；请保留任务并腾出空间后重试', 409)
    temporary = destination / ('results.' + uuid.uuid4().hex + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
            for name, path in files.items():
                bundle.write(path, name)
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive, files
