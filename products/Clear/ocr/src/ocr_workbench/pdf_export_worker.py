"""Coordinate text layers through OCRmyPDF FPDF2; original pages through QPDF.

Upstream MPL-2.0 modules are imported unmodified. No OCRmyPDF OCR pipeline,
system fonts, glyphless fonts or network font fallback is invoked.
"""
import hashlib
import json
from pathlib import Path
import tempfile


class OfflineFonts:
    def __init__(self, folder):
        from ocrmypdf.font.font_manager import FontManager
        self.fonts = {name: FontManager(Path(folder) / (name+'.ttf')) for name in
                      ('NotoSans-Regular', 'NotoSansSC-Regular', 'NotoSansSymbols2-Regular')}

    def get_font(self, name):
        return self.fonts.get(name)

    def get_available_fonts(self):
        return list(self.fonts)

    def find_font_with_glyphs(self, text):
        for name, font in self.fonts.items():
            if all(c.isspace() or font.has_glyph(ord(c)) for c in text):
                return name, font
        return None

    def get_fallback_font(self):
        raise ValueError('当前离线字体不能完整覆盖文字，请检查缺字列表')

    def check(self, texts):
        missing = sorted({c for text in texts for c in text if not c.isspace() and
                          not any(font.has_glyph(ord(c)) for font in self.fonts.values())})
        if missing:
            raise ValueError('离线字体缺字：' + ' '.join(f'{c} (U+{ord(c):04X})' for c in missing[:40]))


def text_page(spec, fonts, output, *, image=False):
    from ocrmypdf.models.ocr_element import OcrElement, BoundingBox
    from ocrmypdf.fpdf_renderer.renderer import Fpdf2PdfRenderer
    from ocrmypdf.font.multi_font_manager import MultiFontManager
    from ocr_workbench.coordinates import bounds
    units = spec['units'] if image else [u for u in spec['units'] if not u['native_preserved']]
    fonts.check(u['text'] for u in units)
    page = OcrElement('ocr_page', bbox=BoundingBox(0, 0, spec['width'], spec['height']))
    for unit in units:
        x0, y0, x1, y1 = bounds(unit['polygon'])
        value = ' '.join(unit['text'].splitlines()).replace('\t', ' ')
        if not value.strip():
            continue
        # Typography is fitted within the reliable line/cell, never exposed as
        # inferred character/cell detection evidence. Mixed scripts use runs.
        runs = []
        for char in value:
            selected = fonts.find_font_with_glyphs(char)
            if not selected:
                raise ValueError('没有可用字体')
            name, font = selected
            if runs and runs[-1][0] == name:
                runs[-1][1] += char
            else:
                runs.append([name, char])
        widths = []
        import uharfbuzz as hb
        for name, text in runs:
            font = fonts.get_font(name)
            buffer = hb.Buffer(); buffer.add_str(text); buffer.guess_segment_properties()
            hb.shape(font.hb_font, buffer)
            widths.append(max(.01, sum(p.x_advance for p in buffer.glyph_positions)/font.hb_face.upem))
        words, left = [], x0
        for (name, text), width in zip(runs, widths):
            right = left + (x1-x0)*width/sum(widths)
            words.append(OcrElement('ocrx_word', text=text, bbox=BoundingBox(left, y0, right, y1)))
            left = right
        page.children.append(OcrElement('ocr_line', bbox=BoundingBox(x0,y0,x1,y1), children=words))
    Fpdf2PdfRenderer(page, spec['dpi'], MultiFontManager(font_provider=fonts), invisible_text=True,
                     image=Path(spec['image_path']) if image else None).render(output)


def clean_navigation(pdf, kept, rebuilt, report):
    """Resolve named destinations and remove targets omitted from a partial PDF."""
    import pikepdf
    names = {}
    def read_names(node):
        values = node.get('/Names', [])
        for i in range(0, len(values), 2): names[str(values[i])] = values[i+1]
        for child in node.get('/Kids', []): read_names(child)
    if '/Names' in pdf.Root and '/Dests' in pdf.Root.Names:
        read_names(pdf.Root.Names.Dests)
    if '/Dests' in pdf.Root:
        for key, value in pdf.Root.Dests.items(): names[str(key).lstrip('/')] = value
    def destination(value):
        if isinstance(value, (pikepdf.Name, pikepdf.String)):
            value = names.get(str(value).lstrip('/'))
        if isinstance(value, pikepdf.Dictionary): value = value.get('/D')
        if not isinstance(value, pikepdf.Array) or not value: return None
        first = value[0]
        if isinstance(first, int):
            if not 0 <= first < len(pdf.pages): return None
            first = pdf.pages[first].obj
        if first.objgen not in kept: return None
        if first.objgen in rebuilt:
            return pikepdf.Array([first, pikepdf.Name.Fit])
        return pikepdf.Array([first, *list(value)[1:]])
    try:
        with pdf.open_outline() as outline:
            def filter_items(items):
                retained = []
                for item in items:
                    item.children[:] = filter_items(item.children)
                    if item.destination is not None:
                        resolved = destination(item.destination)
                        if resolved is None:
                            report['removed_bookmarks'] += 1
                            retained.extend(item.children)
                            continue
                        item.destination = resolved
                    elif item.action is not None and item.action.get('/S') == pikepdf.Name.GoTo:
                        resolved = destination(item.action.get('/D'))
                        if resolved is None:
                            report['removed_bookmarks'] += 1
                            retained.extend(item.children)
                            continue
                        item.action.D = resolved
                    retained.append(item)
                return retained
            outline.root[:] = filter_items(outline.root)
    except (pikepdf.PdfError, ValueError, KeyError) as error:
        report['navigation_warnings'].append('损坏的书签：'+type(error).__name__)
        if '/Outlines' in pdf.Root: del pdf.Root.Outlines
    for page in pdf.pages:
        if page.obj.objgen not in kept: continue
        annotations = []
        for annotation in page.obj.get('/Annots', []):
            if '/Dest' in annotation:
                resolved = destination(annotation.Dest)
                if resolved is None:
                    report['removed_links'] += 1
                    continue
                annotation.Dest = resolved
            elif '/A' in annotation and annotation.A.get('/S') == pikepdf.Name.GoTo:
                resolved = destination(annotation.A.get('/D'))
                if resolved is None:
                    report['removed_links'] += 1
                    continue
                annotation.A.D = resolved
            annotations.append(annotation)
        if '/Annots' in page.obj: page.obj.Annots = pikepdf.Array(annotations)
    # Every retained link/outline now uses an explicit page object. Remove the
    # old name tree so omitted pages are not retained as dangling destinations.
    if '/Names' in pdf.Root and '/Dests' in pdf.Root.Names: del pdf.Root.Names.Dests
    if '/Dests' in pdf.Root: del pdf.Root.Dests
    if '/OpenAction' in pdf.Root:
        action = pdf.Root.OpenAction
        value = action.get('/D') if isinstance(action, pikepdf.Dictionary) and action.get('/S') == pikepdf.Name.GoTo else action
        if isinstance(value, (pikepdf.Array, pikepdf.Name, pikepdf.String)):
            resolved = destination(value)
            if resolved is None: del pdf.Root.OpenAction
            else: pdf.Root.OpenAction = resolved


def export_document(request):
    import pikepdf
    from ocrmypdf._graft import strip_invisible_text, _build_text_layer_ctm
    doc, output = request['document'], Path(request['output'])
    fonts = OfflineFonts(request['fonts'])
    original = doc['kind'] == 'pdf'
    pdf = pikepdf.open(doc['source_path'], password=request.get('password') or '') if original else pikepdf.Pdf.new()
    report = {'schema_version': 1, 'document_id': doc['id'], 'name': doc['name'], 'source_sha256': doc['source_sha256'],
              'format': 'searchable PDF', 'renderer': 'OCRmyPDF 17.11.0 FPDF2', 'pages': [], 'removed_links': 0,
              'removed_bookmarks': 0, 'navigation_warnings': [], 'claims': {'pdf_a': False, 'pdf_ua': False, 'signature_validity': False}}
    try:
        kept, rebuilt = set(), set()
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            for filename in doc['pages']:
                spec = json.loads(Path(filename).read_text('utf-8'))
                layer_path = Path(temporary) / 'layer.pdf'
                rebuild = bool(spec['rebuild_reasons'])
                if rebuild or any(not u['native_preserved'] for u in spec['units']):
                    text_page(spec, fonts, layer_path, image=rebuild)
                page = pdf.pages[spec['page_number']-1] if original else None
                if rebuild:
                    with pikepdf.open(layer_path) as layer:
                        pdf.pages.append(layer.pages[0])
                    replacement = pdf.pages[-1]
                    if page is not None:
                        report['removed_links'] += len(page.obj.get('/Annots', []))
                        page.emplace(replacement)
                        del pdf.pages[-1]
                    else: page = replacement
                    rebuilt.add(page.obj.objgen)
                else:
                    strip_invisible_text(pdf, page)
                    if any(not u['native_preserved'] for u in spec['units']):
                        with pikepdf.open(layer_path) as layer:
                            xobject = pdf.copy_foreign(layer.pages[0].as_form_xobject())
                            width, height = map(float, layer.pages[0].mediabox[2:])
                        crop = list(map(float, page.cropbox))
                        ctm = _build_text_layer_ctm(width, height, crop[2]-crop[0], crop[3]-crop[1], crop[0], crop[1], -int(page.obj.get('/Rotate', 0)) % 360)
                        # Detach inherited resources before adding a page-local layer.
                        page.obj.Resources = pikepdf.Dictionary(page.Resources)
                        page.Resources.XObject = pikepdf.Dictionary(page.Resources.get('/XObject', {}))
                        name = pikepdf.Name.random(prefix='OCR-')
                        page.Resources.XObject[name] = xobject
                        stream = b'q\n' + ((ctm.encode()+b' cm\n') if ctm is not None else b'') + bytes(name)+b' Do\nQ\n'
                        page.contents_add(pdf.make_stream(stream))
                kept.add(page.obj.objgen)
                report['pages'].append({k: v for k, v in spec.items() if k not in ('units', 'image_path')})
                report['pages'][-1]['positioned_units'] = len(spec['units'])
                report['pages'][-1]['mode'] = 'rebuilt' if rebuild else 'original-page-with-text-layer'
            clean_navigation(pdf, kept, rebuilt, report)
            for number in range(len(pdf.pages)-1, -1, -1):
                if pdf.pages[number].obj.objgen not in kept: del pdf.pages[number]
            manifest = json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8')
            pdf.attachments['OCR-sources.json'] = manifest
            pdf.save(output)
        # QPDF reopens the final file independently of its in-memory writer.
        with pikepdf.open(output) as verify:
            if len(verify.pages) != len(doc['pages']): raise ValueError('导出后页数校验失败')
            errors = verify.check_pdf_syntax()
            if errors: raise ValueError('导出 PDF 结构校验失败：' + str(errors[:3]))
        output.with_suffix('.sources.json').write_bytes(manifest)
        return {'path': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'pages': len(doc['pages'])}
    finally:
        pdf.close()
