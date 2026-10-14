"""Versioned geometry semantics; all working polygons are current-image pixels.

Logical intervals are half open. Provider adapters, not shape heuristics, define
the original coordinate format. Content ranges never confer full-cell capability.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import unicodedata

from ocr_workbench.coordinates import IDENTITY, bounds, box_polygon, matrix, polygon, validate_polygon

CONTRACT_VERSION = 2
ALGORITHM_VERSION = 'table-local-v2.1'
ORIGINS = {'direct_detection', 'structure_prediction', 'derived_from_cells', 'text_extent', 'manual'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def matching_text(value):
    # NFC deliberately avoids compatibility folding (e.g. circled identifiers).
    # Keep signs, punctuation, decimal points, currency and leading zeroes.
    return ''.join(unicodedata.normalize('NFC', value).split())


def checked_polygon(points, width, height):
    points=validate_polygon(points,width,height)
    # Convex polygons only: intersection uses convex clipping. Reject bow ties
    # and concave geometry instead of silently scoring its bounding rectangle.
    cross=[]
    for i,a in enumerate(points):
        b,c=points[(i+1)%len(points)],points[(i+2)%len(points)]
        cross.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
    if min(cross)<-1e-8 and max(cross)>1e-8:raise ValueError('invalid_polygon')
    edges=list(zip(points,points[1:]+points[:1]))
    def orientation(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    for i,(a,b) in enumerate(edges):
        for j,(c,d) in enumerate(edges):
            if j<=i+1 or (i==0 and j==len(edges)-1):continue
            if (orientation(a,b,c)*orientation(a,b,d)<0 and orientation(c,d,a)*orientation(c,d,b)<0) or a==c or b==d:
                raise ValueError('invalid_polygon')
    return points


def signed_area(points):
    return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points,points[1:]+points[:1]))/2


def intersection_area(subject, clip):
    """Sutherland–Hodgman clipping, preserving actual quadrilateral geometry."""
    output=list(subject);sign=1 if signed_area(clip)>0 else -1
    for a,b in zip(clip,clip[1:]+clip[:1]):
        incoming,output=output,[]
        if not incoming:break
        def side(p):return sign*((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]))
        prev=incoming[-1];dprev=side(prev)
        for current in incoming:
            dcur=side(current)
            if (dcur>=0)!=(dprev>=0):
                ratio=dprev/(dprev-dcur)
                output.append([prev[0]+ratio*(current[0]-prev[0]),prev[1]+ratio*(current[1]-prev[1])])
            if dcur>=0:output.append(current)
            prev,dprev=current,dcur
    return abs(signed_area(output)) if len(output)>=3 else 0.


def polygon_iou(a,b):
    overlap=intersection_area(a,b);union=abs(signed_area(a))+abs(signed_area(b))-overlap
    return overlap/union if union>0 else 0.


@dataclass(frozen=True)
class TextToken:
    id: str
    source_result: str
    engine: str
    image_version: str
    raw_text: str
    matching_text: str
    polygon: list
    granularity: str
    source_kind: str
    original_polygon: list
    transform: list


@dataclass(frozen=True)
class PredictedCell:
    id: str
    provider: str
    model_version: str
    table_id: str
    original_cell_id: str
    row_start: int
    row_end: int
    column_start: int
    column_end: int
    text: str
    cell_polygon: list | None
    geometry_origin: str
    original_polygon: list | None = None
    transform: list = field(default_factory=lambda: IDENTITY[:])
    index_mapping: dict = field(default_factory=dict)
    derivation: list = field(default_factory=list)
    reason_codes: list = field(default_factory=list)


@dataclass(frozen=True)
class AdoptedCell:
    id: str
    result_id: str
    revision: int
    table_snapshot: str
    row_start: int
    row_end: int
    column_start: int
    column_end: int
    raw_text: str
    matching_text: str


@dataclass
class TokenAssignment:
    token_id: str
    candidates: list
    adopted_cell_id: str | None
    conflicts: list
    source_result: str
    independent_evidence_id: str


@dataclass
class CellCorrespondence:
    adopted_cell_id: str
    predicted_cell_ids: list
    relation: str
    anchors: list
    scores: dict
    candidate_margin: float | None
    reason_codes: list


def tokens_from_blocks(blocks, *, source_result, image_version, width, height, engine='external'):
    tokens=[];rejected=[];ids=set()
    for i,block in enumerate(blocks):
        if not isinstance(block.get('text'),str) or not block['text'].strip() or block.get('kind')=='table':continue
        token_id=str(block.get('id') or f'{source_result}:token:{i}')
        if token_id in ids:raise ValueError('Duplicate text token ID')
        ids.add(token_id)
        if block.get('image_version',image_version)!=image_version:
            rejected.append({'id':token_id,'reason':'token_version_mismatch'});continue
        original=block.get('polygon')
        if block.get('coordinate_space','image-top-left-pixels')!='image-top-left-pixels' and not block.get('coordinate_transform'):
            rejected.append({'id':token_id,'reason':'coordinate_version_mismatch'});continue
        try:
            transform=matrix(block.get('coordinate_transform',IDENTITY))
            poly=checked_polygon(polygon(transform,original),width,height)
        except (ValueError,TypeError):
            rejected.append({'id':token_id,'reason':'invalid_polygon'});continue
        native=block.get('source')=='pdf-native' or block.get('source_kind')=='native'
        granularity=block.get('granularity','word' if native else 'line')
        if granularity not in ('word','line','phrase','character'):raise ValueError('Unknown token granularity')
        tokens.append(TextToken(token_id,str(source_result),block.get('source',engine),image_version,
            block['text'],matching_text(block['text']),poly,granularity,'native' if native else 'scan',original,transform))
    return tokens,rejected


def adopted_cells(table, *, table_id, result_id, revision):
    result=[];occupied=set();snapshot=fingerprint(table)
    for cell in table['cells']:
        r,c,rs,cs=(cell[k] for k in ('row','column','row_span','column_span'))
        if any(type(v) is not int for v in (r,c,rs,cs)) or min(r,c)<0 or min(rs,cs)<1 or r+rs>table['rows'] or c+cs>table['columns']:raise ValueError('span_conflict')
        slots={(y,x) for y in range(r,r+rs) for x in range(c,c+cs)}
        if slots&occupied:raise ValueError('span_conflict')
        occupied|=slots
        result.append(AdoptedCell(f'{table_id}:{r}:{c}',result_id,revision,snapshot,r,r+rs,c,c+cs,cell['text'],matching_text(cell['text'])))
    return result


def evidence_v2(*, content_polygons=(), cell_polygons=(), display_polygon=None, origin=None, **details):
    if origin is not None and origin not in ORIGINS:raise ValueError('Unknown geometry origin')
    full=bool(cell_polygons) and origin!='text_extent'
    scope='full_cell' if full else 'text_extent' if content_polygons else 'table_region' if display_polygon else 'image'
    return {**details,'contract_version':CONTRACT_VERSION,'range_semantics':scope,
            'content_polygons':list(content_polygons),'cell_polygons':list(cell_polygons),
            'display_polygon':display_polygon,'polygon':display_polygon,'geometry_origin':origin,
            'level':'cell' if full else 'region' if display_polygon else 'image',
            'capabilities':{'full_cell':full,'text_preview':bool(content_polygons),'pdf_cell_text':full},
            'coordinate_space':'image-top-left-pixels','interval_end':'exclusive'}


def full_cell_evidence(item):
    details=item.get('details',item)
    if details.get('contract_version',1)<2:return details.get('level')=='cell'
    return (details.get('range_semantics')=='full_cell' and details.get('capabilities',{}).get('full_cell') is True
            and details.get('capabilities',{}).get('pdf_cell_text') is True and bool(details.get('cell_polygons'))
            and details.get('geometry_origin') in ORIGINS-{'text_extent'} and details.get('level')=='cell')


def as_json(value):return asdict(value)
