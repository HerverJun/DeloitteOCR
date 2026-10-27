"""Offline, bounded page rendering in the already installed document runtime."""
from __future__ import annotations

import hashlib
import configparser
import json
import math
import os
from pathlib import Path
import posixpath
import socket
import shutil
import subprocess
import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET

NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def checked(path):
    package = os.environ.get('CLEAR_RUNTIME_PACKAGE_ROOT')
    if package and package not in sys.path:
        sys.path.insert(0, package)
    from packages.runtime import require_path
    path = require_path(path, 'Page rendering', role='read')
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('Page rendering cannot traverse a reparse point')
    return path.resolve()


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ppt_metadata(path):
    """Extract diagnostics only: visibility is determined by rendered pixels."""
    with zipfile.ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 512 * 1024 * 1024:
            raise ValueError('PPTX expanded content exceeds 512 MiB')
        # Office must not load linked content while rendering a local document.
        for name in archive.namelist():
            if name.endswith('.rels'):
                for rel in ET.fromstring(archive.read(name)):
                    if rel.get('TargetMode') == 'External' and not rel.get('Type', '').endswith('/hyperlink'):
                        raise ValueError('PPTX contains externally linked media; embed it before parsing')
        main = ET.fromstring(archive.read('ppt/presentation.xml'))
        size = main.find('p:sldSz', NS)
        width, height = int(size.get('cx')), int(size.get('cy'))
        relationships = {rel.get('Id'): rel.get('Target') for rel in
                         ET.fromstring(archive.read('ppt/_rels/presentation.xml.rels'))}
        slides = []
        for number, slide in enumerate(main.findall('p:sldIdLst/p:sldId', NS), 1):
            target = relationships[slide.get('{' + NS['r'] + '}id')]
            name = target.lstrip('/') if target.startswith('/') else posixpath.normpath('ppt/' + target)
            if not name.startswith('ppt/slides/'):
                raise ValueError('Invalid slide relationship')
            root = ET.fromstring(archive.read(name))
            candidates = []
            tree = root.find('p:cSld/p:spTree', NS)
            for shape in list(tree) if tree is not None else []:
                # Group transforms/rotations are deliberately diagnostic-only.
                transform = shape.find('p:spPr/a:xfrm', NS)
                if transform is None:
                    transform = shape.find('p:xfrm', NS)
                box = None
                if transform is not None and not transform.get('rot'):
                    off, ext = transform.find('a:off', NS), transform.find('a:ext', NS)
                    if off is not None and ext is not None:
                        x, y, w, h = (int(off.get('x')), int(off.get('y')), int(ext.get('cx')), int(ext.get('cy')))
                        if 0 <= x < x + w <= width and 0 <= y < y + h <= height:
                            box = [x / width, y / height, (x + w) / width, (y + h) / height]
                texts = [''.join(p.itertext()) for p in shape.findall('.//a:t', NS)]
                text = '\n'.join(texts).strip()
                native_table = shape.find('.//a:tbl', NS)
                table = None
                if native_table is not None:
                    rows = [[ ''.join(cell.itertext()) for cell in row.findall('a:tc', NS)]
                            for row in native_table.findall('a:tr', NS)]
                    table = {'rows': rows}
                if text:
                    properties = shape.find('.//p:cNvPr', NS)
                    candidates.append({'shape_id': properties.get('id') if properties is not None else None,
                                       'text': text, 'bbox': box, 'table': table,
                                       'visibility': 'unverified', 'role': 'diagnostic_only'})
            slides.append({'slide_id': slide.get('id'), 'slide_number': number,
                           'hidden': root.get('show', '1') in {'0', 'false'},
                           'native_candidates': candidates, 'slide_size_emu': [width, height]})
        return slides


def encode_image(image, output, number, config):
    from PIL import Image, ImageOps
    image = ImageOps.exif_transpose(image)
    if image.width * image.height > 80_000_000:
        raise ValueError('Page image exceeds 80 million pixels')
    original = [image.width, image.height]
    # Transparent pixels may contain invisible text. Composite the displayed
    # surface before RGB conversion so those hidden values never become OCR input.
    if image.mode in {'RGBA', 'LA'} or image.mode == 'P' and 'transparency' in image.info:
        rgba = image.convert('RGBA')
        image = Image.alpha_composite(Image.new('RGBA', rgba.size, 'white'), rgba).convert('RGB')
    else:
        image = image.convert('RGB')
    verified_blank = all(low == high == 255 for low, high in image.getextrema())
    edge = config.get('max_image_edge', 2560)
    image.thumbnail((edge, edge))
    path = output / f'page-{number:04d}.png'
    image.save(path, format='PNG', optimize=False)
    mime = 'image/png'
    limit = config.get('image_max_bytes', 8 * 1024 * 1024)
    if path.stat().st_size > limit:
        path.unlink()
        path = output / f'page-{number:04d}.jpg'
        mime = 'image/jpeg'
        for quality in (95, 90, 85):
            image.save(path, format='JPEG', quality=quality, subsampling=0)
            if path.stat().st_size <= limit:
                break
        if path.stat().st_size > limit:
            raise ValueError('Page image exceeds byte budget without excessive quality loss')
    return {'image_path': str(path), 'image_sha256': digest(path), 'media_type': mime,
            'width': image.width, 'height': image.height, 'original_size': original,
            'image_bytes': path.stat().st_size, 'rendered_page_number': number, 'verified_blank': verified_blank}


def convert_ppt(source, output, office):
    """LibreOffice profile internals exceed MAX_PATH under deep revision folders."""
    short_root = checked(os.environ.get('CLEAR_LAYOUT_TEMP_ROOT') or Path(os.environ['CLEAR_PADDLE_TEMP']) / 'layout')
    short_root.mkdir(parents=True, exist_ok=True)
    scratch = checked(short_root / uuid.uuid4().hex[:12])
    scratch.mkdir()
    success = False
    record = {'scratch': str(scratch), 'output': str(output), 'owned': True, 'cleaned': False}
    (output / 'office-conversion.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    try:
        office_env = os.environ.copy()
        for key in ('TEMP','TMP','TMPDIR','APPDATA','LOCALAPPDATA','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME'):
            directory = checked(scratch / key.lower())
            directory.mkdir(exist_ok=True)
            office_env[key] = str(directory)
        profile = checked(scratch / 'profile')
        export = scratch / 'export'
        export.mkdir()
        office_input = scratch / 'source.pptx'
        shutil.copyfile(source, office_input)
        options = 'pdf:impress_pdf_Export:' + json.dumps({
            'ExportHiddenSlides': {'type': 'boolean', 'value': 'true'},
            'ExportNotesPages': {'type': 'boolean', 'value': 'false'},
            'ExportOnlyNotesPages': {'type': 'boolean', 'value': 'false'},
            'ExportNotes': {'type': 'boolean', 'value': 'false'}}, separators=(',', ':'))
        result = subprocess.run([str(office), '-env:UserInstallation=' + profile.as_uri(), '--headless',
                                 '--nologo', '--nodefault', '--nolockcheck', '--norestore',
                                 '--convert-to', options, '--outdir', str(export), str(office_input)],
                                cwd=scratch, env=office_env, capture_output=True, timeout=600,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        (output / 'libreoffice.log').write_bytes(result.stdout + result.stderr)
        record['returncode'] = result.returncode
        rendered = export / 'source.pdf'
        if result.returncode or not rendered.is_file():
            raise ValueError(f'LibreOffice failed to export the presentation (exit {result.returncode})')
        destination = checked(output / 'office-export')
        destination.mkdir(exist_ok=True)
        pdf = destination / 'source.pdf'
        shutil.copyfile(rendered, pdf)
        record['pdf_sha256'] = digest(pdf)
        if record['pdf_sha256'] != digest(rendered):
            raise ValueError('Converted PDF changed while copying to the revision output')
        success = True
        return pdf
    finally:
        if success:
            # Only this invocation's random, verified child is ever removed.
            actual = checked(scratch)
            if actual.parent != short_root or actual.name != scratch.name:
                raise ValueError('Office scratch cleanup ownership mismatch')
            shutil.rmtree(actual)
            record['cleaned'] = True
        (output / 'office-conversion.json').write_text(json.dumps(record, indent=2), encoding='utf-8')


def render(request):
    from PIL import Image, ImageDraw, ImageFont
    import pypdfium2 as pdfium
    output = checked(request['output'])
    output.mkdir(parents=True, exist_ok=True)
    if request.get('mode') == 'probe':
        image = Image.new('RGB', (640, 180), 'white')
        font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 48)
        ImageDraw.Draw(image).text((25, 55), request['expected_text'], fill='black', font=font)
        record = encode_image(image, output, 1, {'max_image_edge': 640, 'image_max_bytes': 65536})
        image.close()
        return [record]
    sources = [checked(path) for path in request['sources']]
    if not sources or any(not path.is_file() or path.stat().st_size > 128 * 1024 * 1024 for path in sources):
        raise ValueError('Missing input or file exceeds 128 MiB')
    config = request['config']
    slides = None
    office_identity = None
    source = sources[0]
    suffix = request.get('suffix', source.suffix).lower()
    if suffix == '.pptx':
        if len(sources) != 1:
            raise ValueError('PPTX input must contain one presentation')
        slides = ppt_metadata(source)
        if not slides or len(slides) > min(request['max_pages'], 100):
            raise ValueError('PPTX exceeds 100 slides or contains no slides')
        office = checked(request['libreoffice'])
        version = configparser.ConfigParser(interpolation=None)
        version.read(office.parent / 'version.ini', encoding='utf-8')
        office_identity = {'id': 'LibreOffice', 'build_id': version.get('Version', 'buildid', fallback='unknown'),
                           'binary_sha256': digest(office), 'export_hidden_slides': True, 'export_notes': False,
                           'view': 'static'}
        source = convert_ppt(source, output, office)
        suffix = '.pdf'
    pages = []
    if suffix == '.pdf':
        if len(sources) != 1:
            raise ValueError('PDF input must contain one document')
        with pdfium.PdfDocument(source) as document:
            count = len(document)
            if not count or count > min(request['max_pages'], 100 if slides is not None else 200):
                raise ValueError('Document page limit exceeded or document is empty')
            if slides is not None and count != len(slides):
                raise ValueError('Rendered slide count does not match original slide mapping')
            for index in range(count):
                page = document[index]
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                        raise ValueError('Invalid PDF page dimensions')
                    scale = min(200 / 72, config.get('max_image_edge', 2560) / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        record = encode_image(image, output, index + 1, config)
                        image.close()
                    finally:
                        bitmap.close()
                    record.update(page_number=index + 1, source_size_points=[width, height],
                                  coordinate_transform={'scale_x': record['width']/width,
                                                        'scale_y': record['height']/height})
                    if slides is not None:
                        record.update(slides[index])
                    pages.append(record)
                finally:
                    page.close()
    else:
        if len(sources) > min(request['max_pages'], 100):
            raise ValueError('Image collection exceeds 100 pages')
        for index, path in enumerate(sources):
            with Image.open(path) as image:
                if getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('Multi-frame images require explicit frame selection')
                record = encode_image(image, output, index + 1, config)
                record.update(page_number=index + 1, source_image_sha256=digest(path))
                pages.append(record)
    for record in pages:
        record['renderer'] = {'id': 'clear-page-renderer', 'version': '1',
                              'pdfium_version': str(pdfium.PYPDFIUM_INFO)}
        if office_identity:
            record['renderer']['office'] = office_identity
    return pages


def main():
    request = json.loads(checked(sys.argv[1]).read_text('utf-8'))
    def offline(*args, **kwargs):
        raise OSError('Offline page renderer cannot access the network')
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = offline
    import tempfile
    tempfile.tempdir = str(checked(os.environ['TEMP']))
    pages = render(request)
    (checked(request['output']) / 'pages.json').write_text(
        json.dumps(pages, ensure_ascii=False, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
