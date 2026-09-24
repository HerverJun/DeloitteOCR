"""Pinned native vector candidates, preserving the default and one bounded variant.

The optional shading-free candidate removes only non-stroked filled rectangles.
It keeps source line geometry, original text and default tolerances. Both remain
explicitly reviewable alternatives; neither edits the adopted result.
"""
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys

from ocr_workbench.coordinates import box_polygon, validate_polygon


ROOT = Path(__file__).resolve().parents[2]


def without_shading(obj):
    # Thin filled rectangles are genuine vector rules in many government PDFs.
    # Three PDF points is a fixed pre-evaluation bound, never document-tuned.
    return not (obj.get('object_type') == 'rect' and obj.get('fill') and not obj.get('stroke')
                and min(obj.get('width',0),obj.get('height',0)) > 3)


def extract_tables(path, number, metadata, password=None):
    config=json.loads((ROOT/'config/pdf-table-tool.json').read_text('utf-8'))
    for relative, expected in config['files'].items():
        if hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()!=expected:
            raise ValueError('PDF 表格工具文件与锁定版本不一致')
    for package, expected in config['dependencies'].items():
        if version(package)!=expected:
            raise ValueError('PDF 表格工具依赖与锁定版本不一致: '+package)
    sys.path.insert(0,str(ROOT/config['source']))
    import pdfplumber
    if pdfplumber.__version__!=config['version']:
        raise ValueError('PDF 表格工具版本不一致')
    scale=metadata['render_dpi']/72*metadata['user_unit']
    output=[]; alternatives=[]
    with pdfplumber.open(path, password=password) as document:
        page=document.pages[number-1]
        # pdfplumber objects already use rotated, top-left MediaBox coordinates.
        # The rendered image uses the rotated CropBox; only scale/offset remain.
        crop=page.cropbox; page=page.crop(crop)
        transform=[scale,0,-crop[0]*scale,0,scale,-crop[1]*scale,0,0,1]
        default_tables=page.find_tables(table_settings=config['table_settings'])
        variants=[('default',default_tables)]
        if any(not without_shading(obj) for obj in page.rects):
            filtered=page.filter(without_shading).find_tables(table_settings=config['table_settings'])
            if [t.cells for t in filtered] != [t.cells for t in default_tables]:
                variants.append(('without-shading-v1',filtered))
        for variant, found in variants:
          destination=output if variant=='default' else alternatives
          for index, table in enumerate(found):
            xs=sorted({v for cell in table.cells for v in (cell[0],cell[2])})
            ys=sorted({v for cell in table.cells for v in (cell[1],cell[3])})
            cells=[]
            for ci, box in enumerate(table.cells):
                mapped=[(box[0]-crop[0])*scale,(box[1]-crop[1])*scale,
                        (box[2]-crop[0])*scale,(box[3]-crop[1])*scale]
                poly=validate_polygon(box_polygon(mapped),metadata['width'],metadata['height'])
                cells.append({'id':str(ci),'row':ys.index(box[1]),'column':xs.index(box[0]),
                    'row_span':ys.index(box[3])-ys.index(box[1]),'column_span':xs.index(box[2])-xs.index(box[0]),
                    'polygon':poly,'original_box':list(box),'text':'','confidence':None})
            box=table.bbox
            destination.append({'id':f'pdfplumber:{index}','rows':len(ys)-1,'columns':len(xs)-1,'cells':cells,
                'polygon':box_polygon([(box[0]-crop[0])*scale,(box[1]-crop[1])*scale,(box[2]-crop[0])*scale,(box[3]-crop[1])*scale]),
                'original_box':list(box),'transform':transform,'candidate_variant':variant})
    return {'component':'pdfplumber','tool_version':config['version'],'pdfplumber_tables':output,
        'settings':config['table_settings'],'settings_sha256':hashlib.sha256((ROOT/'config/pdf-table-tool.json').read_bytes()).hexdigest(),
        'coordinate_contract':'rotated-mediabox-top-left-to-crop-image','model_revisions':{'pdfplumber':config['version']},
        'experimental_alternatives': [{'variant':'without-shading-v1','pdfplumber_tables':alternatives}] if alternatives else []}
