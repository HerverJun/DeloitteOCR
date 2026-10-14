"""Cross-check canonical midpoint grids against official structure-image XML.

This audit reads annotations/images only, never candidate geometry or OCR.
"""
import argparse
import io
import json
from pathlib import Path
import sys
import tarfile
import xml.etree.ElementTree as ET

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / 'src')]
from geometry_eval_common import ROOT, sha, write_json, verify_inputs
from ocr_workbench.geometry_labels import derive_grid_sample

REVISION = '35b1c097807e0b07ec5313879b85956b7b3890db'
URL = f'https://hf-mirror.com/datasets/bsmock/pubtables-1m/resolve/{REVISION}/PubTables-1M-Structure_Annotations_Test.tar.gz'


def main():
    p = argparse.ArgumentParser()
    for name in ('manifest', 'annotations', 'canonical', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    args = p.parse_args()
    manifest = verify_inputs(args.manifest)
    if manifest['split'] == 'test':
        raise ValueError('Label audit uses development/validation inputs, never the new test')
    args.output.mkdir(parents=True, exist_ok=True)
    archive_path = args.output / 'official-structure-test.tar.gz'
    receipt_path = args.output / 'download.json'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text('utf-8'))
        if sha(archive_path) != receipt['sha256']:
            raise ValueError('Official archive changed')
    else:
        import requests
        session = requests.Session(); session.trust_env = False
        with session.get(URL, stream=True, timeout=(20, 60)) as response:
            response.raise_for_status()
            with archive_path.open('wb') as out:
                for chunk in response.iter_content(1024 * 1024): out.write(chunk)
        write_json(receipt_path, {'url': URL, 'revision': REVISION, 'sha256': sha(archive_path), 'bytes': archive_path.stat().st_size})
    needed = {s['id'] for s in manifest['samples']}
    xmls = {}
    with tarfile.open(archive_path, 'r:gz') as archive:
        for member in archive:
            key = Path(member.name).stem
            if key in needed and member.isfile():
                content = archive.extractfile(member).read()
                path = args.output / 'xml' / (key + '.xml')
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(content)
                xmls[key] = ET.fromstring(content)
    annotations = json.loads(args.annotations.read_text('utf-8'))
    results = []
    for sample in annotations['samples']:
        canonical = json.loads((args.canonical / (sample['id'] + '.json')).read_text('utf-8'))
        derived = derive_grid_sample(sample, canonical)
        item = {'id': sample['id'], 'targets': len(sample['targets'])}
        try:
            xml = xmls[sample['id']]
            sx = sample['width'] / float(xml.findtext('size/width'))
            sy = sample['height'] / float(xml.findtext('size/height'))
            groups = {}
            for obj in xml.findall('object'):
                box = [float(obj.findtext('bndbox/' + k)) * scale
                       for k, scale in zip(('xmin', 'ymin', 'xmax', 'ymax'), (sx, sy, sx, sy))]
                groups.setdefault(obj.findtext('name'), []).append(box)
            rows = sorted(groups['table row'], key=lambda b: b[3])
            cols = sorted(groups['table column'], key=lambda b: b[2])
            if len(rows) != len(canonical['rows']) or len(cols) != len(canonical['columns']):
                raise ValueError('Official row/column count differs')
            errors = []
            for target in derived['targets']:
                rs = range(target['row'], target['row'] + target['row_span'])
                cs = range(target['column'], target['column'] + target['column_span'])
                rb = [min(rows[r][0] for r in rs), min(rows[r][1] for r in rs), max(rows[r][2] for r in rs), max(rows[r][3] for r in rs)]
                cb = [min(cols[c][0] for c in cs), min(cols[c][1] for c in cs), max(cols[c][2] for c in cs), max(cols[c][3] for c in cs)]
                expected = [max(rb[0], cb[0]), max(rb[1], cb[1]), min(rb[2], cb[2]), min(rb[3], cb[3])]
                errors.append(max(abs(a - b) for a, b in zip(expected, target['box'])))
            item.update(status='pass' if max(errors) <= 2 else 'mismatch', maximum_pixel_error=max(errors),
                        targets_within_2px=sum(e <= 2 for e in errors), xml_sha256=sha(args.output / 'xml' / (sample['id'] + '.xml')))
        except (KeyError, ValueError, TypeError) as error:
            item.update(status='failed', error=str(error))
        results.append(item)
    write_json(args.output / 'crosscheck.json', {'boundary_convention': 'full_grid', 'tolerance_pixels': 2,
        'annotations_sha256': sha(args.annotations), 'manifest_sha256': sha(args.manifest),
        'source_archive_sha256': sha(archive_path), 'uses_predictions_or_ocr': False,
        'complete': all(r['status'] == 'pass' for r in results), 'samples': results})
    print('Official XML:', len(results), 'tables;', sum(r['status'] == 'pass' for r in results), 'pass', flush=True)


if __name__ == '__main__':
    main()
