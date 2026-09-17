"""Single-page CPU subprocess protocol. Passwords are received through stdin only.

No model pipeline or network download is used in this runtime. Process exit
releases PDFium, QPDF and Docling caches even after a decoder failure.
"""

import json
import math
from pathlib import Path
import sys
import time
import unicodedata

from ocr_workbench.coordinates import bounds, box_polygon, inverse, pdf_transform, polygon

MAX_PIXELS = 80_000_000
MAX_PAGES = 10000


def page_metadata(page, number, dpi):
    media = list(map(float, page.mediabox))
    crop = list(map(float, page.cropbox))
    # CropBox is clipped by MediaBox by PDFium, as required by ISO 32000.
    crop = [max(crop[0], media[0]), max(crop[1], media[1]),
            min(crop[2], media[2]), min(crop[3], media[3])]
    rotation = int(page.obj.get('/Rotate', 0)) % 360
    user_unit = float(page.obj.get('/UserUnit', 1))
    transform = pdf_transform(crop, rotation, dpi, user_unit)
    extent = bounds(polygon(transform, box_polygon(crop)))
    width, height = math.ceil(extent[2]), math.ceil(extent[3])
    return {'page_number': number, 'width_points': (crop[2]-crop[0])*user_unit,
            'height_points': (crop[3]-crop[1])*user_unit, 'crop_box': crop,
            'media_box': media, 'rotation': rotation, 'user_unit': user_unit,
            'render_dpi': dpi, 'width': width, 'height': height,
            'pdf_to_pixel': transform, 'exceeds_pixel_limit': width*height > MAX_PIXELS}


def inspect_document(request):
    import pikepdf
    path, dpi = Path(request['path']), request.get('dpi', 300)
    with pikepdf.open(path, password=request.get('password') or '') as pdf:
        if not 1 <= len(pdf.pages) <= MAX_PAGES:
            raise ValueError(f'PDF 必须包含 1–{MAX_PAGES} 页')
        pages = [page_metadata(page, n+1, dpi) for n, page in enumerate(pdf.pages)]
        return {'pages': pages, 'encrypted': pdf.is_encrypted,
                'metadata': {str(key): str(value) for key, value in pdf.docinfo.items()
                             if isinstance(value, (str, pikepdf.String))},
                'has_bookmarks': '/Outlines' in pdf.Root}


def _intersect(a, b):
    return max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))


def _area(box):
    return max(0, box[2]-box[0]) * max(0, box[3]-box[1])


def _unmapped(text):
    return (not text or '\ufffd' in text or '(cid:' in text or
            any(unicodedata.category(c) in ('Co', 'Cs', 'Cn') or
                (unicodedata.category(c) == 'Cc' and c not in '\t\n\r') for c in text))


def native_content(path, number, metadata, password=None, pdfium_chars=()):
    from docling_parse.pdf_parser import DoclingPdfParser, ContentConfig
    # Docling 7.19.1 already rotates and translates cells into the CropBox,
    # but dimension.crop_bbox still retains its absolute origin. Applying that
    # offset again shifts highlights. This contract is covered by real PDFs.
    parser = DoclingPdfParser(loglevel='fatal')
    doc = parser.load(path, password=password, lazy=True,
                      content_config=ContentConfig(include_bitmap_bytes=False))
    try:
        page = doc.get_page(number)
        scale = metadata['render_dpi'] / 72 * metadata['user_unit']
        page_width, page_height = page.dimension.width, page.dimension.height
        to_pixel = [scale, 0, 0, 0, -scale, page_height*scale, 0, 0, 1]
        to_pdf = inverse(metadata['pdf_to_pixel'])
        width, height = metadata['width'], metadata['height']

        def location(rect):
            mapped = polygon(to_pixel, [list(xy) for xy in rect.to_polygon()])
            if any(not math.isfinite(v) for xy in mapped for v in xy):
                return None
            if any(x < -0.01 or y < -0.01 or x > width+0.01 or y > height+0.01 for x, y in mapped):
                return None
            return [[min(width, max(0, x)), min(height, max(0, y))] for x, y in mapped]

        chars = []
        for cell in page.char_cells:
            poly = location(cell.rect)
            box = bounds(poly) if poly else None
            mode = int(cell.rendering_mode)
            # The parser reports UNKNOWN for the PDF default Tr=0. Resolve it
            # from PDFium's actual text object, never assume unknown means visible.
            matches = [c for c in pdfium_chars if box and c['box'] and c['text'] == cell.text
                       and _intersect(box, c['box']) > min(_area(box), _area(c['box']))*0.5]
            observed_modes = {c['mode'] for c in matches}
            if mode == -1 and len(observed_modes) == 1:
                mode = next(iter(observed_modes))
            chars.append({'text': cell.text, 'box': box,
                          'mode': mode, 'unmapped': _unmapped(cell.text) or any(c['unmapped'] for c in matches)})
        units, flagged = [], []
        for cell in page.word_cells or page.char_cells:
            poly = location(cell.rect)
            box = bounds(poly) if poly else None
            matches = [c for c in chars if box and c['box'] and _intersect(box, c['box']) > _area(c['box'])*0.5]
            modes = {c['mode'] for c in matches}
            reason = ('invalid_coordinates' if not poly or _area(box) < 0.01 else
                      'unmapped_characters' if _unmapped(cell.text) or any(c['unmapped'] for c in matches) else
                      'old_invisible_ocr' if modes and modes <= {3} else
                      'ambiguous_render_mode' if not modes or modes & {-1, 3, 7} else None)
            unit = {'text': cell.text, 'polygon': poly, 'pdf_polygon': polygon(to_pdf, poly) if poly else None,
                    'confidence': None, 'source': 'pdf-native', 'font_name': cell.font_name,
                    'rendering_modes': sorted(modes), 'reliable': reason is None, 'reason': reason,
                    'native_index': cell.index}
            (flagged if reason else units).append(unit)
        images = []
        for bitmap in page.bitmap_resources:
            poly = location(bitmap.rect)
            if poly and _area(bounds(poly)) >= 16:
                images.append({'polygon': poly, 'source': 'docling-parse', 'kind': 'bitmap'})
        return {'units': units, 'flagged': flagged, 'images': images,
                'parser': 'docling-parse', 'parser_version': '7.19.1',
                'coordinate_contract': 'rotated-crop-relative-bottom-left',
                'dimension': page.dimension.model_dump(mode='json'),
                'shape_count': len(page.shapes), 'char_count': len(chars)}
    finally:
        doc.unload_pages((number, number+1))
        doc.unload()


def uncovered_regions(image, native):
    """PDF-aware bitmap regions plus visible raster content outside native text.

    This only proposes OCR crop regions. It never creates text/cell coordinates.
    Reliable native words are masked for coverage inspection, not OCR inference;
    ambiguous overlap is retained by the page merger as a review conflict.
    """
    from PIL import ImageDraw, ImageFilter
    width, height = image.size
    small = image.convert('L')
    small.thumbnail((800, 800))
    sx, sy = small.width / width, small.height / height
    draw = ImageDraw.Draw(small)
    for unit in native['units']:
        x0, y0, x1, y1 = bounds(unit['polygon'])
        draw.rectangle((x0*sx-3, y0*sy-3, x1*sx+3, y1*sy+3), fill=255)
    # Connect local ink into reading regions without allocating full-page arrays.
    small = small.point(lambda v: 0 if v < 205 else 255).filter(ImageFilter.MinFilter(7))
    pixels = small.load()
    seen = set()
    candidates = [bounds(entry['polygon']) for entry in native['images']]
    candidates.extend(bounds(entry['polygon']) for entry in native['flagged'] if entry['polygon'])
    for y in range(small.height):
        for x in range(small.width):
            if pixels[x, y] != 0 or (x, y) in seen:
                continue
            stack, count, bbox = [(x, y)], 0, [x, y, x, y]
            seen.add((x, y))
            while stack:
                px, py = stack.pop()
                count += 1
                bbox = [min(bbox[0], px), min(bbox[1], py), max(bbox[2], px), max(bbox[3], py)]
                for nx, ny in ((px-1, py), (px+1, py), (px, py-1), (px, py+1)):
                    if 0 <= nx < small.width and 0 <= ny < small.height and (nx, ny) not in seen and pixels[nx, ny] == 0:
                        seen.add((nx, ny))
                        stack.append((nx, ny))
            if count >= 12:
                candidates.append([max(0, (bbox[0]-2)/sx), max(0, (bbox[1]-2)/sy),
                                   min(width, (bbox[2]+3)/sx), min(height, (bbox[3]+3)/sy)])
    # Merge touching proposals, preserving every uncovered component.
    merged = []
    for box in candidates:
        again = True
        while again:
            again = False
            for index, previous in enumerate(merged):
                gap = [box[0]-8, box[1]-8, box[2]+8, box[3]+8]
                if _intersect(gap, previous) > 0:
                    box = [min(box[0], previous[0]), min(box[1], previous[1]),
                           max(box[2], previous[2]), max(box[3], previous[3])]
                    merged.pop(index)
                    again = True
                    break
        merged.append(box)
    # Huge collections of vector fragments are processed together as a page.
    if len(merged) > 100:
        merged = [[0, 0, width, height]]
    return [{'polygon': box_polygon(b), 'kind': 'ocr-needed', 'source': 'pdf-coverage',
             'overlaps_native': any(_intersect(b, bounds(u['polygon'])) > 0 for u in native['units'])}
            for b in sorted(merged, key=lambda b: (b[1], b[0]))]


def render_page(request):
    import pikepdf
    import pypdfium2 as pdfium
    started = time.perf_counter()
    path = Path(request['path'])
    number, dpi = request['page_number'], request.get('dpi', 300)
    password = request.get('password') or ''
    with pikepdf.open(path, password=password) as pdf:
        if type(number) is not int or not 1 <= number <= len(pdf.pages):
            raise ValueError('页码超出范围')
        metadata = page_metadata(pdf.pages[number-1], number, dpi)
    if metadata['exceeds_pixel_limit']:
        raise ValueError('此页在当前 DPI 下超过 8000 万像素，请降低渲染 DPI 后重试')
    document = pdfium.PdfDocument(path, password=password)
    page = None
    try:
        page = document[number-1]
        bitmap = page.render(scale=dpi/72 * metadata['user_unit'])
        try:
            image = bitmap.to_pil().convert('RGB')
        finally:
            bitmap.close()
        if image.width*image.height > MAX_PIXELS:
            raise ValueError('PDF 渲染结果超过 8000 万像素，请降低 DPI')
        metadata['width'], metadata['height'] = image.size
        image.save(request['image_output'], format='PNG')
        import pypdfium2.raw as raw
        textpage = page.get_textpage()
        pdfium_chars = []
        try:
            for index in range(textpage.count_chars()):
                textobj = raw.FPDFText_GetTextObject(textpage.raw, index)
                box = list(textpage.get_charbox(index))
                try:
                    mapped = bounds(polygon(metadata['pdf_to_pixel'], box_polygon(box)))
                except ValueError:
                    mapped = None
                pdfium_chars.append({'text': chr(raw.FPDFText_GetUnicode(textpage.raw, index)),
                    'box': mapped, 'mode': raw.FPDFTextObj_GetTextRenderMode(textobj) if textobj else -1,
                    'unmapped': raw.FPDFText_HasUnicodeMapError(textpage.raw, index) == 1})
        finally:
            textpage.close()
        native = native_content(path, number, metadata, password or None, pdfium_chars)
        # Auxiliary table detection runs after this base page is published, in
        # a separate CPU request. Its timeout cannot discard native extraction.
        regions = uncovered_regions(image, native)
        native['coverage_regions'] = regions
        native['page_class'] = ('mixed' if native['units'] and regions else
                                'native' if native['units'] else
                                'scan' if regions else 'blank')
        return {'metadata': metadata, 'native': native,
                'seconds': time.perf_counter()-started, 'render_engine': 'pdfium'}
    finally:
        if page is not None:
            page.close()
        document.close()


def tiff_document(request):
    from PIL import Image
    from ocr_workbench.image_utils import normalized_rgb
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(request['path']) as source:
        if not 1 <= source.n_frames <= MAX_PAGES:
            raise ValueError('TIFF 页数超出范围')
        if request['operation'] == 'inspect-tiff':
            return {'pages': [{'page_number': n+1, 'render_dpi': request.get('dpi', 300)} for n in range(source.n_frames)]}
        source.seek(request['page_number']-1)
        if source.width*source.height > MAX_PIXELS:
            raise ValueError('TIFF 此页超过 8000 万像素，请缩小后导入')
        image = normalized_rgb(source)
        image.save(request['image_output'], format='PNG')
        return {'metadata': {'width': image.width, 'height': image.height},
                'native': {'units': [], 'flagged': [], 'images': [], 'page_class': 'scan',
                    'coverage_regions': [{'polygon': box_polygon([0, 0, image.width, image.height]),
                                         'kind': 'ocr-needed', 'source': 'tiff'}]}}


def main():
    request = json.load(sys.stdin)
    # No request echo, command-line password, or secret-bearing traceback.
    output = Path(request.pop('response_path'))
    from ocr_workbench.offline import install_guard
    install_guard(output.parent / 'network-blocked.jsonl')
    try:
        if request['operation'] == 'inspect':
            result = inspect_document(request)
        elif request['operation'] == 'render':
            result = render_page(request)
        elif request['operation'] == 'table-structure':
            from ocr_workbench.pdf_tables import extract_tables
            result = extract_tables(request['path'], request['page_number'], request['metadata'], request.get('password'))
        elif request['operation'] in ('inspect-tiff', 'render-tiff'):
            result = tiff_document(request)
        elif request['operation'] == 'export':
            from ocr_workbench.pdf_export_worker import export_document
            result = export_document(request)
        else:
            raise ValueError('未知 PDF 操作')
        response = {'status': 'success', 'result': result}
    except Exception as error:
        import pikepdf
        from pdfminer.pdfdocument import PDFPasswordIncorrect
        # Upstream adapters may wrap the typed password exception; preserve its
        # control-flow meaning without guessing from localized error strings.
        current, seen, unlock = error, set(), False
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            unlock |= isinstance(current, (pikepdf.PasswordError, PDFPasswordIncorrect))
            current = current.__cause__ or current.__context__
        message = ('PDF 需要密码或密码不正确' if unlock else
                   str(error) if isinstance(error, ValueError) else 'PDF 解析或渲染失败，请检查文件与独立运行时')
        secret = request.get('password')
        if secret:
            message = message.replace(secret, '[redacted]')
        response = {'status': 'waiting_unlock' if unlock else 'error', 'message': message,
                    'type': type(error).__name__}
    from ocr_workbench.atomic_files import write_json
    write_json(output, response)


if __name__ == '__main__':
    main()
