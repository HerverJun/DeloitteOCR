"""Pure text can finish offline; mixed, unknown and damaged PDFs cannot bypass OCR."""
import hashlib

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from workbench_platform.documents.normalize import normalize
from workbench_platform.documents.pdf_coverage import inspect_pdf, verified_pages


def make_pdf(path, extra=b'', *, font_subtype='/Type1', annotations=False, content=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject(font_subtype), NameObject('/BaseFont'): NameObject('/Helvetica')})
    image = DecodedStreamObject()
    image.set_data(b'\x00\x00\x00')
    image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
        NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
        NameObject('/ColorSpace'): NameObject('/DeviceRGB'), NameObject('/BitsPerComponent'): NumberObject(8)})
    page[NameObject('/Resources')] = DictionaryObject({
        NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)}),
        NameObject('/XObject'): DictionaryObject({NameObject('/Im1'): writer._add_object(image)})})
    stream = DecodedStreamObject()
    stream.set_data(content if content is not None else b'BT /F1 12 Tf 10 10 Td (Native heading) Tj ET\n' + extra)
    page[NameObject('/Contents')] = writer._add_object(stream)
    if annotations:
        page[NameObject('/Annots')] = ArrayObject([DictionaryObject({NameObject('/Subtype'): NameObject('/Stamp')})])
    writer.write(path)
    return path


def native_document(path):
    source = {'asset_id': 'asset', 'asset_revision': 1,
        'blob_hash': hashlib.sha256(path.read_bytes()).hexdigest(), 'original_name': path.name}
    return normalize({'pages': [{'page_idx': 0, 'blocks': [{'type': 'text', 'content': 'Native heading'}]}]}, source, 'document')


def test_native_text_pdf_has_exact_source_bound_coverage(tmp_path):
    path = make_pdf(tmp_path / 'native.pdf')
    report = inspect_pdf(path)
    document = native_document(path)
    assert report['pages'][0]['text_only'] is True
    assert verified_pages(document, report, document['source']['blob_hash']) == {1}
    assert verified_pages(document, report, 'b' * 64) == set()
    # A page missing from native parsing cannot be declared complete.
    document['pages'] = []
    assert verified_pages(document, report, document['source']['blob_hash']) == set()


@pytest.mark.parametrize('extra', [
    b'q 100 0 0 100 0 0 cm /Im1 Do Q',  # Real bitmap below a native heading.
    b'0 0 50 50 re f',  # Vector drawing can include outlined glyphs.
    b'1 2 unsupportedOperator',
    b'BT 3 Tr (Hidden text) Tj ET',
    b'BT (Unterminated string',
])
def test_mixed_unknown_hidden_and_damaged_streams_keep_native_coverage_required(tmp_path, extra):
    path = make_pdf(tmp_path / 'unverified.pdf', extra)
    report = inspect_pdf(path)
    assert report['pages'][0]['text_only'] is False
    document = native_document(path)
    assert verified_pages(document, report, document['source']['blob_hash']) == set()


@pytest.mark.parametrize('options', [{'font_subtype': '/Type3'}, {'font_subtype': '/Type0'}, {'annotations': True}])
def test_font_charprocs_and_annotation_appearances_are_not_assumed_to_be_text(tmp_path, options):
    path = make_pdf(tmp_path / 'appearance.pdf', **options)
    assert inspect_pdf(path)['pages'][0]['text_only'] is False


def test_unreadable_pdf_and_empty_native_extraction_never_prove_completeness(tmp_path):
    broken = tmp_path / 'broken.pdf'
    broken.write_bytes(b'not a PDF')
    assert inspect_pdf(broken)['pages'] == []
    path = make_pdf(tmp_path / 'native.pdf')
    document = native_document(path)
    document['pages'][0]['needs_ocr'] = True
    assert verified_pages(document, inspect_pdf(path), document['source']['blob_hash']) == set()
    document['pages'][0]['needs_ocr'] = False
    document['pages'][0]['blocks'][0]['text'] = 'Unmapped glyph \ufffd'
    assert verified_pages(document, inspect_pdf(path), document['source']['blob_hash']) == set()


@pytest.mark.parametrize('extra', [
    b'1 2 Tj',
    b'BT /F1 12 Tf 1 Tj ET',
    b'BT /F1 12 Tf /Name Tj ET',
    b'BT /F1 12 Tf (one) (two) Tj ET',
    b'BT /F1 12 Tf (not an array) TJ ET',
    b'BT /F1 12 Tf [(one) /Name] TJ ET',
    b'BT /F1 12 Tf [(one) true] TJ ET',
    b'BT /F1 12 Tf [(one) [(nested)]] TJ ET',
    b'BT /F1 12 Tf (one) 1 \' ET',
    b'BT /F1 12 Tf 0 (missing number) " ET',
    b'BT /F1 12 Tf (spacing) 0 (one) " ET',
    b'1 0 0 1 0 cm',
    b'1 0 0 1 0 /Name cm',
    b'0 0 rg',
    b'0 0 true rg',
    b'(1) g',
    b'BT /F1 12 Tf 10 Td ET',
    b'BT /F1 12 Tf (100) Tz ET',
    b'BT /F1 (12) Tf (one) Tj ET',
    b'BT /F1 12 Tf 0 0 Tr ET',
    b'BT /F1 12 Tf ' + b'9' * 400 + b' 0 Td (one) Tj ET',
    b'BT /F1 12 Tf (Missing ET) Tj',
    b'ET',
    b'BT BT ET ET',
    b'q',
    b'Q',
    b'1 BT (Extra operand) Tj ET',
])
def test_malformed_known_operators_never_prove_native_coverage(tmp_path, extra):
    path = make_pdf(tmp_path / 'malformed.pdf', extra)
    report = inspect_pdf(path)
    assert report['pages'][0]['text_only'] is False
    document = native_document(path)
    assert verified_pages(document, report, document['source']['blob_hash']) == set()


def test_well_formed_text_arrays_quotes_matrices_and_states_keep_native_fast_path(tmp_path):
    path = make_pdf(tmp_path / 'native-operators.pdf', b'''q 1 0 0 1 0 0 cm
        0 G 0 g 0 0 0 RG 0 0 0 rg 0 0 0 1 K 0 0 0 1 k
        BT /F1 11 Tf 1 Tc 2 Tw 100 Tz 12 TL 0 Tr 0 Ts
        1 0 0 1 20 20 Tm 0 -12 TD 0 -12 Td (a) Tj
        [(b) -10 <63>] TJ (d) ' 0 0 (e) " T* ET Q''')
    report = inspect_pdf(path)
    assert report['pages'][0]['text_only'] is True


def test_graphics_restore_does_not_keep_a_font_that_was_only_set_inside_saved_state(tmp_path):
    path = make_pdf(tmp_path / 'restored-font.pdf', content=b'q /F1 12 Tf Q BT (No font selected) Tj ET')
    assert inspect_pdf(path)['pages'][0]['text_only'] is False
