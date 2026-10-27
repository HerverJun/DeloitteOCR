"""Source-only, durable decisions for an explicitly requested precision route."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from ocr_workbench.paths import require_path

from ocr_workbench.screen_diagnostics import observed_box, _area, _intersection

VERSION='screen-business-language-route-v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def decide_engine_route(preparation,evidence,source_sha256,input_version_id):
    """No candidate, truth, filename, sample ID or prior score participates."""
    path=require_path(str(preparation.get('input_path') or ''), '精度路线原图', role='read')
    if (preparation.get('input_sha256')!=source_sha256
            or hashlib.sha256(path.read_bytes()).hexdigest()!=source_sha256):
        raise ValueError('精度路线缺少当前原图字节绑定')
    regions=[box for region in preparation.get('business_regions',[]) if isinstance(region,dict)
        and (box:=observed_box({'box':region.get('table_box')})) is not None]
    readings=evidence.get('text',[]) if isinstance(evidence,dict) else []
    binding={'version':VERSION,'source_sha256':source_sha256,'input_version_id':input_version_id,
        'source_regions_sha256':_digest(regions),'source_evidence_sha256':_digest(evidence)}
    provenance=evidence.get('provenance',{}) if isinstance(evidence,dict) else {}
    trusted=(provenance.get('contains_reference_input') is False
        and provenance.get('image_version')==input_version_id
        and provenance.get('input_image_version')==input_version_id
        and provenance.get('source_sha256')==source_sha256
        and provenance.get('coordinate_space')=='exif_normalized_original'
        and provenance.get('source_candidate_id','original')=='original')
    selected=[item for item in readings if trusted and isinstance(item,dict)
        and str(item.get('source','')).startswith('PP-OCR')
        and item.get('kind') not in {'toolbar','taskbar','formula_bar','ruler','window_border','scrollbar','ui'}
        and (box:=observed_box(item)) and any(_intersection(box,region)/_area(box)>=.5 for region in regions)]
    content=' '.join(str(item.get('text','')) for item in selected)
    han=sum('\u3400'<=char<='\u9fff' for char in content)
    latin=sum('A'<=char<='Z' or 'a'<=char<='z' for char in content)
    if trusted and han>=20 and han>latin:
        engines=['hunyuan'];reason='cjk_business_region'
    elif trusted and latin>=20 and latin>=4*max(1,han):
        engines=['glm','hunyuan'];reason='latin_business_region'
    else:
        engines=['hunyuan'];reason='source_language_unverified_or_undecided'
    return {**binding,'engines':engines,'reason':reason,'han_count':han,'ascii_latin_count':latin,
        'matched_auxiliary_readings':len(selected),'matched_text_sha256':hashlib.sha256(content.encode('utf-8')).hexdigest(),
        'candidate_values_read':False,'reference_input_used':False,'human_review_required':True}


def frozen_engine_route(preparation,evidence,source_sha256,input_version_id):
    current=decide_engine_route(preparation,evidence,source_sha256,input_version_id)
    stored=preparation.get('engine_route_snapshot')
    if stored is not None and stored!=current:
        raise ValueError('已冻结的逐图模型路线或来源证据已变化')
    return deepcopy(stored or current)


def reserve_seconds(policy):
    return policy.get('technical_fallback_reserve_seconds',0) if policy.get('engine_route')=='business_language_v1' else 0
