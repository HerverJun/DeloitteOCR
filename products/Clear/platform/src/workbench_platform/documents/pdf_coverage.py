"""Conservative evidence for PDF pages painted entirely with native text.

Any image, drawing, form, annotation, hidden text or unrecognised operation
keeps the existing native page coverage pipeline responsible for the page.
"""
import hashlib
import math
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import ArrayObject, ByteStringObject, FloatObject, NameObject, NumberObject, TextStringObject


FORMAT = 'clear-pdf-text-coverage-v1'
UNICODE_CMAPS = {f'/Uni{language}-{encoding}-{direction}'
                 for language in ('GB', 'CNS', 'JIS', 'KS')
                 for encoding in ('UCS2', 'UTF16') for direction in ('H', 'V')}
TEXT_OPERATORS = {b'Tj', b'TJ', b"'", b'"'}
ALLOWED_OPERATORS = TEXT_OPERATORS | {
    b'q', b'Q', b'cm', b'BT', b'ET', b'Tc', b'Tw', b'Tz', b'TL', b'Tf',
    b'Tr', b'Ts', b'Td', b'TD', b'Tm', b'T*', b'G', b'g', b'RG', b'rg', b'K', b'k',
}
NUMERIC_ARITY = {
    b'cm': 6, b'Tm': 6, b'Tc': 1, b'Tw': 1, b'Tz': 1, b'TL': 1,
    b'Tr': 1, b'Ts': 1, b'Td': 2, b'TD': 2,
    b'G': 1, b'g': 1, b'RG': 3, b'rg': 3, b'K': 4, b'k': 4,
}
NO_OPERANDS = {b'q', b'Q', b'BT', b'ET', b'T*'}
TEXT_POSITIONING = {b'Tm', b'Td', b'TD', b'T*'}


def finite_number(value):
    if not isinstance(value, (NumberObject, FloatObject)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def text_string(value):
    return isinstance(value, (TextStringObject, ByteStringObject))


def valid_operands(operator, operands):
    if operator in NUMERIC_ARITY:
        return len(operands) == NUMERIC_ARITY[operator] and all(finite_number(value) for value in operands)
    if operator in NO_OPERANDS:
        return not operands
    if operator == b'Tf':
        return len(operands) == 2 and isinstance(operands[0], NameObject) and finite_number(operands[1])
    if operator in {b'Tj', b"'"}:
        return len(operands) == 1 and text_string(operands[0])
    if operator == b'"':
        return len(operands) == 3 and all(finite_number(value) for value in operands[:2]) and text_string(operands[2])
    if operator == b'TJ':
        return (len(operands) == 1 and isinstance(operands[0], ArrayObject)
                and all(text_string(value) or finite_number(value) for value in operands[0]))
    return False


def text_only(page):
    if page.get('/Annots'):
        return False, 'annotations'
    content = page.get_contents()
    if content is None:
        return False, 'no_text_operations'
    resources = page.get('/Resources') or {}
    if hasattr(resources, 'get_object'):
        resources = resources.get_object()
    fonts = resources.get('/Font') or {}
    if hasattr(fonts, 'get_object'):
        fonts = fonts.get_object()
    has_text, has_font, in_text = False, False, False
    saved_fonts = []
    for operands, operator in content.operations:
        if operator not in ALLOWED_OPERATORS:
            return False, 'non_text_or_unknown_operation'
        if not valid_operands(operator, operands):
            return False, 'invalid_operation_operands'
        if operator == b'BT':
            if in_text:
                return False, 'unbalanced_text_object'
            in_text = True
        elif operator == b'ET':
            if not in_text:
                return False, 'unbalanced_text_object'
            in_text = False
        elif operator == b'q':
            if in_text:
                return False, 'unverified_graphics_state'
            saved_fonts.append(has_font)
        elif operator == b'Q':
            if in_text or not saved_fonts:
                return False, 'unverified_graphics_state'
            has_font = saved_fonts.pop()
        if operator in TEXT_OPERATORS | TEXT_POSITIONING and not in_text:
            return False, 'text_outside_text_object'
        if operator == b'Tr' and operands[0] not in (0, 1, 2):
            return False, 'hidden_or_clipped_text'
        if operator == b'Tf':
            if len(operands) != 2 or not 0 < float(operands[1]) < 10000:
                return False, 'unverified_font'
            font = fonts.get(operands[0])
            if font is None:
                return False, 'unverified_font'
            font = font.get_object()
            if font.get('/Subtype') not in {'/Type0', '/TrueType', '/Type1', '/MMType1'}:
                return False, 'unverified_font'
            if (font.get('/Subtype') == '/Type0' and not font.get('/ToUnicode')
                    and font.get('/Encoding') not in UNICODE_CMAPS):
                return False, 'unverified_font_mapping'
            has_font = True
        if operator in TEXT_OPERATORS:
            if not has_font:
                return False, 'unverified_font'
            has_text = True
    if in_text or saved_fonts:
        return False, 'unbalanced_text_or_graphics_state'
    return (True, 'native_text_operations') if has_text else (False, 'no_text_operations')


def inspect_pdf(path):
    """A failed inspection is unknown coverage, never evidence of completeness."""
    report = {'format': FORMAT, 'source_sha256': None, 'page_count': None, 'pages': []}
    try:
        with Path(path).open('rb') as stream:
            report['source_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
            stream.seek(0)
            reader = PdfReader(stream, strict=True)
            report['page_count'] = len(reader.pages)
            for number, page in enumerate(reader.pages, 1):
                try:
                    complete, reason = text_only(page)
                except Exception:
                    complete, reason = False, 'unreadable_content_stream'
                report['pages'].append({'page_number': number, 'text_only': complete, 'reason': reason})
    except Exception:
        report.update(page_count=None, pages=[], reason='unreadable_pdf')
    return report


def verified_pages(document, report, source_sha256):
    """Bind coverage to this exact source, complete page map and readable text."""
    if not isinstance(report, dict) or report.get('format') != FORMAT or report.get('source_sha256') != source_sha256:
        return set()
    pages = report.get('pages')
    count = report.get('page_count')
    if type(count) is not int or count < 1 or not isinstance(pages, list) or len(pages) != count:
        return set()
    if any(not isinstance(page, dict) or type(page.get('page_number')) is not int for page in pages):
        return set()
    expected = set(range(1, count + 1))
    if len(document['pages']) != count or {page['page_number'] for page in pages} != expected or {page['page_number'] for page in document['pages']} != expected:
        return set()
    readable = {page['page_number'] for page in document['pages'] if not page['needs_ocr']
                and not any('\ufffd' in block.get('text', '') for block in page['blocks'])}
    return {page['page_number'] for page in pages if page.get('text_only') is True} & readable
